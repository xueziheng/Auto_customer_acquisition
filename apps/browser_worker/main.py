"""Tool Gateway 授权的公开页面读取 worker。

浏览器状态、页面正文和 Trace 只存在于 reader/Artifact Store 边界内；进程队列和
日志只保存任务 ID 与不可变快照元数据。
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import RunId, TenantId

logger = logging.getLogger(__name__)

_EXIT_RUNTIME_NOT_CONFIGURED = 2


class WorkerReadinessError(RuntimeError):
    """schema、浏览器策略或 Artifact Store 注册不完整。"""


@dataclass(frozen=True)
class BrowserJob:
    """由 Tool Gateway 签发的单次公开页面读取任务。"""

    job_id: str
    tenant_id: TenantId
    run_id: RunId
    tool_call_id: str
    url: str
    gateway_authorized: bool

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
        except ValueError:
            raise ValidationError("browser job URL 无效") from None
        if (
            not self.job_id
            or self.job_id != self.job_id.strip()
            or len(self.job_id) > 128
            or not self.tool_call_id
            or self.tool_call_id != self.tool_call_id.strip()
            or len(self.tool_call_id) > 128
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            and not 1 <= port <= 65_535
            or not isinstance(self.gateway_authorized, bool)
        ):
            raise ValidationError("browser job 元数据无效")


@dataclass(frozen=True)
class BrowserSnapshot:
    """公开页面读取后可进入队列/工作流的最小不可变元数据。"""

    canonical_url: str
    observed_at: str
    content_hash: str
    snapshot_artifact_ref: str

    def __post_init__(self) -> None:
        if (
            len(self.content_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.content_hash)
            or not self.snapshot_artifact_ref.startswith("art_")
            or not self.observed_at.endswith("Z")
        ):
            raise ValidationError("browser snapshot 元数据无效")


class BrowserJobRepository(Protocol):
    """durable browser 队列端口。

    实现必须绑定 tenant 和 Tool Gateway 签发状态，并使用
    ``FOR UPDATE SKIP LOCKED`` 有界领取。
    """

    async def assert_schema_current(self) -> None: ...

    async def claim_gateway_reads(self, *, limit: int) -> tuple[BrowserJob, ...]: ...

    async def complete(self, *, job_id: str, snapshot: BrowserSnapshot) -> None: ...

    async def fail(
        self, *, job_id: str, category: str, retryable: bool
    ) -> None: ...


class PublicPageReader(Protocol):
    """固定浏览器策略 + Artifact Store adapter；不得复用跨租户 context。"""

    async def assert_ready(self) -> None: ...

    async def read_public_page(self, job: BrowserJob) -> BrowserSnapshot: ...


@dataclass(frozen=True)
class BrowserWorkerConfig:
    batch_limit: int
    idle_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.batch_limit, bool)
            or not isinstance(self.batch_limit, int)
            or self.batch_limit < 1
        ):
            raise ValidationError("browser worker batch_limit 必须为正整数")
        if (
            isinstance(self.idle_seconds, bool)
            or not isinstance(self.idle_seconds, (int, float))
            or not math.isfinite(self.idle_seconds)
            or self.idle_seconds <= 0
        ):
            raise ValidationError("browser worker idle_seconds 必须为正数")


@dataclass(frozen=True)
class BrowserWorkerRuntime:
    jobs: BrowserJobRepository
    reader: PublicPageReader
    config: BrowserWorkerConfig


@dataclass(frozen=True)
class BrowserWorkerResult:
    jobs_completed: int
    jobs_failed: int


WaitForWork = Callable[[float, asyncio.Event], Awaitable[None]]


class RuntimeFactory(Protocol):
    def __call__(self) -> AbstractAsyncContextManager[BrowserWorkerRuntime]: ...


async def _default_wait(seconds: float, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def _install_stop_signals(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.warning("browser worker 信号处理器不可用", extra={"signal": signum.name})
        else:
            installed.append(signum)

    def cleanup() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return cleanup


def _error_disposition(error: BaseException) -> tuple[str, bool]:
    if isinstance(error, TradeOSError):
        category = "transient" if error.is_retryable else "permanent"
        return category, error.is_retryable
    return "unexpected", False


async def run_browser_worker(
    runtime: BrowserWorkerRuntime,
    *,
    stop_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
    wait: WaitForWork | None = None,
    install_signal_handlers: bool = True,
) -> BrowserWorkerResult:
    """就绪检查后处理公开读取任务，停止信号不取消当前页面快照。"""

    await runtime.jobs.assert_schema_current()
    await runtime.reader.assert_ready()
    stop = stop_event if stop_event is not None else asyncio.Event()
    wait_for_work = wait if wait is not None else _default_wait
    cleanup = _install_stop_signals(stop) if install_signal_handlers else lambda: None
    completed = 0
    failed = 0
    if ready_event is not None:
        ready_event.set()

    try:
        while not stop.is_set():
            jobs = await runtime.jobs.claim_gateway_reads(
                limit=runtime.config.batch_limit
            )
            if not jobs:
                await wait_for_work(runtime.config.idle_seconds, stop)
                continue
            for job in jobs:
                if stop.is_set():
                    break
                if not job.gateway_authorized:
                    await runtime.jobs.fail(
                        job_id=job.job_id,
                        category="gateway_authorization",
                        retryable=False,
                    )
                    failed += 1
                    continue
                try:
                    snapshot = await runtime.reader.read_public_page(job)
                    await runtime.jobs.complete(job_id=job.job_id, snapshot=snapshot)
                    completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - 统一脱敏并标记 job
                    category, retryable = _error_disposition(error)
                    await runtime.jobs.fail(
                        job_id=job.job_id,
                        category=category,
                        retryable=retryable,
                    )
                    failed += 1
                    logger.error(
                        "browser job 执行失败",
                        extra={
                            "job_id": job.job_id,
                            "tool_call_id": job.tool_call_id,
                            "error_category": category,
                        },
                    )
    finally:
        cleanup()
        if ready_event is not None:
            ready_event.clear()
    return BrowserWorkerResult(completed, failed)


async def _run_configured(factory: RuntimeFactory) -> None:
    async with factory() as runtime:
        await run_browser_worker(runtime)


def main(runtime_factory: RuntimeFactory | None = None) -> None:
    """生产入口；缺少完整 composition 时固定失败关闭。"""

    if runtime_factory is None:
        logger.critical("browser worker runtime 未配置")
        raise SystemExit(_EXIT_RUNTIME_NOT_CONFIGURED)
    asyncio.run(_run_configured(runtime_factory))


if __name__ == "__main__":
    main()
