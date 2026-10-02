# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Global vector retrieval with optional reranking of the recalled candidates.
"""

import asyncio
import contextvars
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from openviking.core.context import ContextLevel
from openviking.core.retrieval_targets import default_target_directories
from openviking.core.retrieval_types import SearchType
from openviking.models.embedder.base import EmbedResult, embed_compat
from openviking.models.rerank import RerankClient
from openviking.retrieve.retrieval_stats import get_stats_collector
from openviking.server.identity import RequestContext
from openviking.storage.abstract_overview import AbstractOverviewFormatError, body_for_preview
from openviking.storage.expr import FilterExpr
from openviking.storage.vikingdb_manager import VikingDBManager, VikingDBManagerProxy
from openviking.telemetry import get_current_telemetry
from openviking.utils.tags import normalize_search_tags
from openviking.utils.time_decay import parse_duration_ms
from openviking.utils.token_estimation import (
    estimate_text_tokens,
    truncate_text_to_token_budget,
)
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.retrieve.types import (
    ContextType,
    MatchedContext,
    QueryResult,
    TypedQuery,
)
from openviking_cli.utils.config import RetrievalConfig, RerankConfig
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)


class RerankBudget:
    """Accumulated rerank wall-clock budget for a single search.

    Charged per batch (never from request start), so intent analysis, embedding
    and vector retrieval never eat the rerank budget. Exhaustion is a skip, not a
    cancellation: remaining documents keep their vector scores, which costs
    ordering quality but not recall.
    """

    def __init__(self, total_seconds: float) -> None:
        self._total = max(0.0, float(total_seconds or 0.0))
        self._spent = 0.0

    @property
    def total_seconds(self) -> float:
        return self._total

    @property
    def spent_seconds(self) -> float:
        return self._spent

    @property
    def exhausted(self) -> bool:
        return self._total > 0 and self._spent >= self._total

    def add(self, seconds: float) -> None:
        if seconds > 0:
            self._spent += seconds


class RerankMemo:
    """Per-request rerank state: finished scores and in-flight scoring.

    Scores are keyed by (query, document) exactly as the provider sees them. The same
    pair is scored in several phases: measured, the leaf and directory passes overlapped
    on 100% of their documents and rounds re-scored 63% of theirs, so ~48% of a
    request's document calls were repeats whose score was already known.

    In-flight keys matter as much as finished ones: concurrent batches of one round can
    both miss the same document before either writes a score, which measured as ~16% of
    document calls still duplicated. A batch publishes a future per document it is about
    to score, so a concurrent batch joins that call instead of paying for a second one.
    """

    def __init__(self) -> None:
        self.scores: Dict[Tuple[str, str], float] = {}
        self.inflight: Dict[Tuple[str, str], asyncio.Future] = {}


class RetrieverMode(str):
    THINKING = "thinking"
    QUICK = "quick"


class HierarchicalRetriever:
    """Global retriever with dense and sparse vector support."""

    RERANK_CANDIDATE_MULTIPLIER = 2
    LEVEL_URI_SUFFIX = {0: ".abstract.md", 1: ".overview.md"}

    def _count_rerank_candidates(self, telemetry: Any, results: List[Dict[str, Any]]) -> None:
        """Bucket the documents a rerank batch would score.

        Directory summaries (L0/L1) are scored like any memory file, but the memory
        search tool drops them from what it returns. Their share of the scored
        volume is what decides whether scoring them pays for itself, and it has to
        be measurable on a rerank-off run too — the candidate set is the same one.

        Rows carry the directory URI and a level; the ``.abstract.md`` /
        ``.overview.md`` suffix only exists on the user-facing URI that
        ``_append_level_suffix`` reconstructs later, so level is the honest signal.
        """
        if not getattr(telemetry, "enabled", False):
            return
        summary_levels = tuple(self.LEVEL_URI_SUFFIX)
        summary_suffixes = tuple(self.LEVEL_URI_SUFFIX.values())
        telemetry.count("rerank.candidates", len(results))
        telemetry.count(
            "rerank.candidates.directory_summary",
            sum(
                1
                for r in results
                if r.get("level") in summary_levels
                or str(r.get("uri") or "").endswith(summary_suffixes)
            ),
        )

    def __init__(
        self,
        storage: VikingDBManager,
        embedder: Optional[Any],
        rerank_config: Optional[RerankConfig] = None,
        retrieval_config: Optional[RetrievalConfig] = None,
        rerank_client: Optional[Any] = None,
        rerank_executor: Optional[Any] = None,
    ):
        """Initialize retriever with rerank_config.

        Args:
            storage: VikingVectorIndexBackend instance
            embedder: Embedder instance (supports dense/sparse/hybrid)
            rerank_config: Rerank configuration (optional, will fallback to vector search only)
            retrieval_config: Retrieval ranking configuration.
            rerank_client: Process-shared rerank client. When omitted the retriever builds
                its own from ``rerank_config`` — the legacy per-call path kept for tests
                and standalone scripts.
            rerank_executor: Dedicated executor for the blocking provider call, so rerank
                work does not occupy the shared asyncio default pool.
        """
        self.vector_store = storage
        self.embedder = embedder
        self.rerank_config = rerank_config
        self.rerank_max_input_tokens = rerank_config.max_input_tokens if rerank_config else 0
        self.rerank_batch_timeout = rerank_config.batch_timeout if rerank_config else 0.0
        self.rerank_total_budget = rerank_config.total_budget if rerank_config else 0.0
        self.retrieval_config = retrieval_config or RetrievalConfig()
        self._rerank_executor = rerank_executor

        # Use rerank threshold if available, otherwise use a default
        self.threshold = rerank_config.threshold if rerank_config else 0

        # Initialize rerank client — all providers go through unified dispatch
        if rerank_client is not None:
            self._rerank_client = rerank_client
            logger.info(
                f"[HierarchicalRetriever] Rerank enabled (shared client), threshold={self.threshold}"
            )
        elif rerank_config and rerank_config.is_available():
            self._rerank_client = RerankClient.from_config(rerank_config)
            provider = rerank_config._effective_provider()
            logger.info(
                f"[HierarchicalRetriever] Rerank enabled (provider={provider}), threshold={self.threshold}"
            )
        else:
            self._rerank_client = None
            logger.info(
                f"[HierarchicalRetriever] Rerank not configured, using vector search only with threshold={self.threshold}"
            )

    async def retrieve(
        self,
        query: TypedQuery,
        ctx: RequestContext,
        limit: int = 5,
        mode: Optional[RetrieverMode] = None,
        score_threshold: Optional[float] = None,
        score_gte: bool = False,
        scope_dsl: Optional[FilterExpr | Dict[str, Any]] = None,
        level: Optional[List[int]] = None,
        events_time_decay_protection: Optional[str] = None,
        request_now: Optional[datetime] = None,
        search_type: SearchType = "semantic",
        rerank: bool = True,
    ) -> QueryResult:
        """
        Run one global vector search, then optionally rerank its candidates.

        Args:
            ctx: Request context used for tenant and permission filtering
            mode: QUICK uses vector scores; THINKING optionally reranks the recalled
                candidates. None selects THINKING when a reranker is configured.
            score_threshold: Custom score threshold (overrides config)
            score_gte: True uses >=, False uses >
            scope_dsl: Additional scope constraints passed from public find/search filter
            level: Optional result level filter (0=L0, 1=L1, 2=L2)
        """
        t0 = time.monotonic()
        telemetry = get_current_telemetry()
        effective_threshold = self._resolve_threshold(score_threshold)
        rerank_budget = RerankBudget(self.rerank_total_budget)
        rerank_memo = RerankMemo()
        image_query = bool(getattr(query, "image_query", False))
        if mode is None:
            mode = RetrieverMode.THINKING if self._rerank_client else RetrieverMode.QUICK
        use_rerank = (
            mode == RetrieverMode.THINKING
            and self._rerank_client is not None
            and not image_query
            and rerank
        )
        decay_kwargs = {}
        if events_time_decay_protection is not None:
            parse_duration_ms(
                events_time_decay_protection, parameter_name="events_time_decay_protection"
            )
            decay_kwargs = {
                "events_time_decay_protection": events_time_decay_protection,
                "request_now": request_now or datetime.now(timezone.utc),
            }
        if image_query and level is None:
            level = [2]

        # 创建 proxy 包装器，绑定当前 ctx
        vector_proxy = VikingDBManagerProxy(self.vector_store, ctx)

        target_dirs = [d for d in (query.target_directories or []) if d]

        if not await vector_proxy.collection_exists_bound():
            logger.warning(
                "[HierarchicalRetriever] Collection %s does not exist",
                vector_proxy.collection_name,
            )
            return QueryResult(
                query=query,
                matched_contexts=[],
                searched_directories=[],
            )

        # Generate query vectors once to avoid duplicate embedding calls
        query_vector = None
        sparse_query_vector = None
        if search_type == "semantic" and self.embedder:
            if image_query and not getattr(self.embedder, "supports_multimodal", False):
                raise InvalidArgumentError("Image search requires a multimodal embedding model.")
            with telemetry.measure("search.embed_query"):
                embedding_input = getattr(query, "embedding_input", None) or query.query
                result: EmbedResult = await embed_compat(
                    self.embedder,
                    embedding_input,
                    is_query=True,
                )
                query_vector = result.dense_vector
                sparse_query_vector = result.sparse_vector

        # Report the effective search scope in the query result.
        if target_dirs:
            root_uris = target_dirs
        else:
            root_uris = default_target_directories(ctx, context_type=query.context_type)

        context_type = query.context_type.value if query.context_type else None
        if image_query and context_type is None:
            context_type = ContextType.RESOURCE.value

        search_limit = limit * self.RERANK_CANDIDATE_MULTIPLIER if use_rerank else limit
        with telemetry.measure("search.vector_retrieval"):
            if search_type == "keywords":
                vector_results = await vector_proxy.search_by_keywords_in_tenant(
                    query=query.query,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=level,
                    limit=search_limit,
                )
            else:
                vector_results = await vector_proxy.search_in_tenant(
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    context_type=context_type,
                    target_directories=target_dirs,
                    extra_filter=scope_dsl,
                    level=level,
                    limit=search_limit,
                    **decay_kwargs,
                )
        telemetry.count("vector.searches", 1)
        telemetry.count("vector.scored", len(vector_results))
        telemetry.count("vector.scanned", len(vector_results))
        self._count_rerank_candidates(telemetry, vector_results)

        # Recall scores already include event decay from the vector engine.
        # Keep the highest-scored hit for each URI before model reranking.
        collected_by_uri: Dict[str, Dict[str, Any]] = {}
        for result in vector_results:
            uri = result.get("uri", "")
            if not uri:
                continue
            score = self._finite_score(result.get("_score", 0.0))
            previous = collected_by_uri.get(uri)
            if previous is None or score > previous["_score"]:
                collected_by_uri[uri] = {**result, "_score": score}

        candidates = sorted(
            collected_by_uri.values(), key=lambda candidate: candidate["_score"], reverse=True
        )
        scores = [candidate["_score"] for candidate in candidates]
        rerank_used = use_rerank and bool(candidates)
        if rerank_used:
            with telemetry.measure("search.rerank"):
                scores = await self._rerank_scores_timed(
                    query.query,
                    [str(candidate.get("abstract", "")) for candidate in candidates],
                    scores,
                    rerank_budget,
                    rerank_memo,
                )
            # Report use honestly: only scores that actually landed from the
            # provider count, not the mere presence of a client (984568444).
            rerank_used = bool(rerank_memo.scores)

        # A low vector score can still rerank highly, so filter only after reranking.
        candidates = [
            {**candidate, "_final_score": score}
            for candidate, score in zip(candidates, scores, strict=True)
            if self._passes_threshold(score, effective_threshold, score_gte)
        ]
        telemetry.count("vector.passed", len(candidates))
        matched = await self._convert_to_matched_contexts(candidates, ctx=ctx)
        final = matched[:limit]

        elapsed_ms = (time.monotonic() - t0) * 1000
        get_stats_collector().record_query(
            context_type=context_type or "unknown",
            result_count=len(final),
            scores=[m.score for m in final],
            latency_ms=elapsed_ms,
            rerank_used=rerank_used,
        )

        return QueryResult(
            query=query,
            matched_contexts=final,
            searched_directories=root_uris,
        )

    def _resolve_threshold(self, threshold: Optional[float]) -> float:
        resolved = threshold if threshold is not None else self.threshold
        return resolved if resolved is not None else 0.0

    @staticmethod
    def _finite_score(value: Any, default: float = 0.0) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return default
        return score if math.isfinite(score) else default

    @staticmethod
    def _is_finite_score(value: Any) -> bool:
        """True when the provider returned a usable score (not NaN/None/garbage)."""
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _passes_threshold(score: float, threshold: float, score_gte: bool) -> bool:
        if score_gte:
            return score >= threshold
        return score > threshold

    async def _rerank_scores_timed(
        self,
        query: str,
        documents: List[str],
        fallback_scores: List[float],
        budget: Optional[RerankBudget],
        memo: Optional[RerankMemo] = None,
    ) -> List[float]:
        """Score one sequential batch and charge its wall-clock time to the budget.

        Concurrent batches (a recursion round) are charged once for the whole
        gather at the call site, so parallel work never double-counts.
        """
        started = time.monotonic()
        try:
            return await self._rerank_scores(query, documents, fallback_scores, budget, memo)
        finally:
            if budget is not None:
                budget.add(time.monotonic() - started)

    async def _run_rerank_batch(self, query: str, documents: List[str]) -> Optional[List[float]]:
        """Run the blocking provider call off the event loop.

        Uses the injected rerank executor when available so rerank threads stay out
        of the shared asyncio default pool. ``run_in_executor`` does not copy
        contextvars the way ``asyncio.to_thread`` does, so the copy is explicit —
        without it the worker thread loses telemetry/observability attribution.
        """
        client = self._rerank_client
        if client is None:
            return None
        if self._rerank_executor is None:
            return await asyncio.to_thread(client.rerank_batch, query, documents)
        context = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(
            self._rerank_executor,
            lambda: context.run(client.rerank_batch, query, documents),
        )

    async def _rerank_scores(
        self,
        query: str,
        documents: List[str],
        fallback_scores: List[float],
        budget: Optional[RerankBudget] = None,
        memo: Optional[RerankMemo] = None,
    ) -> List[float]:
        """Return rerank scores or fall back to vector scores."""
        if not self._rerank_client or not documents:
            return fallback_scores

        rerank_query = query
        rerank_documents = [
            (index, document) for index, document in enumerate(documents) if document.strip()
        ]
        if not rerank_documents:
            return fallback_scores

        if self.rerank_max_input_tokens > 0:
            max_query_tokens = self.rerank_max_input_tokens * 3 // 4
            if estimate_text_tokens(query) > max_query_tokens:
                rerank_query = truncate_text_to_token_budget(query, max_query_tokens)
            document_tokens = self.rerank_max_input_tokens - estimate_text_tokens(rerank_query)
            rerank_documents = [
                (index, truncate_text_to_token_budget(document, document_tokens))
                for index, document in rerank_documents
            ]

        # Sort every document into one of three fates: already scored (memo), being
        # scored right now by a concurrent batch of this round (join its future), or
        # this batch's own work. Identical texts inside one batch collapse into a single
        # entry, so a repeated abstract is paid for once and scattered back to every
        # index it occupies.
        normalized_scores = list(fallback_scores)
        owned: Dict[str, List[int]] = {}
        joined: List[Tuple[int, str, asyncio.Future]] = []
        for index, document in rerank_documents:
            if memo is not None:
                key = (rerank_query, document)
                cached = memo.scores.get(key)
                if cached is not None:
                    normalized_scores[index] = cached
                    continue
                inflight = memo.inflight.get(key)
                if inflight is not None:
                    joined.append((index, document, inflight))
                    continue
            owned.setdefault(document, []).append(index)

        if not owned and not joined:
            # Zero-cost memo hits survive budget exhaustion: the skip below only
            # covers work that would reach the provider.
            return normalized_scores

        if owned and budget is not None and budget.exhausted:
            logger.warning(
                "[HierarchicalRetriever] Rerank budget of %.1fs exhausted (spent %.1fs); "
                "skipping %s document(s) and keeping vector scores",
                budget.total_seconds,
                budget.spent_seconds,
                sum(len(indices) for indices in owned.values()),
            )
            get_current_telemetry().count("rerank.skipped", 1)
            owned = {}

        if owned:
            await self._score_owned(rerank_query, owned, fallback_scores, normalized_scores, memo)
        if joined:
            # A joined document is already paid for by another batch, so it is still
            # worth adopting after this batch's own budget ran out.
            await self._join_inflight(joined, fallback_scores, normalized_scores)
        return normalized_scores

    async def _score_owned(
        self,
        rerank_query: str,
        owned: Dict[str, List[int]],
        fallback_scores: List[float],
        normalized_scores: List[float],
        memo: Optional[RerankMemo],
    ) -> None:
        """Score this batch's documents and publish the result to the memo and joiners."""
        pending: Dict[Tuple[str, str], asyncio.Future] = {}
        if memo is not None:
            loop = asyncio.get_running_loop()
            for document in owned:
                key = (rerank_query, document)
                future = loop.create_future()
                pending[key] = future
                memo.inflight[key] = future
        try:
            try:
                scores = await asyncio.wait_for(
                    self._run_rerank_batch(rerank_query, list(owned)),
                    timeout=self.rerank_batch_timeout or None,
                )
            except asyncio.TimeoutError:
                # The executor thread keeps running to completion; the dedicated pool
                # bounds that orphan instead of leaking it into the default pool.
                logger.warning(
                    "[HierarchicalRetriever] Rerank batch exceeded %.1fs, fallback to vector scores",
                    self.rerank_batch_timeout,
                )
                get_current_telemetry().count("rerank.timeouts", 1)
                return
            except Exception as e:
                logger.warning(
                    "[HierarchicalRetriever] Rerank failed, fallback to vector scores: %s", e
                )
                return

            if not scores or len(scores) != len(owned):
                logger.warning(
                    "[HierarchicalRetriever] Invalid rerank result, fallback to vector scores"
                )
                return

            for score, (document, indices) in zip(scores, owned.items(), strict=True):
                for index in indices:
                    normalized_scores[index] = self._finite_score(score, fallback_scores[index])
                finite = self._is_finite_score(score)
                # Cache real provider scores only: caching a fallback would freeze one
                # transient failure (a 429, say) into every later phase of the request.
                if memo is not None and finite:
                    memo.scores[(rerank_query, document)] = float(score)
                future = pending.get((rerank_query, document))
                if future is not None and not future.done():
                    future.set_result(float(score) if finite else math.nan)
        finally:
            # Settle on every exit path: a joiner must never wait on a key whose owner
            # already finished, and an unresolved future would strand it until its own
            # batch cut. Unsettled means "no score", so the joiner keeps its vector score
            # instead of inheriting a fake one.
            if memo is not None:
                for key, future in pending.items():
                    memo.inflight.pop(key, None)
                    if not future.done():
                        future.set_result(math.nan)

    async def _join_inflight(
        self,
        joined: List[Tuple[int, str, asyncio.Future]],
        fallback_scores: List[float],
        normalized_scores: List[float],
    ) -> None:
        """Adopt scores a concurrent batch of this round is already paying for.

        Waits without cancelling the shared future (``asyncio.wait``, not ``wait_for``):
        the future belongs to the owning batch, so cancelling this joiner's wait would
        strand every other joiner waiting on the same key.
        """
        done, _ = await asyncio.wait(
            {future for _, _, future in joined}, timeout=self.rerank_batch_timeout or None
        )
        for index, _document, future in joined:
            if future not in done or future.cancelled():
                continue
            if future.exception() is not None:
                continue
            value = future.result()
            # A joined failure (NaN) must not override the vector score either.
            if self._is_finite_score(value):
                normalized_scores[index] = self._finite_score(value, fallback_scores[index])

    async def _convert_to_matched_contexts(
        self,
        candidates: List[Dict[str, Any]],
        ctx: RequestContext,
    ) -> List[MatchedContext]:
        """Convert candidates to contexts ordered by vector or rerank score."""
        results = []
        for c in candidates:
            final_score = self._finite_score(c.get("_final_score", c.get("_score", 0.0)))
            level = c.get("level", 2)
            display_uri = self._append_level_suffix(c.get("uri", ""), level)
            abstract = c.get("abstract", "")
            if level in {ContextLevel.ABSTRACT, ContextLevel.OVERVIEW}:
                # New records persist body-only rerank scalars, but imported or
                # legacy indexes may still contain the full OKF document. Keep
                # the public find/search preview contract body-only at its final
                # conversion boundary. L2 user Markdown is intentionally left
                # untouched, including ordinary YAML frontmatter.
                try:
                    abstract = body_for_preview(abstract)
                except AbstractOverviewFormatError as exc:
                    logger.warning("Malformed sidecar in retrieval result %s: %s", display_uri, exc)
                    abstract = ""

            results.append(
                MatchedContext(
                    uri=display_uri,
                    context_type=ContextType(c["context_type"])
                    if c.get("context_type")
                    else ContextType.RESOURCE,
                    level=level,
                    abstract=abstract,
                    category=c.get("category", ""),
                    score=final_score,
                    search_tags=normalize_search_tags(c.get("search_tags"), discard_invalid=True),
                    origin_score=c.get("_origin_score"),
                    time_score=c.get("_time_score"),
                )
            )

        results.sort(key=lambda x: x.score, reverse=True)
        return results

    @classmethod
    def _append_level_suffix(cls, uri: str, level: int) -> str:
        """Return user-facing URI with L0/L1 suffix reconstructed by level."""
        suffix = cls.LEVEL_URI_SUFFIX.get(level)
        if not uri or not suffix:
            return uri
        if uri.endswith(f"/{suffix}"):
            return uri
        if uri.endswith("/.abstract.md") or uri.endswith("/.overview.md"):
            return uri
        if uri.endswith("/") and not uri.endswith("://"):
            uri = uri.rstrip("/")
        return f"{uri}/{suffix}"
