# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused tests for QueueManager concurrency selection and worker lifecycle."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import openviking.storage.queuefs.queue_manager as qm
from openviking.pyagfs import AsyncAGFSClient
from openviking.storage.queuefs.named_queue import DequeueHandlerBase, NamedQueue
from openviking.storage.queuefs.process_result import ProcessResult
from openviking.storage.queuefs.queue_manager import QueueManager
from openviking.storage.queuefs.queue_middleware import QueueMiddleware
from openviking.storage.queuefs.task_work_index import bind_task_context


def test_queuefs_package_imports_in_a_clean_process(tmp_path) -> None:
    env = os.environ.copy()
    env["OPENVIKING_CONFIG_FILE"] = str(tmp_path / "missing-ov.conf")

    subprocess.run(
        [sys.executable, "-c", "from openviking.storage.queuefs import QueueManager"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_queue_concurrency_uses_separate_configured_values() -> None:
    manager = QueueManager(
        agfs=object(),
        max_concurrent_external_parse=9,
        max_concurrent_add_resource=7,
        max_concurrent_session_commit=5,
    )

    assert manager._max_concurrent_for_queue(manager.EXTERNAL_PARSE) == 9
    assert manager._max_concurrent_for_queue(manager.ADD_RESOURCE) == 7
    assert manager._max_concurrent_for_queue(manager.SESSION_COMMIT) == 5



# ===== local: QueueManager worker lifecycle (thread leak / re-init / stop) =====


def test_init_queue_manager_replaces_without_orphaning_threads() -> None:
    """Re-init must stop the previous manager: thread count must not grow.

    Regression for the queuefs worker leak: init_queue_manager() used to
    overwrite the global _instance without stopping the old manager, orphaning
    its worker threads (2000+ leaked threads in tests/server).
    """
    old = qm._instance
    base = threading.active_count()
    try:
        for _ in range(5):
            qm.init_queue_manager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
        qm._instance.setup_standard_queues(vector_store=MagicMock(), start=True)
        current = threading.active_count()
        assert current <= base + 6, (
            f"thread leak on re-init: base={base}, after 5 re-inits={current}"
        )
    finally:
        if qm._instance is not None:
            qm._instance.stop(join_timeout=2.0)
            qm._instance = None
        _ = old  # do not restore: suite owns _instance lifecycle


def test_init_queue_manager_stops_previous_instance() -> None:
    old = qm._instance
    try:
        first = qm.init_queue_manager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
        first.setup_standard_queues(vector_store=MagicMock(), start=True)
        second = qm.init_queue_manager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
        assert second is not first
        assert not first.is_running(), "previous instance must be stopped on re-init"
    finally:
        if qm._instance is not None:
            qm._instance.stop(join_timeout=2.0)
            qm._instance = None
        _ = old


def test_worker_thread_exits_when_agfs_calls_stall() -> None:
    """An unbounded agfs call must not pin the worker thread past stop().

    NamedQueue.size() goes through AsyncAGFSClient.run -> asyncio.to_thread;
    patching read() with a real blocking side_effect reproduces the binding
    stall that made stop() joins time out in tests/server.
    """
    manager = QueueManager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
    stall = threading.Event()
    manager._agfs.read = MagicMock(side_effect=lambda *a, **k: stall.wait(30))
    manager._agfs_call_timeout = 0.5  # bound the agfs call well below join timeout
    manager.get_queue(manager.EMBEDDING, dequeue_handler=MagicMock(), allow_create=True)
    manager.start()
    worker = manager._queue_threads[manager.EMBEDDING]
    assert worker.is_alive()
    try:
        manager.stop(join_timeout=5.0)
        assert not worker.is_alive(), "worker thread must exit despite stalled agfs calls"
    finally:
        stall.set()


def test_stop_unregisters_atexit_callback() -> None:
    manager = QueueManager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
    with patch("atexit.unregister") as spy:
        manager.stop(join_timeout=1.0)
    spy.assert_called_once_with(manager.stop)
    # Idempotent: second stop after unregister must not raise or re-register.
    with patch("atexit.register") as reg:
        manager.stop(join_timeout=1.0)
    reg.assert_not_called()
    assert not manager.is_running()


def test_stop_is_idempotent_and_thread_safe_after_reinit() -> None:
    old = qm._instance
    try:
        first = qm.init_queue_manager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
        first.setup_standard_queues(vector_store=MagicMock(), start=True)
        second = qm.init_queue_manager(agfs=MagicMock(), timeout=1, mount_point="/tmp/ov-qm-test")
        second.setup_standard_queues(vector_store=MagicMock(), start=True)
        # Stopping the OLD instance must not clear the NEW global instance.
        first.stop(join_timeout=2.0)
        assert qm.get_queue_manager() is second, (
            "re-init replacement must survive old-instance stop"
        )
    finally:
        if qm._instance is not None:
            qm._instance.stop(join_timeout=2.0)
            qm._instance = None
        _ = old


# ===== upstream: middleware chain + backend-owned status =====
@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_skill_shutdown_releases_lock_after_embedding_worker_exits(
    transport, monkeypatch, concurrency
):
    from openviking.service.task_tracker_concurrency import run_to_completion
    from openviking.storage.queuefs.semantic_executor import SemanticTreeStats
    from openviking.storage.queuefs.semantic_msg import SemanticMsg
    from openviking.storage.queuefs.semantic_processor import SemanticProcessor
    from openviking.telemetry import OperationTelemetry
    from openviking.telemetry.request_wait_tracker import get_request_wait_tracker

    tracker = get_request_wait_tracker()
    telemetry_id = OperationTelemetry("skill-shutdown").telemetry_id
    tracker.register_request(telemetry_id)
    msg = SemanticMsg(
        uri="viking://agent/skills/demo", context_type="skill", telemetry_id=telemetry_id
    )
    started, queued, release = threading.Event(), threading.Event(), threading.Event()
    events = []

    class Lease:
        lock = {"lease_ref": "demo"}

        async def close(self):
            events.append("lock-released")

    class Dag:
        stale = False

        def __init__(self, **kwargs):
            pass

        async def run(self, uri):
            tracker.register_embedding_root(telemetry_id, "active")
            tracker.register_embedding_root(telemetry_id, "queued")
            queued.set()

        def get_stats(self):
            return SemanticTreeStats()

    async def write(data):
        async def finish():
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            events.append("write-finished")
            tracker.mark_embedding_done(telemetry_id, "active")

        await run_to_completion(finish)
        return ProcessResult.success()

    class Queue:
        def __init__(self, name, handler):
            self.name, self.handler, self.dispatched = name, handler, False

        def has_dequeue_handler(self):
            return True

        async def size(self):
            return int(not self.dispatched)

        async def dequeue_raw(self):
            if self.dispatched:
                return None
            self.dispatched = True
            return {"id": self.name, "data": msg.to_json()}

        async def process_dequeued(self, data):
            return await self.handler(data)

        async def ack(self, *args):
            events.append(f"ack-{self.name}")

        async def dequeue(self):
            data = await self.dequeue_raw()
            await self.process_dequeued(data)
            await self.ack(data)

    monkeypatch.setattr("openviking.storage.queuefs.semantic_processor.SemanticTreeExecutor", Dag)
    monkeypatch.setattr(
        SemanticProcessor, "_resolve_skill_semantic_lock", AsyncMock(return_value=Lease())
    )
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.get_viking_fs",
        lambda: SimpleNamespace(exists=AsyncMock(return_value=True)),
    )
    monkeypatch.setattr(
        "openviking.storage.collection_schemas.TextEmbeddingHandler", lambda _: SimpleNamespace()
    )
    manager = QueueManager(
        object(), max_concurrent_semantic=concurrency, max_concurrent_embedding=concurrency
    )
    manager._poll_interval = 0.001
    manager.setup_standard_queues(object(), start=False)
    semantic = manager._queues[manager.SEMANTIC]._dequeue_handler
    manager._queues = {
        manager.EMBEDDING: Queue(manager.EMBEDDING, write),
        manager.SEMANTIC: Queue(manager.SEMANTIC, semantic.on_dequeue),
    }
    manager.start()
    threads = list(manager._queue_threads.values())
    stopping = None
    try:
        async with asyncio.timeout(2):
            while not (started.is_set() and queued.is_set()):
                await asyncio.sleep(0.01)
        stopping = asyncio.create_task(asyncio.to_thread(manager.stop))
        # Cross the concurrent worker's cancellation deadline while a physical
        # write is still active: the Skill lease must remain held.
        await asyncio.sleep(5.2 if concurrency == 2 else 0.1)
        assert "lock-released" not in events
        assert not manager._embedding_worker_stopped.is_set()
        release.set()
        await asyncio.wait_for(asyncio.shield(stopping), 2)
        assert all(not thread.is_alive() for thread in threads)
        assert events.index("write-finished") < events.index("lock-released")
        assert f"ack-{manager.SEMANTIC}" not in events
        assert not tracker.is_complete(telemetry_id)  # Queued vectors await restart.
    finally:
        release.set()
        if stopping is not None:
            await stopping
        else:
            await asyncio.to_thread(manager.stop)
        tracker.cleanup(telemetry_id)


async def test_status_waits_for_processing_messages_from_other_workers(monkeypatch) -> None:
    client = AsyncMock()

    async def read(path: str):
        if path.endswith("/status"):
            return b'{"pending":0,"processing":1}'
        raise AssertionError(f"unexpected read: {path}")

    client.read.side_effect = read
    monkeypatch.setattr(
        "openviking.storage.queuefs.named_queue.AsyncAGFSClient",
        lambda _: client,
    )

    status = await NamedQueue(object(), "/queue", "Test").get_status()

    assert status.pending == 0
    assert status.in_progress == 1
    assert not status.is_complete


@pytest.fixture
def transport(monkeypatch):
    client = AsyncMock(spec=AsyncAGFSClient)
    client.write.return_value = "message-1"
    client.read.return_value = b"0"
    monkeypatch.setattr(
        "openviking.storage.queuefs.named_queue.AsyncAGFSClient", lambda _client: client
    )
    return client


async def test_constructor_middlewares_apply_to_all_queues(transport):
    calls = []

    class Recorder(QueueMiddleware):
        async def enqueue(self, ctx, call_next):
            calls.append(ctx.queue)
            return await call_next(ctx)

    middlewares = [Recorder()]
    manager = QueueManager(object(), middlewares=middlewares)
    middlewares.clear()
    existing = manager.get_queue("existing", allow_create=True)
    with bind_task_context("task", "account", "user"):
        await existing.enqueue({})
        middlewares.append(Recorder())
        future = manager.get_queue("future", allow_create=True)
        await future.enqueue({})
    assert calls == ["existing", "future"]
    assert manager._task_work_index.has_work("task")
    assert transport.write.await_count == 2


async def test_init_queue_manager_forwards_constructor_middlewares(transport, monkeypatch):
    from openviking.storage.queuefs import queue_manager as queue_module

    calls = []

    class Recorder(QueueMiddleware):
        async def enqueue(self, ctx, call_next):
            calls.append(ctx.queue)
            return await call_next(ctx)

    monkeypatch.setattr(queue_module, "_instance", None)
    manager = queue_module.init_queue_manager(object(), middlewares=[Recorder()])
    assert queue_module.get_queue_manager() is manager
    await manager.get_queue("Test", allow_create=True).enqueue({})
    assert calls == ["Test"]
    transport.write.assert_awaited_once_with("/queue/Test/enqueue", b"{}")


@pytest.mark.parametrize("fail", [False, True])
async def test_concurrent_worker_uses_process_and_ack_middleware(transport, fail):
    events = []
    stop = threading.Event()

    class Recorder(QueueMiddleware):
        async def process(self, ctx, call_next):
            events.append("process")
            return await call_next(ctx)

        async def ack(self, ctx, call_next):
            events.append("ack")
            return await call_next(ctx)

    class Handler(DequeueHandlerBase):
        async def on_dequeue(self, data):
            stop.set()
            if fail:
                raise RuntimeError("worker failure")
            return ProcessResult.success(data)

    manager = QueueManager(object(), middlewares=[Recorder()])
    queue = manager.get_queue("Test", dequeue_handler=Handler(), allow_create=True)
    reads = [b'{"id":"message-1","data":"{}"}']

    async def read(path):
        return reads.pop(0) if reads else b"{}"

    transport.read.side_effect = read
    await manager._worker_async_concurrent(queue, stop, 2)
    assert queue._processed == (not fail)
    assert queue._error_count == fail
    assert events == (["process"] if fail else ["process", "ack"])
    if fail:
        transport.write.assert_not_awaited()
    else:
        transport.write.assert_awaited_once_with("/queue/Test/ack", b"message-1")


async def test_bootstrap_restores_legacy_messages_into_middleware_index(transport):
    manager = QueueManager(object())
    queue = manager.get_queue("Test", allow_create=True)
    message = {
        "id": "legacy-message",
        "data": json.dumps({"task_id": "task", "account_id": "account", "user_id": "user"}),
    }
    transport.read.return_value = json.dumps([message]).encode()
    attached = []

    class Tracker:
        def attach_work_index(self, index):
            attached.append(index)
            assert index.has_work("task")

        async def restore_work_tasks(self, owners):
            assert owners == {"task": ("account", "user")}
            return ["restored"]

    assert await manager.prepare_task_tracking(Tracker()) == ["restored"]
    assert attached == [manager._task_work_index]
    await queue.ack("legacy-message", message)
    assert not attached[0].has_work("task")
    transport.write.assert_awaited_once_with("/queue/Test/ack", b"legacy-message")
