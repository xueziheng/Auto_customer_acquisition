"""真实栈测试夹具的 scheduler 收尾契约。"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from apps.scheduler_worker.main import WorkerRunResult, WorkerStartStatus


def _shutdown_scheduler():
    module = importlib.import_module("tests.e2e.conftest")
    try:
        return module._shutdown_scheduler
    except AttributeError as exc:
        pytest.fail(f"RED：E2E scheduler 安全收尾尚未实现（{exc}）")


@pytest.mark.asyncio
async def test_scheduler_shutdown_accepts_only_started_result() -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        asyncio.sleep(
            0,
            result=WorkerRunResult(WorkerStartStatus.LOCK_LOST, 1),
        )
    )

    with pytest.raises(AssertionError, match="lock_lost"):
        await _shutdown_scheduler()(task, stop, timeout_seconds=1)

    assert stop.is_set()


@pytest.mark.asyncio
async def test_scheduler_shutdown_cancels_task_after_timeout() -> None:
    stop = asyncio.Event()

    async def ignore_stop_event() -> WorkerRunResult:
        await asyncio.Event().wait()
        return WorkerRunResult(WorkerStartStatus.STARTED, 0)

    task = asyncio.create_task(ignore_stop_event())

    with pytest.raises(AssertionError, match="超时"):
        await _shutdown_scheduler()(task, stop, timeout_seconds=0.01)

    assert stop.is_set()
    assert task.cancelled()

