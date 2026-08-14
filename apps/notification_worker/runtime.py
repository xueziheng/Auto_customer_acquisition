"""notification worker 的生产装配、循环与对称清理。"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.repositories.in_app_notifications import (
    PostgresInAppNotificationStore,
)
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from notification_gateway.channels.in_app import InAppChannel
from notification_gateway.jobs import NotificationJobClaim, NotificationJobStore
from notification_gateway.models import Notification, NotificationChannel
from notification_gateway.router import NotificationRouter, RoutingPolicy
from notification_gateway.templates import (
    FixedNotificationTemplateRenderer,
    NotificationRenderer,
)
from shared.errors import PolicyViolation, TenantIsolationViolation, TradeOSError

from .config import NotificationWorkerConfig
from .health import NotificationHealthServer, NotificationHealthState

logger = logging.getLogger("apps.notification_worker")


@dataclass(frozen=True)
class NotificationWorkerRuntime:
    jobs: NotificationJobStore
    router: NotificationRouter
    renderer: NotificationRenderer
    config: NotificationWorkerConfig
    health: NotificationHealthState


class WorkerRunStatus(str, Enum):
    STARTED = "started"


@dataclass(frozen=True)
class WorkerRunResult:
    status: WorkerRunStatus
    cycles_completed: int
    jobs_completed: int


WaitForNextCycle = Callable[[int, asyncio.Event], Awaitable[None]]


class _InAppOnlyPolicy(RoutingPolicy):
    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        del notification
        if len(available) != 1 or available[0].name != "in_app":
            raise PolicyViolation("通知渠道注册表无效")
        return list(available)


@asynccontextmanager
async def notification_worker_runtime(
    config: NotificationWorkerConfig,
) -> AsyncIterator[NotificationWorkerRuntime]:
    """装配 Postgres stores、in-app router 与 health，并对称释放资源。"""
    if not isinstance(config, NotificationWorkerConfig):
        raise TypeError("通知 worker 配置类型无效")
    health = NotificationHealthState()
    health.mark_ready("config")
    engine = create_engine_from(config.database_url.get_secret_value())
    server: NotificationHealthServer | None = None
    health_task: asyncio.Task[None] | None = None
    health_started_task: asyncio.Task[None] | None = None
    primary: BaseException | None = None
    try:
        await assert_database_schema_current(engine)
        health.mark_ready("schema")
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        health.mark_ready("database")
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        jobs = PostgresNotificationJobStore(factory)
        router = NotificationRouter(PostgresNotificationDedupStore(factory), _InAppOnlyPolicy())
        router.register_channel(InAppChannel(PostgresInAppNotificationStore(factory)))
        health.mark_ready("registry")
        server = NotificationHealthServer(health, config.health_port)
        health_task = asyncio.create_task(server.serve())
        health_started_task = asyncio.create_task(server.wait_started())
        done, _pending = await asyncio.wait(
            (health_task, health_started_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if health_task in done:
            await health_task
            raise RuntimeError("notification health exited before listening")
        await health_started_task
        if health_task.done():
            await health_task
            raise RuntimeError("notification health exited before listening")
        yield NotificationWorkerRuntime(
            jobs,
            router,
            FixedNotificationTemplateRenderer(),
            config,
            health,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        if server is not None:
            try:
                await server.close()
            except BaseException as error:  # noqa: BLE001
                if primary is None:
                    cleanup_error = error
                else:
                    logger.error("通知 worker health 资源关闭失败")
            health_tasks = tuple(
                task
                for task in (health_started_task, health_task)
                if task is not None
            )
            for task in health_tasks:
                if not task.done():
                    task.cancel()
            for task in health_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except BaseException as error:  # noqa: BLE001
                    if primary is None and cleanup_error is None:
                        cleanup_error = error
                    else:
                        logger.error("通知 worker health 任务关闭失败")
        try:
            await engine.dispose()
        except BaseException as error:  # noqa: BLE001
            if primary is None and cleanup_error is None:
                cleanup_error = error
            else:
                logger.error("通知 worker 数据库资源关闭失败")
        if cleanup_error is not None:
            raise cleanup_error


async def _default_wait(seconds: int, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def install_stop_signals(stop_event: asyncio.Event) -> Callable[[], None]:
    """SIGINT/SIGTERM 只设置 stop flag，返回对称清理函数。"""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.warning(
                "通知 worker 信号处理器不可用",
                extra={"signal_name": signum.name},
            )
        else:
            installed.append(signum)

    def cleanup() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return cleanup


def _error_category(error: BaseException) -> str:
    if isinstance(error, PolicyViolation):
        return "policy"
    if isinstance(error, TradeOSError):
        return "transient" if error.is_retryable else "permanent"
    return "unexpected"


def _log_claim_failure(
    claim: NotificationJobClaim, error: BaseException, *, phase: str
) -> None:
    logger.error(
        "通知 worker 任务失败",
        extra={
            "tenant_id": str(claim.tenant_id),
            "notification_job_id": str(claim.job_id),
            "worker_phase": phase,
            "error_category": _error_category(error),
            "error_type": type(error).__name__,
            "attempt": claim.attempt_count,
        },
    )


async def run_notification_worker(
    runtime: NotificationWorkerRuntime,
    *,
    stop_event: asyncio.Event | None = None,
    wait: WaitForNextCycle | None = None,
) -> WorkerRunResult:
    """逐周期认领；每条 claim 独立 render/dispatch/持久化结果。"""
    stop = stop_event if stop_event is not None else asyncio.Event()
    wait_next = wait if wait is not None else _default_wait
    cleanup_signals = install_stop_signals(stop)
    cycles = 0
    completed = 0
    try:
        while not stop.is_set():
            claims = await runtime.jobs.claim_due(
                runtime.config.tenant_id,
                limit=runtime.config.batch_limit,
                lease_owner=runtime.config.lease_owner,
            )
            for claim in claims:
                try:
                    try:
                        if claim.tenant_id != runtime.config.tenant_id:
                            raise TenantIsolationViolation("跨租户通知任务被拒绝")
                        notification = runtime.renderer.render(claim)
                        await runtime.router.dispatch(notification)
                    except TradeOSError as error:
                        _log_claim_failure(claim, error, phase="dispatch")
                        if error.is_retryable:
                            await runtime.jobs.retry(
                                runtime.config.tenant_id,
                                claim.job_id,
                                claim_token=claim.claim_token,
                                error=error,
                            )
                        else:
                            await runtime.jobs.reject(
                                runtime.config.tenant_id,
                                claim.job_id,
                                claim_token=claim.claim_token,
                                error=error,
                            )
                    except Exception as error:  # noqa: BLE001
                        # 未分类异常可能来自暂态基础设施；保留 retry 并由测试锁定。
                        _log_claim_failure(claim, error, phase="dispatch")
                        await runtime.jobs.retry(
                            runtime.config.tenant_id,
                            claim.job_id,
                            claim_token=claim.claim_token,
                            error=error,
                        )
                    else:
                        persisted = await runtime.jobs.complete(
                            runtime.config.tenant_id,
                            claim.job_id,
                            claim_token=claim.claim_token,
                        )
                        if persisted:
                            completed += 1
                except Exception as error:  # noqa: BLE001
                    runtime.health.mark_degraded()
                    _log_claim_failure(claim, error, phase="persistence")
            cycles += 1
            if stop.is_set():
                break
            await wait_next(runtime.config.poll_interval_seconds, stop)
        return WorkerRunResult(WorkerRunStatus.STARTED, cycles, completed)
    finally:
        cleanup_signals()
