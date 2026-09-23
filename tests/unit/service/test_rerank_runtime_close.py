# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Shared rerank runtime lifecycle: close order must drain in-flight batches first."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from openviking.service.core import OpenVikingService


async def test_close_shuts_down_rerank_executor_before_client():
    """executor.shutdown (waits for in-flight) must precede client.close, in order."""

    order: list[str] = []

    class FakeExecutor:
        def shutdown(self, wait: bool = True) -> None:
            order.append("executor")

    class FakeClient:
        def close(self) -> None:
            order.append("client")

    svc = OpenVikingService.__new__(OpenVikingService)
    # Stubs keep the test off the real ResourceService/config; only close()'s
    # rerank section plus its immediate guards are exercised here.
    svc._resource_service = SimpleNamespace(close_background_tasks=AsyncMock())  # type: ignore[assignment]
    svc._runtime_config_manager = None
    svc._watch_scheduler = None
    svc._session_auto_commit_scheduler = None
    svc._queue_manager = None
    svc._rerank_executor = FakeExecutor()
    svc._rerank_client = FakeClient()
    svc._config = SimpleNamespace(vlm=SimpleNamespace(close=lambda: None))  # type: ignore[assignment]
    svc._vikingdb_manager = None
    svc._agfs_client = None
    svc._release_data_dir_lock = lambda: None  # type: ignore[method-assign]

    await svc.close()

    assert order == ["executor", "client"]
