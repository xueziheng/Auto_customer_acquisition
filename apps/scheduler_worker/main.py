"""单副本 scheduler worker：安全锁定后驱动 outbox 与 workflow。

本模块只负责进程编排，不构造领域服务或 handler。完整注册的 runtime 由
``runtime`` composition 注入；零参数 ``main`` 仍明确失败关闭，避免未提供环境与
密钥解析器时启动 partial registry。
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import TenantId
from workflows.quote_approval.expiry import QuoteExpiryDriver

logger = logging.getLogger(__name__)

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_EXIT_RUNTIME_NOT_CONFIGURED = 2
_EXIT_LOCK_NOT_ACQUIRED = 3
_EXIT_LOCK_LOST = 4


class OutboxDrainer(Protocol):
    """已完整注册 handler 的 durable outbox 投递器。"""

    async def drain(self) -> int: ...


class WorkflowPoller(Protocol):
    """已完整注册 definition/step handler 的 durable workflow 引擎。"""

    async def poll_due(self, tenant_id: TenantId, limit: int) -> int: ...


class CampaignDriver(Protocol):
    """到期序列推进驱动：扫描到期 Enrollment 起 run，取消失效 run。"""

    async def scan_once(self) -> int: ...


class SourcingAdmissionDriverProtocol(Protocol):
    """老板策略门禁后的 durable 寻源准入驱动。"""

    async def scan_once(self) -> object: ...


class RuntimeActivation(Protocol):
    """仅由已持有并确认 dedicated scheduler 锁的副本执行的启动事实。"""

    async def activate(self) -> None: ...


@dataclass(frozen=True)
class SchedulerConfig:
    """调度参数；值由 composition root 明确注入，不在业务循环里猜默认值。"""

    interval_seconds: float
    batch_limit: int
    lock_key: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (int, float))
            or not math.isfinite(self.interval_seconds)
            or self.interval_seconds <= 0
        ):
            raise ValidationError("scheduler interval 必须为正数")
        if (
            isinstance(self.batch_limit, bool)
            or not isinstance(self.batch_limit, int)
            or self.batch_limit < 1
        ):
            raise ValidationError("scheduler batch_limit 必须为正整数")
        if (
            isinstance(self.lock_key, bool)
            or not isinstance(self.lock_key, int)
            or not _INT64_MIN <= self.lock_key <= _INT64_MAX
        ):
            raise ValidationError("scheduler lock_key 必须是合法 int64")


@dataclass(frozen=True)
class SchedulerRuntime:
    """已装配 runtime；lock connection、业务 handler 与 actor 均由外部注入。"""

    lock_engine: AsyncEngine
    outbox: OutboxDrainer
    workflow: WorkflowPoller
    tenant_id: TenantId
    config: SchedulerConfig
    campaign_driver: CampaignDriver | None = None
    activation: RuntimeActivation | None = None
    quote_expiry_driver: QuoteExpiryDriver | None = None
    sourcing_admission_driver: SourcingAdmissionDriverProtocol | None = None

    def __post_init__(self) -> None:
        if not str(self.tenant_id).strip():
            raise ValidationError("scheduler tenant_id 不得为空")
        if self.activation is not None and not callable(
            getattr(self.activation, "activate", None)
        ):
            raise ValidationError("scheduler runtime activation 无效")
        if self.sourcing_admission_driver is not None and not callable(
            getattr(self.sourcing_admission_driver, "scan_once", None)
        ):
            raise ValidationError("scheduler sourcing admission driver 无效")


class RuntimeFactory(Protocol):
    """生产 composition 的窄入口；上下文负责 runtime 资源的创建与释放。"""

    def __call__(self) -> AbstractAsyncContextManager[SchedulerRuntime]: ...


class WorkerStartStatus(str, Enum):
    """worker 是否真正取得单副本锁并进入调度生命周期。"""

    STARTED = "started"
    NOT_STARTED = "not_started"
    LOCK_LOST = "lock_lost"


@dataclass(frozen=True)
class WorkerRunResult:
    """一次 worker 生命周期的可观测结果。"""

    status: WorkerStartStatus
    cycles_completed: int


WaitForNextCycle = Callable[[float, asyncio.Event], Awaitable[None]]
ConfirmSchedulerLock = Callable[[], Awaitable[None]]


class _SchedulerLockLost(RuntimeError):
    """阶段边界失去原专用连接；不得当作普通业务phase异常吞掉。"""


def _error_category(error: BaseException) -> str:
    """按调用方处置方式分类；不读取或记录异常消息/context。"""
    if isinstance(error, TradeOSError):
        return "transient" if error.is_retryable else "permanent"
    return "unexpected"


def _log_phase_error(
    *, phase: str, error: BaseException, tenant_id: TenantId, cycle: int
) -> None:
    """只记录固定消息与安全维度，禁止异常原文、context 或 traceback。"""
    logger.error(
        "scheduler 阶段失败",
        extra={
            "scheduler_phase": phase,
            "error_category": _error_category(error),
            "tenant_id": str(tenant_id),
            "cycle": cycle,
        },
    )


async def _default_wait(interval: float, stop_event: asyncio.Event) -> None:
    """等待下轮或 stop；stop 只改变循环边界，不取消正在执行的 phase。"""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval)
    except TimeoutError:
        return


def _install_stop_signals(stop_event: asyncio.Event) -> Callable[[], None]:
    """把 SIGINT/SIGTERM 映射为 stop flag，并返回对称清理函数。"""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.warning(
                "scheduler 信号处理器不可用",
                extra={"signal_name": signum.name},
            )
        else:
            installed.append(signum)

    def cleanup() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return cleanup


async def _run_cycle(
    runtime: SchedulerRuntime,
    cycle: int,
    *,
    confirm_lock: ConfirmSchedulerLock | None = None,
) -> None:
    """执行一个固定顺序 cycle；各 phase 隔离且不跨 phase 回滚。

    顺序：outbox 前置投递 → Campaign 到期扫描 → 报价到期 → 寻源准入 →
    workflow 推进 → 有推进时 outbox 后置投递。
    """
    if (
        runtime.quote_expiry_driver is not None
        or runtime.sourcing_admission_driver is not None
    ) and confirm_lock is None:
        raise RuntimeError("单副本阶段扫描缺少 scheduler 锁确认")
    pre_count = 0
    campaign_count = 0
    workflow_count = 0
    post_count = 0

    try:
        pre_count = await runtime.outbox.drain()
    except Exception as error:  # noqa: BLE001 - phase 必须隔离并统一脱敏
        _log_phase_error(
            phase="outbox_pre",
            error=error,
            tenant_id=runtime.tenant_id,
            cycle=cycle,
        )

    if runtime.campaign_driver is not None:
        try:
            campaign_count = await runtime.campaign_driver.scan_once()
        except Exception as error:  # noqa: BLE001 - phase 必须隔离并统一脱敏
            _log_phase_error(
                phase="campaign",
                error=error,
                tenant_id=runtime.tenant_id,
                cycle=cycle,
            )

    if runtime.quote_expiry_driver is not None:
        assert confirm_lock is not None
        await confirm_lock()
        try:
            await runtime.quote_expiry_driver.scan_once()
        except Exception as error:  # noqa: BLE001 - phase独立失败、固定类型日志
            _log_phase_error(
                phase="quote_expiry",
                error=error,
                tenant_id=runtime.tenant_id,
                cycle=cycle,
            )

    if runtime.sourcing_admission_driver is not None:
        assert confirm_lock is not None
        await confirm_lock()
        try:
            await runtime.sourcing_admission_driver.scan_once()
        except Exception as error:  # noqa: BLE001 - phase独立失败、固定类型日志
            _log_phase_error(
                phase="sourcing_admission",
                error=error,
                tenant_id=runtime.tenant_id,
                cycle=cycle,
            )

    workflow_succeeded = False
    try:
        workflow_count = await runtime.workflow.poll_due(
            runtime.tenant_id, runtime.config.batch_limit
        )
        workflow_succeeded = True
    except Exception as error:  # noqa: BLE001 - phase 必须隔离并统一脱敏
        _log_phase_error(
            phase="workflow",
            error=error,
            tenant_id=runtime.tenant_id,
            cycle=cycle,
        )

    if workflow_succeeded and workflow_count > 0:
        try:
            post_count = await runtime.outbox.drain()
        except Exception as error:  # noqa: BLE001 - phase 必须隔离并统一脱敏
            _log_phase_error(
                phase="outbox_post",
                error=error,
                tenant_id=runtime.tenant_id,
                cycle=cycle,
            )

    logger.info(
        "scheduler 周期完成",
        extra={
            "tenant_id": str(runtime.tenant_id),
            "cycle": cycle,
            "outbox_pre_count": pre_count,
            "campaign_count": campaign_count,
            "workflow_count": workflow_count,
            "outbox_post_count": post_count,
        },
    )


async def _same_lock_backend(connection: AsyncConnection, expected_pid: int) -> bool:
    """确认 dedicated physical connection 未变化，并结束 heartbeat 事务。"""
    try:
        current_pid = (
            await connection.execute(text("SELECT pg_backend_pid()"))
        ).scalar_one()
        await connection.commit()
    except Exception:  # noqa: BLE001 - 锁存活检查必须失败关闭
        try:
            await connection.rollback()
        except Exception:  # noqa: BLE001, S110 - close 仍会释放/清理
            pass
        return False
    return int(current_pid) == expected_pid


async def run_scheduler_worker(
    runtime: SchedulerRuntime,
    *,
    stop_event: asyncio.Event | None = None,
    wait: WaitForNextCycle | None = None,
    install_signal_handlers: bool = True,
) -> WorkerRunResult:
    """取得 dedicated advisory lock 后运行循环；所有退出路径对称解锁。

    ``runtime.outbox`` 必须已注册全部订阅 handler，``runtime.workflow`` 必须已
    注册全部流程 definition/step handler。函数不会补空 registry，也不会从 event
    payload 构造 actor。``CancelledError`` 不按普通 phase 错误处理，会向上传播，
    但仍经过 ``finally`` 在同一 connection 解锁。
    """
    stop = stop_event if stop_event is not None else asyncio.Event()
    wait_next = wait if wait is not None else _default_wait
    lock_key = runtime.config.lock_key

    async with runtime.lock_engine.connect() as connection:
        lock_row = (
            await connection.execute(
                text(
                    "SELECT pg_try_advisory_lock(:lock_key) AS acquired, "
                    "pg_backend_pid() AS backend_pid"
                ),
                {"lock_key": lock_key},
            )
        ).one()
        await connection.commit()
        if lock_row.acquired is not True:
            logger.warning(
                "scheduler worker 未获得单副本锁",
                extra={"tenant_id": str(runtime.tenant_id)},
            )
            return WorkerRunResult(WorkerStartStatus.NOT_STARTED, 0)
        lock_backend_pid = int(lock_row.backend_pid)

        cleanup_signals = (
            _install_stop_signals(stop) if install_signal_handlers else lambda: None
        )
        cycles = 0
        lock_owned = True
        activation_pending = runtime.activation is not None

        async def confirm_lock() -> None:
            if not await _same_lock_backend(connection, lock_backend_pid):
                raise _SchedulerLockLost()

        try:
            while not stop.is_set():
                if not await _same_lock_backend(connection, lock_backend_pid):
                    lock_owned = False
                    logger.error(
                        "scheduler worker 单副本锁已丢失",
                        extra={"tenant_id": str(runtime.tenant_id)},
                    )
                    return WorkerRunResult(WorkerStartStatus.LOCK_LOST, cycles)
                if stop.is_set():
                    break
                if activation_pending:
                    assert runtime.activation is not None
                    await runtime.activation.activate()
                    activation_pending = False
                    await confirm_lock()
                await _run_cycle(runtime, cycles + 1, confirm_lock=confirm_lock)
                cycles += 1
                if stop.is_set():
                    break
                await wait_next(runtime.config.interval_seconds, stop)
            return WorkerRunResult(WorkerStartStatus.STARTED, cycles)
        except _SchedulerLockLost:
            lock_owned = False
            logger.error(
                "scheduler worker 单副本锁已丢失",
                extra={"tenant_id": str(runtime.tenant_id)},
            )
            return WorkerRunResult(WorkerStartStatus.LOCK_LOST, cycles)
        finally:
            cleanup_signals()
            if lock_owned:
                try:
                    released = (
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": lock_key},
                        )
                    ).scalar_one()
                    await connection.commit()
                except Exception:  # noqa: BLE001 - close 仍须作为解锁兜底
                    logger.error(
                        "scheduler worker 单副本锁释放失败",
                        extra={"tenant_id": str(runtime.tenant_id)},
                    )
                else:
                    if released is not True:
                        logger.error(
                            "scheduler worker 单副本锁释放未确认",
                            extra={"tenant_id": str(runtime.tenant_id)},
                        )


async def _run_from_factory(runtime_factory: RuntimeFactory) -> int:
    """在 factory 资源上下文内运行，并把未获锁映射为明确非零退出。"""
    async with runtime_factory() as runtime:
        result = await run_scheduler_worker(runtime)
    if result.status is WorkerStartStatus.NOT_STARTED:
        return _EXIT_LOCK_NOT_ACQUIRED
    if result.status is WorkerStartStatus.LOCK_LOST:
        return _EXIT_LOCK_LOST
    return 0


def main(runtime_factory: RuntimeFactory | None = None) -> int:
    """同步进程入口；缺少真实 composition 时固定消息、非零失败关闭。"""
    if runtime_factory is None:
        logger.error("scheduler worker runtime 未配置")
        return _EXIT_RUNTIME_NOT_CONFIGURED
    try:
        return asyncio.run(_run_from_factory(runtime_factory))
    except Exception:  # noqa: BLE001 - 进程边界固定消息、非零退出
        logger.error("scheduler worker 启动失败")
        return _EXIT_RUNTIME_NOT_CONFIGURED


if __name__ == "__main__":
    raise SystemExit(main())
