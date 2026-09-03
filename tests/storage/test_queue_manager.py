# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused tests for QueueManager concurrency selection and worker lifecycle."""

import os
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

import openviking.storage.queuefs.queue_manager as qm
from openviking.storage.queuefs.queue_manager import QueueManager


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
