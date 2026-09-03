# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""OwnerLoopDispatcher lifecycle tests: explicit bind transfers ownership."""

import asyncio
import threading

from openviking.service.task_tracker_concurrency import OwnerLoopDispatcher


def _bind_from_thread(
    dispatcher: OwnerLoopDispatcher, loop: asyncio.AbstractEventLoop
) -> asyncio.AbstractEventLoop:
    """Bind on ``loop`` from its own thread (a running loop can't nest)."""
    result: dict = {}

    def _run() -> None:
        async def _do():
            return dispatcher.bind_current_loop()

        try:
            result["loop"] = loop.run_until_complete(_do())
        except BaseException as exc:  # surfaced via assert below
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=10.0)
    assert "error" not in result, result.get("error")
    return result["loop"]


async def test_bind_current_loop_transfers_ownership_from_open_loop() -> None:
    """A second service in one process must not brick the singleton tracker.

    Regression: TaskTracker is a process-level singleton and every
    OpenVikingService.initialize() re-binds it (attach_work_index). The second
    service on a new loop (tests/server running_server runs uvicorn in a
    thread) used to die with "owner event loop is already bound".
    """
    dispatcher = OwnerLoopDispatcher()

    old_owner = asyncio.new_event_loop()
    try:
        bound = _bind_from_thread(dispatcher, old_owner)
        assert bound is old_owner
        # Explicit bind from the new loop transfers ownership even though the
        # old owner loop is still open.
        assert dispatcher.bind_current_loop() is asyncio.get_running_loop()
    finally:
        old_owner.close()


async def test_bind_current_loop_transfers_from_closed_loop() -> None:
    """Same transfer when the previous owner loop is already closed."""
    dispatcher = OwnerLoopDispatcher()

    first_owner = asyncio.new_event_loop()
    try:
        bound = _bind_from_thread(dispatcher, first_owner)
        assert bound is first_owner
    finally:
        first_owner.close()

    assert dispatcher.bind_current_loop() is asyncio.get_running_loop()


async def test_bind_current_loop_is_idempotent_on_same_loop() -> None:
    dispatcher = OwnerLoopDispatcher()
    first = dispatcher.bind_current_loop()
    second = dispatcher.bind_current_loop()
    assert first is second is asyncio.get_running_loop()
