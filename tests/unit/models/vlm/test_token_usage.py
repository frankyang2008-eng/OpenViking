# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Concurrency tests for the shared TokenUsageTracker.

Rerank/embedding clients update this shared singleton from ``asyncio.to_thread``
worker threads while the metrics scrape path reads it from another thread, so
unsynchronized mutation showed up as lost counters and as
``RuntimeError: dictionary changed size during iteration`` inside a swallowed
``except Exception`` (silently dropping the rerank block from /metrics).
"""

import threading

from openviking.models.vlm.token_usage import TokenUsageTracker

_THREADS = 8
_UPDATES_PER_THREAD = 200


def test_concurrent_updates_do_not_lose_tokens():
    tracker = TokenUsageTracker()
    start = threading.Barrier(_THREADS + 1)

    def writer(worker_id: int) -> None:
        start.wait()
        for _ in range(_UPDATES_PER_THREAD):
            tracker.update(f"model-{worker_id % 2}", "llm_score", 10, 1)

    workers = [threading.Thread(target=writer, args=(i,)) for i in range(_THREADS)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join()

    expected_per_model = _THREADS // 2 * _UPDATES_PER_THREAD
    usage = tracker.to_dict()

    assert usage["total_usage"]["prompt_tokens"] == _THREADS * _UPDATES_PER_THREAD * 10
    assert usage["total_usage"]["completion_tokens"] == _THREADS * _UPDATES_PER_THREAD
    assert set(usage["usage_by_model"]) == {"model-0", "model-1"}
    for model_usage in usage["usage_by_model"].values():
        assert model_usage["total_usage"]["prompt_tokens"] == expected_per_model * 10
        assert model_usage["total_usage"]["call_count"] == expected_per_model
        assert model_usage["usage_by_provider"]["llm_score"]["call_count"] == expected_per_model


def test_concurrent_reads_during_key_inserts_do_not_raise():
    """``to_dict`` must not iterate a dict another thread is inserting into."""
    tracker = TokenUsageTracker()
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        index = 0
        while not stop.is_set():
            tracker.update(f"model-{index % 50}", f"provider-{index % 7}", 1, 1)
            index += 1

    def reader() -> None:
        try:
            while not stop.is_set():
                tracker.to_dict()
                tracker.get_total_usage()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    stop.wait(0.4)
    stop.set()
    for thread in threads:
        thread.join()

    assert errors == []


def test_merge_reads_source_trackers_under_lock():
    source = TokenUsageTracker()
    source.update("model-a", "llm_score", 7, 3)
    source.update("model-a", "llm_score", 7, 3)

    merged = TokenUsageTracker.merge(source)

    usage = merged.to_dict()
    assert usage["total_usage"]["prompt_tokens"] == 14
    # merge() carries the source's provider-level call_count over (its documented
    # behavior); the merged total_usage.call_count stays at one per provider row.
    assert usage["usage_by_model"]["model-a"]["usage_by_provider"]["llm_score"]["call_count"] == 2
    assert usage["usage_by_model"]["model-a"]["total_usage"]["call_count"] == 1
