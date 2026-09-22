# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
LLM Score Rerank client.

Pointwise relevance scoring with a chat model: each document is scored
independently against the query by a chat completion returning a single
integer 0-100, mapped to a 0.0-1.0 rerank score.

For OpenAI-compatible chat endpoints (e.g. Volcengine Ark plan endpoint)
where no native rerank API is available. Doubao-family models should set
thinking_disabled=true. Quality is below dedicated cross-encoder rerank
models and above vector-only retrieval; absolute scores drift across
queries, so tune `threshold` from logs — start at 0.05-0.1; higher values
silently drop recall.

Same interface as the other rerank clients:
rerank_batch(query, documents) -> List[float]
"""

import json
import math
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import httpx

from openviking.models.rerank.base import RerankBase
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

# "." and "," are deliberately absent: a trailing period is a valid score suffix
# ("85."), so decimals and thousands separators are rejected by _DECIMAL_RE
# instead of by a blanket character ban.
_SCORE_REJECT_CHARS = ("-", "－", "–", "/", "／")
_SCORE_RE = re.compile(r"(?<!\d)(\d{1,3})\s*分?\s*[。.．]?\s*$")
_DECIMAL_RE = re.compile(r"\d\s*[.,，．。]\s*\d")


def _error_body(response: httpx.Response) -> str:
    """Best-effort error body snippet for logs (bounded to 500 chars)."""
    return (getattr(response, "text", "") or "")[:500]


def _is_retryable_status(status: int) -> bool:
    """429 and 5xx are worth retrying; other 4xx fail fast."""
    return status == 429 or 500 <= status < 600


def _should_retry(max_retries: int, attempt: int, status: Optional[int]) -> bool:
    """Retry while attempts remain and the failure looks transient."""
    if attempt >= max_retries:
        return False
    return status is None or _is_retryable_status(status)


def _parse_score(content: str) -> Optional[int]:
    """Parse a 0-100 integer score from model output, or None if unparseable.

    End-anchored and left-bounded by a digit boundary: the final standalone
    integer wins ("100分满分给85" -> 85) while digits glued to other digits are
    rejected ("1085" -> None, previously misread as 85; "1000" -> None,
    previously misread as 0). Decimals, ranges, and thousands separators
    ("0.85", "90-100", "1,000", "85．5", "85。5") are rejected — the old
    first-match regex misread those as 0/90/1/5.
    """
    text = content.strip()
    if not text or any(c in text for c in _SCORE_REJECT_CHARS):
        return None
    if _DECIMAL_RE.search(text):
        return None
    m = _SCORE_RE.search(text)
    if not m:
        return None
    try:
        score = int(m.group(1))
    except ValueError:  # unreachable: \d{1,3} guarantees digits — defensive for the linter
        return None
    return score if 0 <= score <= 100 else None


_PROMPT_SYSTEM = """你是检索相关性评分器。给定查询和候选内容，输出 0-100 的整数相关性分。
评分标准：
90-100 直接回答查询；70-89 高度相关；40-69 部分相关；10-39 弱相关；0-9 不相关。
只输出整数分数，不要输出任何其他文字。"""

_PROMPT_FEWSHOT = [
    (
        "Query: OpenViking 如何配置 embedding\n"
        "Document: 在 ov.conf 的 embedding 段配置 provider、model 和 api_base，支持本地 ollama 与云端服务。",
        "95",
    ),
    (
        "Query: 如何重置登录密码\nDocument: OpenViking 采用 AGPL-3.0 许可证，由火山引擎开源。",
        "3",
    ),
]


def _build_messages(query: str, document: str) -> list:
    messages = [{"role": "system", "content": _PROMPT_SYSTEM}]
    for user_content, score in _PROMPT_FEWSHOT:
        messages.append({"role": "user", "content": user_content + "\n相关性分数:"})
        messages.append({"role": "assistant", "content": score})
    messages.append(
        {"role": "user", "content": f"Query: {query}\nDocument: {document}\n相关性分数:"}
    )
    return messages


class LlmScoreRerankClient(RerankBase):
    """Chat-model pointwise scoring rerank client."""

    def __init__(
        self,
        api_key: str,
        api_base: str,
        model_name: str,
        timeout: float = 30.0,
        concurrency: int = 8,
        thinking_disabled: bool = False,
        log_payloads: bool = False,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout
        self.thinking_disabled = thinking_disabled
        self.log_payloads = log_payloads
        self.max_retries = max(0, max_retries)
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)
        self.provider = "llm_score"
        base = api_base.rstrip("/")
        self.api_url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            # Per-phase timeouts, not a total budget: the wall-clock cut is
            # enforced one level up (RerankConfig.batch_timeout).
            timeout=httpx.Timeout(connect=5.0, read=timeout, write=timeout, pool=5.0),
            # Keep the pool aligned with the worker count. trust_env stays at the
            # httpx default so deployment proxy/CA environment variables apply.
            limits=httpx.Limits(
                max_connections=max(1, concurrency),
                max_keepalive_connections=max(1, concurrency),
            ),
        )
        self._executor = ThreadPoolExecutor(max_workers=max(1, concurrency))

    def _score_one(self, query: str, document: str) -> Optional[tuple]:
        """Score one document; returns (score 0.0-1.0, usage dict), or None on failure.

        Runs on a worker thread: touches no shared state here — token usage is
        aggregated once on the calling thread in ``rerank_batch``
        (JevRerankClient precedent; TokenUsageTracker is not thread-safe).
        """
        body = {
            "model": self.model_name,
            "messages": _build_messages(query, document),
            "temperature": 0,
            "max_tokens": 8,
        }
        if self.thinking_disabled:
            body["thinking"] = {"type": "disabled"}
        try:
            if self.log_payloads:
                logger.warning(
                    "[LlmScoreRerank] Request payload=%s",
                    json.dumps(body, ensure_ascii=False),
                )
            resp = self._post_with_retry(body)
            if resp is None:
                return None
            data = resp.json()
            content = str(data["choices"][0]["message"]["content"]).strip()
            usage = data.get("usage") or {}
            usage_info = {
                "model_name": data.get("model") or self.model_name,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0)
                or self._estimate_tokens(query) + self._estimate_tokens(document),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            }
            score = _parse_score(content)
            if score is None:
                logger.warning(
                    "[LlmScoreRerank] Unparseable or out-of-range score content=%r", content
                )
                return None
            return score / 100.0, usage_info
        except Exception as e:
            logger.error("[LlmScoreRerank] Score failed: %s", e)
            return None

    def _post_with_retry(self, body: dict) -> Optional[httpx.Response]:
        """POST with bounded retry on transient errors (429/5xx/transport).

        A transient failure must not become a fake 0.0 sinking a relevant
        document below irrelevant ones. 4xx (except 429) fails fast.
        """
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(self.api_url, json=body)
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if _should_retry(self.max_retries, attempt, status):
                    self._sleep_before_retry(attempt, e.response)
                    continue
                logger.error(
                    "[LlmScoreRerank] Score failed: HTTP %s body=%s",
                    status,
                    _error_body(e.response),
                )
                return None
            except httpx.TransportError as e:
                if _should_retry(self.max_retries, attempt, None):
                    self._sleep_before_retry(attempt, None)
                    continue
                logger.error("[LlmScoreRerank] Score failed after retries: %s", e)
                return None
        return None  # unreachable: every loop path returns — keeps mypy narrow

    def _sleep_before_retry(self, attempt: int, response: Optional[httpx.Response]) -> None:
        """Backoff before a retry: Retry-After wins for 429, else exponential, always jittered.

        The jitter matters more than the mean delay: this client runs ``concurrency``
        workers that hit the provider limit at the same instant, so a fixed backoff
        makes them retry in lockstep and re-trigger the limiter. Equal jitter keeps
        half the computed delay as a floor, so the retry still backs off.
        """
        retry_after = 0.0
        if response is not None:
            try:
                retry_after = min(float(response.headers.get("retry-after", 0) or 0), 10.0)
            except (TypeError, ValueError):
                retry_after = 0.0
        computed = self.retry_backoff_seconds * (2**attempt)
        # Equal jitter: keep half of our own backoff as a floor and randomize the
        # rest, so workers that failed together do not retry together.
        delay = computed / 2 + random.uniform(0, computed / 2)
        # A server-provided Retry-After is a floor and is never undercut by jitter.
        delay = max(delay, retry_after)
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _record_error(error_code: str) -> None:
        """Emit a rerank error event; metrics must never break rerank execution."""
        try:
            from openviking.metrics.datasources import RerankEventDataSource
            from openviking.observability.context import get_root_observability_context

            root_context = get_root_observability_context()
            RerankEventDataSource.record_error(
                error_code=error_code,
                account_id=root_context.account_id if root_context is not None else None,
            )
        except Exception:
            pass

    def rerank_batch(self, query: str, documents: List[str]) -> Optional[List[float]]:
        """Score documents against a query.

        Per-document failure -> NaN, so the caller keeps that document's vector
        score instead of sinking it to the bottom. All failed -> None so the
        caller falls back to vector scores for the whole batch.
        """
        if not documents:
            return []

        batch_started = time.monotonic()
        futures = [self._executor.submit(self._score_one, query, doc) for doc in documents]
        scores: List[float] = []
        usages: List[dict] = []
        failed = 0
        for fut in futures:
            try:
                result = fut.result()
            except Exception as e:
                logger.error("[LlmScoreRerank] Worker failed: %s", e)
                result = None
            if result is None:
                failed += 1
                # NaN, not 0.0: "no score" must not be indistinguishable from
                # "scored 0". The retriever maps a non-finite score back to the
                # document's vector score, so a transient 429 cannot sink a
                # relevant document below irrelevant ones.
                scores.append(math.nan)
            else:
                score, usage_info = result
                scores.append(score)
                usages.append(usage_info)

        if failed:
            # Failure visibility: per-doc 0.0 is indistinguishable from "truly
            # irrelevant" in scores alone, so failures must surface in metrics.
            # Emitted before the all-failed early return — a provider-wide outage
            # is exactly the case the error metric exists to catch.
            self._record_error("all_failed" if failed == len(documents) else "score_failed")

        if failed == len(documents):
            logger.error(
                "[LlmScoreRerank] All %s documents failed; falling back to vector scores",
                len(documents),
            )
            return None

        if usages:
            # One token-usage update per batch, on the calling thread.
            # duration is batch wall-clock, not the sum of parallel sub-calls.
            self.update_token_usage(
                model_name=usages[0]["model_name"],
                provider=self.provider,
                prompt_tokens=sum(u["prompt_tokens"] for u in usages),
                completion_tokens=sum(u["completion_tokens"] for u in usages),
                duration_seconds=time.monotonic() - batch_started,
            )

        logger.debug("[LlmScoreRerank] Reranked %s documents (failed=%s)", len(documents), failed)
        return scores

    def close(self) -> None:
        """Stop scoring and release the HTTP pool.

        In-flight documents are awaited first: closing the HTTP client underneath
        a running worker turns its request into a per-doc failure (score 0.0),
        which is indistinguishable from "irrelevant" in the ranking.
        """
        self._executor.shutdown(wait=True)
        self._client.close()

    @classmethod
    def from_config(cls, config) -> Optional["LlmScoreRerankClient"]:
        """Create LlmScoreRerankClient from RerankConfig."""
        if not config or not config.is_available():
            return None
        if (
            config.model
            and config.model.lower().startswith("doubao")
            and not config.thinking_disabled
        ):
            logger.warning(
                "[LlmScoreRerank] model=%s with thinking_disabled=false: thinking output "
                "is truncated by max_tokens=8 and may fail every document (silent "
                "fallback to vector scores). Set thinking_disabled=true for Doubao-family "
                "models.",
                config.model,
            )
        return cls(
            api_key=config.api_key,
            api_base=config.api_base,
            model_name=config.model,
            timeout=config.timeout,
            concurrency=config.concurrency,
            thinking_disabled=config.thinking_disabled,
            log_payloads=config.log_payloads,
            max_retries=config.max_retries,
            retry_backoff_seconds=config.retry_backoff_seconds,
        )
