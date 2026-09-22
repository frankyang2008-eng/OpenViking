# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Hierarchical retriever rerank behavior tests."""

import asyncio
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from openviking.core.context import ContextLevel
from openviking.retrieve.hierarchical_retriever import (
    HierarchicalRetriever,
    RerankBudget,
    RetrieverMode,
)
from openviking.server.identity import RequestContext, Role
from openviking.storage.abstract_overview import render_abstract_overview
from openviking.utils.token_estimation import estimate_text_tokens
from openviking_cli.retrieve.types import ContextType, TypedQuery
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config import RerankConfig, RetrievalConfig


def _result(uri, score, level=2, abstract=None, **extra):
    result = {
        "uri": uri,
        "abstract": abstract if abstract is not None else uri.rsplit("/", 1)[-1],
        "_score": score,
        "level": level,
        "context_type": "resource",
    }
    result.update(extra)
    return result


class DummyEmbedResult:
    def __init__(self) -> None:
        self.dense_vector = [1.0]
        self.sparse_vector = {"hello": 1.0}


class DummyEmbedder:
    def prepare_embedding_input(self, text: str) -> str:
        return text

    def embed(self, _query: str, is_query: bool = False) -> DummyEmbedResult:
        return DummyEmbedResult()

    async def embed_async(self, text: str, is_query: bool = False) -> DummyEmbedResult:
        return self.embed(text, is_query=is_query)


class DummyStorage:
    def __init__(self) -> None:
        self.collection_name = "context"
        self.acl_manager = None
        self.search_calls = []
        self.child_search_calls = []

    async def _acl_enabled(self, ctx: RequestContext) -> bool:
        return self.acl_manager is not None and await self.acl_manager.is_enabled(ctx.account_id)

    async def collection_exists_bound(self) -> bool:
        return True

    async def search_in_tenant(
        self,
        ctx,
        query_vector=None,
        sparse_query_vector=None,
        context_type=None,
        target_directories=None,
        extra_filter=None,
        level=None,
        limit: int = 10,
        offset: int = 0,
    ):
        self.search_calls.append(
            {
                "ctx": ctx,
                "query_vector": query_vector,
                "sparse_query_vector": sparse_query_vector,
                "context_type": context_type,
                "target_directories": target_directories,
                "extra_filter": extra_filter,
                "level": level,
                "limit": limit,
                "offset": offset,
            }
        )
        return [
            _result("viking://resources/root-a", 0.2, level=1, abstract="root A"),
            _result("viking://resources/root-b", 0.8, level=1, abstract="root B"),
        ]

    async def search_children_in_tenant(
        self,
        ctx,
        parent_uri: str,
        query_vector=None,
        sparse_query_vector=None,
        context_type=None,
        target_directories=None,
        extra_filter=None,
        limit: int = 10,
    ):
        self.child_search_calls.append(
            {
                "ctx": ctx,
                "parent_uri": parent_uri,
                "query_vector": query_vector,
                "sparse_query_vector": sparse_query_vector,
                "context_type": context_type,
                "target_directories": target_directories,
                "extra_filter": extra_filter,
                "limit": limit,
            }
        )
        if parent_uri == "viking://resources":
            return [
                _result("viking://resources/file-a", 0.2, abstract="child A", category="doc"),
                _result("viking://resources/file-b", 0.8, abstract="child B", category="doc"),
            ]
        return []


class QuickSearchStorage(DummyStorage):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    async def search_in_tenant(
        self,
        ctx,
        query_vector=None,
        sparse_query_vector=None,
        context_type=None,
        target_directories=None,
        extra_filter=None,
        level=None,
        limit: int = 10,
        offset: int = 0,
    ):
        self.search_calls.append(
            {
                "ctx": ctx,
                "query_vector": query_vector,
                "sparse_query_vector": sparse_query_vector,
                "context_type": context_type,
                "target_directories": target_directories,
                "extra_filter": extra_filter,
                "level": level,
                "limit": limit,
                "offset": offset,
            }
        )
        return [
            dict(result)
            for result in self.results
            if level is None or result.get("level", 2) in level
        ]

    async def search_children_in_tenant(
        self,
        ctx,
        parent_uri: str,
        query_vector=None,
        sparse_query_vector=None,
        context_type=None,
        target_directories=None,
        extra_filter=None,
        limit: int = 10,
    ):
        self.child_search_calls.append(
            {
                "ctx": ctx,
                "parent_uri": parent_uri,
                "query_vector": query_vector,
                "sparse_query_vector": sparse_query_vector,
                "context_type": context_type,
                "target_directories": target_directories,
                "extra_filter": extra_filter,
                "limit": limit,
            }
        )
        return [_result(f"{parent_uri}/should-not-be-returned", 1.0, abstract="child")]


class DirectChildProxy:
    async def search_children_in_tenant(
        self,
        parent_uri: str,
        query_vector=None,
        sparse_query_vector=None,
        context_type=None,
        target_directories=None,
        extra_filter=None,
        limit: int = 10,
    ):
        return [
            _result(f"{parent_uri}/file-a", 0.2, abstract="child A"),
            _result(f"{parent_uri}/file-b", 0.8, abstract="child B"),
        ]


class FakeRerankClient:
    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []
        self._cursor = 0
        # A round scores its directories concurrently, so the fake must not race
        # on the cursor that hands out canned scores.
        self._lock = threading.Lock()

    def rerank_batch(self, query: str, documents: list[str]):
        with self._lock:
            self.calls.append((query, list(documents)))
            start = self._cursor
            end = start + len(documents)
            self._cursor = end
        return list(self.scores[start:end])


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acc1", "user1"), role=Role.USER)


def _query() -> TypedQuery:
    return TypedQuery(query="hello", context_type=ContextType.RESOURCE, intent="")


def _config() -> RerankConfig:
    return RerankConfig(ak="ak", sk="sk", threshold=0.1)


def test_retriever_initializes_rerank_client(monkeypatch):
    fake_client = FakeRerankClient([0.9, 0.1])

    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    storage = DummyStorage()
    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )

    assert retriever._rerank_client is fake_client


def test_rerank_max_input_tokens_accepts_zero_or_at_least_128():
    assert RerankConfig(max_input_tokens=0).max_input_tokens == 0
    with pytest.raises(ValueError, match="max_input_tokens"):
        RerankConfig(max_input_tokens=127)


@pytest.mark.asyncio
async def test_retrieve_uses_rerank_scores_in_thinking_mode(monkeypatch):
    fake_client = FakeRerankClient([0.95, 0.05, 0.11, 0.95])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    storage = DummyStorage()
    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=2, mode=RetrieverMode.THINKING)

    assert [ctx.uri for ctx in result.matched_contexts] == [
        "viking://resources/file-b",
        "viking://resources/file-a",
    ]
    assert fake_client.calls[0] == ("hello", ["root A", "root B"])
    assert fake_client.calls[1] == ("hello", ["child A", "child B"])
    assert storage.search_calls[0]["level"] == [0, 1]


@pytest.mark.asyncio
async def test_rerank_scores_preserves_fallbacks_for_empty_documents(monkeypatch):
    fake_client = FakeRerankClient([0.95, 0.05])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )

    scores = await retriever._rerank_scores(
        "hello",
        ["root A", "", "   ", "root D"],
        [0.2, 0.8, 0.7, 0.4],
    )

    assert scores == [0.95, 0.8, 0.7, 0.05]
    assert fake_client.calls == [("hello", ["root A", "root D"])]


@pytest.mark.asyncio
async def test_rerank_memo_reuses_scores_for_repeated_documents(monkeypatch):
    """The same (query, document) is scored once per request, not once per phase."""
    fake_client = FakeRerankClient([0.95, 0.05, 0.7])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )
    memo: dict = {}

    first = await retriever._rerank_scores("hello", ["doc A", "doc B"], [0.2, 0.3], None, memo)
    # A later phase re-scores doc A; measured, the leaf and directory passes overlap.
    second = await retriever._rerank_scores("hello", ["doc A", "doc C"], [0.2, 0.4], None, memo)

    assert first == [0.95, 0.05]
    assert second == [0.95, 0.7]  # doc A served from the memo, doc C from the provider
    assert fake_client.calls == [("hello", ["doc A", "doc B"]), ("hello", ["doc C"])]


@pytest.mark.asyncio
async def test_rerank_memo_does_not_cache_a_fallback(monkeypatch):
    """A transient failure must not be frozen into every later phase of the request."""

    class FailingThenWorkingClient(FakeRerankClient):
        def rerank_batch(self, query: str, documents: list[str]):
            self.calls.append((query, list(documents)))
            return None if len(self.calls) == 1 else [0.9] * len(documents)

    fake_client = FailingThenWorkingClient([])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )
    memo: dict = {}

    first = await retriever._rerank_scores("hello", ["doc A"], [0.2], None, memo)
    second = await retriever._rerank_scores("hello", ["doc A"], [0.2], None, memo)

    assert first == [0.2]  # provider failed -> vector score
    assert second == [0.9]  # retried instead of reusing a poisoned memo entry
    assert len(fake_client.calls) == 2


@pytest.mark.asyncio
async def test_failed_document_keeps_its_vector_score(monkeypatch):
    """A per-document provider failure (NaN) keeps the vector score, never 0.0."""
    fake_client = FakeRerankClient([0.9, math.nan])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )
    memo: dict = {}

    scores = await retriever._rerank_scores("hello", ["doc A", "doc B"], [0.2, 0.35], None, memo)

    assert scores == [0.9, 0.35]  # doc B keeps its vector score instead of sinking to 0.0
    # Only the real provider score is cached; the failure must stay retryable.
    assert list(memo) == [("hello", "doc A")]


@pytest.mark.asyncio
async def test_rerank_scores_does_not_truncate_by_default(monkeypatch):
    oversized_document = "summary-start " + ("填充内容" * 600) + " relevant-tail"
    fake_client = FakeRerankClient([0.95])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=RerankConfig(ak="ak", sk="sk"),
    )

    await retriever._rerank_scores("query", [oversized_document], [0.2])

    assert retriever.rerank_max_input_tokens == 0
    assert fake_client.calls == [("query", [oversized_document])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "oversized_document",
    [
        "summary-start " + ("filler " * 600) + " relevant-tail",
        "摘要开头" + ("填充内容" * 600) + "相关结论",
    ],
)
async def test_rerank_scores_bounds_oversized_documents_and_preserves_tail(
    monkeypatch, oversized_document
):
    fake_client = FakeRerankClient([0.95])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=RerankConfig(ak="ak", sk="sk", max_input_tokens=128),
    )

    scores = await retriever._rerank_scores("query", [oversized_document], [0.2])

    assert scores == [0.95]
    rerank_query, rerank_documents = fake_client.calls[0]
    bounded_document = rerank_documents[0]
    assert estimate_text_tokens(rerank_query) + estimate_text_tokens(bounded_document) <= 128
    assert "summary-start" in bounded_document or "摘要开头" in bounded_document
    assert "relevant-tail" in bounded_document or "相关结论" in bounded_document
    assert bounded_document != oversized_document


@pytest.mark.asyncio
async def test_retrieve_falls_back_to_vector_scores_when_rerank_returns_none(monkeypatch):
    class NoneRerankClient(FakeRerankClient):
        def rerank_batch(self, query: str, documents: list[str]):
            self.calls.append((query, list(documents)))
            return None

    fake_client = NoneRerankClient([])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )

    storage = QuickSearchStorage([
        _result("viking://resources/a/deep-a.md", 0.2, abstract="deep A"),
        _result("viking://resources/b/deep-b.md", 0.8, abstract="deep B"),
    ])
    async def acl_enabled(_account_id):
        return True

    storage.acl_manager = SimpleNamespace(is_enabled=acl_enabled)

    async def no_hierarchical_children(*_args, **_kwargs):
        return []

    storage.search_children_in_tenant = no_hierarchical_children
    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=2, mode=RetrieverMode.THINKING)

    assert [ctx.uri for ctx in result.matched_contexts] == [
        "viking://resources/b/deep-b.md",
        "viking://resources/a/deep-a.md",
    ]
    assert [call["level"] for call in storage.search_calls] == [[0, 1], [2]]
    assert fake_client.calls


@pytest.mark.asyncio
async def test_rerank_scores_runs_blocking_client_off_event_loop():
    class SlowRerankClient:
        def __init__(self):
            self.thread_id = None

        def rerank_batch(self, query: str, documents: list[str]):
            self.thread_id = threading.get_ident()
            time.sleep(0.2)
            return [0.9 for _ in documents]

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=None,
    )
    fake_client = SlowRerankClient()
    retriever._rerank_client = fake_client

    started = time.monotonic()
    rerank_task = asyncio.create_task(retriever._rerank_scores("hello", ["doc"], [0.1]))

    ticks = 0
    while time.monotonic() - started < 0.15:
        await asyncio.sleep(0.01)
        ticks += 1

    assert await rerank_task == [0.9]
    assert fake_client.thread_id != threading.get_ident()
    assert ticks >= 3


@pytest.mark.asyncio
async def test_round_rerank_batches_run_in_parallel(monkeypatch):
    """One round's directories are scored concurrently, not one after another.

    The barrier only releases when both round batches are in flight at the same
    time; a serial ``await`` per directory leaves the first batch waiting alone
    and breaks the barrier.
    """

    class TwoDirRoundStorage(DummyStorage):
        async def search_in_tenant(
            self,
            ctx,
            query_vector=None,
            sparse_query_vector=None,
            context_type=None,
            target_directories=None,
            extra_filter=None,
            level=None,
            limit: int = 10,
            offset: int = 0,
        ):
            return [
                _result("viking://resources/root-a", 0.2, level=1, abstract="root A"),
                _result("viking://resources/root-b", 0.8, level=1, abstract="root B"),
            ]

        async def search_children_in_tenant(
            self,
            ctx,
            parent_uri: str,
            query_vector=None,
            sparse_query_vector=None,
            context_type=None,
            target_directories=None,
            extra_filter=None,
            limit: int = 10,
        ):
            if parent_uri not in (
                "viking://resources/root-a",
                "viking://resources/root-b",
            ):
                return []
            return [
                _result(f"{parent_uri}/file-a", 0.2, abstract=f"{parent_uri} child A"),
                _result(f"{parent_uri}/file-b", 0.8, abstract=f"{parent_uri} child B"),
            ]

    barrier = threading.Barrier(2, timeout=5)
    serialized: list[int] = []

    class BarrierRerankClient:
        def __init__(self):
            self.calls = []
            self._lock = threading.Lock()

        def rerank_batch(self, query: str, documents: list[str]):
            with self._lock:
                self.calls.append((query, list(documents)))
                index = len(self.calls) - 1
            # Call 0 is the directory pass; calls 1 and 2 are this round's two
            # directory batches, which must overlap.
            if 1 <= index <= 2:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    serialized.append(index)
            return [0.5 for _ in documents]

    fake_client = BarrierRerankClient()
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )
    retriever = HierarchicalRetriever(
        storage=TwoDirRoundStorage(),
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=2, mode=RetrieverMode.THINKING)

    assert serialized == []
    assert len(fake_client.calls) == 3  # directory pass + one batch per round directory
    assert result.matched_contexts


def test_viking_fs_stores_injected_rerank_runtime():
    """VikingFS carries the shared client/executor down to the retriever."""
    from unittest.mock import MagicMock

    from openviking.storage.viking_fs import VikingFS

    fs = VikingFS(agfs=MagicMock(), rerank_client="client", rerank_executor="executor")

    assert fs.rerank_client == "client"
    assert fs.rerank_executor == "executor"


@pytest.mark.asyncio
async def test_injected_rerank_client_is_used_without_from_config(monkeypatch):
    """A shared client is injected, so the retriever must not build its own."""
    injected = FakeRerankClient([0.95, 0.05])

    def _explode(config):
        raise AssertionError("from_config must not be called when a client is injected")

    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config", _explode
    )
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=_config(),
        rerank_client=injected,
    )

    assert retriever._rerank_client is injected
    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=2, mode=RetrieverMode.THINKING)

    assert injected.calls
    assert result.matched_contexts


@pytest.mark.asyncio
async def test_rerank_batch_runs_on_injected_executor():
    """Rerank blocks in the dedicated pool, not the shared asyncio default pool."""
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ov-rerank-test")
    seen: list[str] = []

    class ThreadRecordingClient:
        def rerank_batch(self, query: str, documents: list[str]):
            seen.append(threading.current_thread().name)
            return [0.5 for _ in documents]

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=None,
        rerank_executor=executor,
    )
    retriever._rerank_client = ThreadRecordingClient()
    try:
        scores = await retriever._rerank_scores("q", ["a", "b"], [0.1, 0.2])
    finally:
        executor.shutdown(wait=True)

    assert scores == [0.5, 0.5]
    assert seen == ["ov-rerank-test_0"]


@pytest.mark.asyncio
async def test_batch_timeout_falls_back_to_vector_scores():
    """One slow batch is cut by batch_timeout and keeps the vector score."""

    class SlowClient:
        def rerank_batch(self, query: str, documents: list[str]):
            time.sleep(0.3)
            return [0.9 for _ in documents]

    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=RerankConfig(ak="ak", sk="sk", batch_timeout=0.05),
        rerank_client=SlowClient(),
    )

    started = time.monotonic()
    scores = await retriever._rerank_scores("q", ["a"], [0.42])
    elapsed = time.monotonic() - started

    assert scores == [0.42]
    assert elapsed < 0.25


@pytest.mark.asyncio
async def test_total_budget_skips_remaining_batches():
    """Once the search budget is spent, later batches skip rerank entirely."""

    class CountingClient:
        def __init__(self):
            self.calls = 0

        def rerank_batch(self, query: str, documents: list[str]):
            self.calls += 1
            time.sleep(0.1)
            return [0.9 for _ in documents]

    client = CountingClient()
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=RerankConfig(ak="ak", sk="sk", total_budget=0.05),
        rerank_client=client,
    )
    budget = RerankBudget(0.05)

    first = await retriever._rerank_scores_timed("q", ["a"], [0.1], budget)
    second = await retriever._rerank_scores_timed("q", ["b"], [0.2], budget)

    assert client.calls == 1
    assert first == [0.9]
    assert second == [0.2]


@pytest.mark.asyncio
async def test_zero_budget_disables_the_skip():
    """total_budget=0 keeps the documented 'budget off' behavior."""

    class CountingClient:
        def __init__(self):
            self.calls = 0

        def rerank_batch(self, query: str, documents: list[str]):
            self.calls += 1
            return [0.9 for _ in documents]

    client = CountingClient()
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=DummyEmbedder(),
        rerank_config=RerankConfig(ak="ak", sk="sk", total_budget=0),
        rerank_client=client,
    )
    budget = RerankBudget(0.0)

    await retriever._rerank_scores_timed("q", ["a"], [0.1], budget)
    await retriever._rerank_scores_timed("q", ["b"], [0.2], budget)

    assert client.calls == 2


@pytest.mark.asyncio
async def test_quick_mode_uses_single_vector_search_without_rerank_or_recursion(monkeypatch):
    fake_client = FakeRerankClient([0.05, 0.95, 0.95])
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.RerankClient.from_config",
        lambda config: fake_client,
    )
    storage = QuickSearchStorage(
        [
            _result("viking://resources/root", 0.95, level=0, abstract="root abstract"),
            _result("viking://resources/file", 0.9, abstract="file abstract"),
            _result("viking://resources/dir", 0.85, level=1, abstract="dir overview"),
        ]
    )

    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=_config(),
    )

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=3, mode=RetrieverMode.QUICK)

    assert [ctx.uri for ctx in result.matched_contexts] == [
        "viking://resources/root/.abstract.md",
        "viking://resources/file",
        "viking://resources/dir/.overview.md",
    ]
    assert [ctx.level for ctx in result.matched_contexts] == [0, 2, 1]
    assert [ctx.score for ctx in result.matched_contexts] == [
        pytest.approx(0.95),
        pytest.approx(0.9),
        pytest.approx(0.85),
    ]
    assert len(storage.search_calls) == 1
    assert storage.search_calls[0]["limit"] == retriever.GLOBAL_SEARCH_TOPK
    assert storage.search_calls[0]["extra_filter"] is None
    assert storage.search_calls[0]["level"] is None
    assert storage.child_search_calls == []
    assert fake_client.calls == []


@pytest.mark.asyncio
async def test_quick_mode_pushes_explicit_level_filter_to_vector_search():
    storage = QuickSearchStorage(
        [
            _result("viking://resources/root", 0.99, level=0, abstract="root abstract"),
            _result("viking://resources/dir", 0.98, level=1, abstract="dir overview"),
            _result("viking://resources/file-a", 0.5, abstract="file A"),
            _result("viking://resources/file-b", 0.7, abstract="file B"),
        ]
    )
    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=None,
    )

    result = await retriever.retrieve(
        _query(),
        ctx=_ctx(),
        limit=3,
        mode=RetrieverMode.QUICK,
        scope_dsl={"op": "must", "field": "category", "conds": ["doc"]},
        level=[2],
    )

    assert [ctx.uri for ctx in result.matched_contexts] == [
        "viking://resources/file-b",
        "viking://resources/file-a",
    ]
    assert len(storage.search_calls) == 1
    assert storage.search_calls[0]["limit"] == retriever.GLOBAL_SEARCH_TOPK
    assert storage.search_calls[0]["extra_filter"] == {
        "op": "must",
        "field": "category",
        "conds": ["doc"],
    }
    assert storage.search_calls[0]["level"] == [2]
    assert storage.child_search_calls == []


@pytest.mark.asyncio
async def test_quick_mode_threshold_uses_raw_vector_score():
    storage = QuickSearchStorage(
        [
            _result("viking://resources/high", 0.91, abstract="high"),
            _result("viking://resources/exact", 0.9, abstract="exact"),
        ]
    )
    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=None,
    )

    strict_result = await retriever.retrieve(
        _query(),
        ctx=_ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        score_threshold=0.9,
    )
    inclusive_result = await retriever.retrieve(
        _query(),
        ctx=_ctx(),
        limit=2,
        mode=RetrieverMode.QUICK,
        score_threshold=0.9,
        score_gte=True,
    )

    assert [ctx.uri for ctx in strict_result.matched_contexts] == ["viking://resources/high"]
    assert [ctx.uri for ctx in inclusive_result.matched_contexts] == [
        "viking://resources/high",
        "viking://resources/exact",
    ]


@pytest.mark.asyncio
async def test_quick_mode_keeps_scores_pure_when_hotness_and_propagation_configured(monkeypatch):
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.hotness_score",
        lambda *args, **kwargs: pytest.fail("hotness_score should not be called in QUICK mode"),
    )
    storage = QuickSearchStorage(
        [
            _result(
                "viking://resources/file-a",
                0.8,
                abstract="file A",
                active_count=100,
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]
    )
    retriever = HierarchicalRetriever(
        storage=storage,
        embedder=DummyEmbedder(),
        rerank_config=None,
        retrieval_config=RetrievalConfig(hotness_alpha=0.5, score_propagation_alpha=0.1),
    )

    result = await retriever.retrieve(_query(), ctx=_ctx(), limit=1, mode=RetrieverMode.QUICK)

    assert result.matched_contexts[0].score == pytest.approx(0.8)
    assert storage.child_search_calls == []


@pytest.mark.asyncio
async def test_score_propagation_alpha_uses_configured_weight():
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=None,
        rerank_config=None,
        retrieval_config=RetrievalConfig(score_propagation_alpha=1.0),
    )

    candidates = await retriever._recursive_search(
        vector_proxy=DirectChildProxy(),
        query="hello",
        query_vector=None,
        sparse_query_vector=None,
        starting_points=[("viking://resources", 0.4)],
        limit=1,
        mode=RetrieverMode.QUICK,
    )

    assert candidates[0]["uri"] == "viking://resources/file-b"
    assert candidates[0]["_final_score"] == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_default_retrieval_config_uses_semantic_score_without_hotness(monkeypatch):
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.hotness_score",
        lambda *args, **kwargs: pytest.fail("hotness_score should not be called by default"),
    )
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=None,
        rerank_config=None,
    )

    result = await retriever._convert_to_matched_contexts(
        [_result("viking://resources/file-a", 1.0, abstract="child A")],
        ctx=_ctx(),
    )

    assert result[0].score == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_retrieval_hotness_alpha_blends_when_configured(monkeypatch):
    monkeypatch.setattr(
        "openviking.retrieve.hierarchical_retriever.hotness_score",
        lambda *args, **kwargs: 0.5,
    )
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=None,
        rerank_config=None,
        retrieval_config=RetrievalConfig(hotness_alpha=0.2),
    )

    result = await retriever._convert_to_matched_contexts(
        [_result("viking://resources/file-a", 1.0, abstract="child A")],
        ctx=_ctx(),
    )

    assert result[0].score == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_convert_to_matched_contexts_propagates_search_tags():
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=None,
        rerank_config=None,
    )

    result = await retriever._convert_to_matched_contexts(
        [
            _result(
                "viking://resources/file-a",
                1.0,
                abstract="child A",
                search_tags=["default", "team=infra", "bad=", "project=viking"],
            )
        ],
        ctx=_ctx(),
    )

    assert result[0].search_tags == ["team=infra", "project=viking"]


@pytest.mark.asyncio
async def test_convert_to_matched_contexts_defaults_tags_and_body_previews():
    retriever = HierarchicalRetriever(
        storage=DummyStorage(),
        embedder=None,
        rerank_config=None,
    )
    uri = "viking://resources/demo"
    metadata = {
        "source": {"kind": "http", "uri": "https://example.com/private.pdf"},
        "generated_by": {"component": "SemanticProcessor", "trigger": "ingest"},
    }
    markdown = "---\ntitle: User document\n---\n\nVisible body."

    result = await retriever._convert_to_matched_contexts(
        [
            _result(
                uri,
                1.0,
                level=int(ContextLevel.ABSTRACT),
                abstract=render_abstract_overview(
                    ContextLevel.ABSTRACT, uri, "Visible abstract.", metadata
                ),
            ),
            _result(
                uri,
                0.9,
                level=int(ContextLevel.OVERVIEW),
                abstract=render_abstract_overview(
                    ContextLevel.OVERVIEW, uri, "# Visible overview", metadata
                ),
            ),
            _result("viking://resources/demo.md", 0.8, level=2, abstract=markdown),
            _result(
                "viking://resources/malformed",
                0.7,
                level=int(ContextLevel.ABSTRACT),
                abstract="---\n",
            ),
        ],
        ctx=_ctx(),
    )

    assert [item.search_tags for item in result] == [[], [], [], []]
    assert [item.abstract for item in result] == [
        "Visible abstract.",
        "# Visible overview",
        markdown,
        "",
    ]
