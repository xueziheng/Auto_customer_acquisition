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


@pytest.mark.asyncio
async def test_scheduler_shutdown_bounds_cancel_drain_when_task_delays_exit() -> None:
    """吞掉首次取消的任务不能无限阻塞夹具后续资源清理。"""
    stop = asyncio.Event()
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def delay_after_first_cancellation() -> WorkerRunResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
        return WorkerRunResult(WorkerStartStatus.STARTED, 0)

    worker_task = asyncio.create_task(delay_after_first_cancellation())
    shutdown_task = asyncio.create_task(
        _shutdown_scheduler()(
            worker_task,
            stop,
            timeout_seconds=0.01,
            cancel_grace_seconds=0.01,
        )
    )
    try:
        done, _pending = await asyncio.wait({shutdown_task}, timeout=0.1)
        assert shutdown_task in done, "scheduler 取消 drain 仍会无界等待"
        with pytest.raises(AssertionError, match="取消等待超时"):
            await shutdown_task
        assert cancellation_seen.is_set()
    finally:
        release.set()
        if not worker_task.done():
            worker_task.cancel()
        if not shutdown_task.done():
            shutdown_task.cancel()
        await asyncio.gather(worker_task, shutdown_task, return_exceptions=True)
