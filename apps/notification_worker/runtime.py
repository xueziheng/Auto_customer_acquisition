"""notification worker 的生产装配、循环与对称清理。"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.gmail.client import GmailConnector, GmailHttpTransport, SecretResolver
from connectors.gmail.transport import GmailApiHttpTransport
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
    StandardAuditLogger,
)
from domains.sending_identity.service import (
    SendingIdentityService,
    SendingIdentityUnitOfWorkFactory,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.repositories.in_app_notifications import (
    PostgresInAppNotificationStore,
)
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.schema import assert_database_schema_current
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from notification_gateway.channels.email import EmailNotificationChannel
from notification_gateway.channels.in_app import InAppChannel
from notification_gateway.jobs import NotificationJobClaim, NotificationJobStore
from notification_gateway.models import (
    Notification,
    NotificationChannel,
    NotificationPriority,
)
from notification_gateway.router import NotificationRouter, RoutingPolicy
from notification_gateway.templates import (
    FixedNotificationTemplateRenderer,
    NotificationRenderer,
)
from shared.errors import (
    PolicyViolation,
    TenantIsolationViolation,
    TradeOSError,
    ValidationError,
)
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.notification_email import (
    MANIFEST as NOTIFICATION_EMAIL_MANIFEST,
)
from tool_gateway.handlers.notification_email import (
    NotificationEmailRateLimitCheck,
    NotificationEmailSendHandler,
    NotificationEmailTenantCheck,
    ToolGatewayTransactionalNotificationSender,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckStage,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import (
    ToolGatewayUnitOfWork,
    ToolGatewayUnitOfWorkFactory,
)

from .config import NotificationWorkerConfig
from .health import NotificationHealthServer, NotificationHealthState

logger = logging.getLogger("apps.notification_worker")


class NotificationRuntimeMode(str, Enum):
    PRODUCTION = "production"
    CONTROLLED_IN_APP = "controlled_in_app"
    LOCAL_IN_APP = "local_in_app"


@dataclass(frozen=True)
class NotificationWorkerRuntime:
    jobs: NotificationJobStore
    router: NotificationRouter
    renderer: NotificationRenderer
    config: NotificationWorkerConfig
    health: NotificationHealthState
    mode: NotificationRuntimeMode = NotificationRuntimeMode.PRODUCTION


class WorkerRunStatus(str, Enum):
    STARTED = "started"


@dataclass(frozen=True)
class WorkerRunResult:
    status: WorkerRunStatus
    cycles_completed: int
    jobs_completed: int


WaitForNextCycle = Callable[[int, asyncio.Event], Awaitable[None]]


class NotificationRoutingPolicy(RoutingPolicy):
    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        names = [channel.name for channel in available]
        if names != ["in_app", "email"] or len(names) != len(set(names)):
            raise PolicyViolation("通知渠道注册表无效")
        if notification.priority in {
            NotificationPriority.URGENT,
            NotificationPriority.NORMAL,
        }:
            return list(available)
        if notification.priority is NotificationPriority.LOW:
            return [available[0]]
        raise PolicyViolation("通知优先级无效")


class ControlledInAppRoutingPolicy(RoutingPolicy):
    """仅专用本机入口：优先级保持，完成只代表显式站内通道。"""

    def channels_for(
        self, notification: Notification, available: list[NotificationChannel]
    ) -> list[NotificationChannel]:
        if [channel.name for channel in available] != ["in_app"]:
            raise PolicyViolation("受控站内渠道注册表无效")
        return list(available)


@dataclass(frozen=True, repr=False)
class _BoundGmailSecretResolver:
    resolver: SecretResolver
    configured_ref: str

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "GMAIL_OAUTH_TOKEN_REF":
            raise ValidationError("Gmail 凭证引用无效")
        return self.resolver.resolve(self.configured_ref)


async def _compose_email_channel(
    config: NotificationWorkerConfig,
    factory: async_sessionmaker,
    transport_factory: Callable[[str], GmailHttpTransport],
    now: Callable[[], datetime],
) -> tuple[EmailNotificationChannel, GmailHttpTransport]:
    email = config.email
    if email is None:
        raise ValidationError("事务通知邮件未配置")
    fingerprint_key = email.secrets.resolve(email.fingerprint_key_ref).encode("utf-8")
    fingerprints = HmacFingerprintProvider(
        email.fingerprint_key_version, fingerprint_key
    )
    transport = transport_factory(email.gmail_base_url)
    try:
        gmail = GmailConnector(transport, now=now)
        await gmail.configure(
            _BoundGmailSecretResolver(
                email.secrets,
                email.gmail_oauth_token_ref,
            )
        )
        sending_uow_factory = cast(
            SendingIdentityUnitOfWorkFactory,
            lambda requested_tenant: SqlAlchemySendingIdentityUnitOfWork(
                factory, requested_tenant, now=now
            ),
        )
        sending: SendingIdentityService = SendingIdentityServiceImpl(
            sending_uow_factory,
            Phase1SendingIdentityAuthorizer(config.tenant_id),
            StandardAuditLogger(),
            now=now,
        )
        actor = SendingIdentityActor(
            "system:notification",
            SendingIdentityScope(
                level=ScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({email.sending_identity_id}),
            ),
            "system",
        )
        handler = NotificationEmailSendHandler(gmail, fingerprints)
        registry = ToolRegistry()
        registry.register(NOTIFICATION_EMAIL_MANIFEST, handler)
        user_id = UserId(new_id("usr"))

        async def authorize(ctx: ToolCallContext, _state: ToolInvocationState) -> bool:
            return (
                ctx.tenant_id == config.tenant_id
                and ctx.user_id == user_id
                and ctx.tool_id == NOTIFICATION_EMAIL_MANIFEST.tool_id
            )

        checks: dict[str, CheckStage] = {
            "tenant": NotificationEmailTenantCheck(config.tenant_id),
            "permission": PermissionCheck(authorize),
            "idempotency": IdempotencyCheck(),
            "rate_limit": NotificationEmailRateLimitCheck(
                handler,
                email.recipients,
                sending,
                email.sending_identity_id,
                actor,
            ),
        }

        def tool_uow(requested_tenant: TenantId) -> ToolGatewayUnitOfWork:
            return cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(factory, requested_tenant, now=now),
            )

        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            checks,
            cast(ToolGatewayUnitOfWorkFactory, tool_uow),
            lease_duration=timedelta(seconds=email.tool_lease_seconds),
            lease_owner=config.lease_owner,
            now=now,
            id_factory=new_id,
        )
        sender = ToolGatewayTransactionalNotificationSender(gateway, handler, user_id)
        return EmailNotificationChannel(sender), transport
    except BaseException:
        close = getattr(transport, "aclose", None)
        if callable(close):
            try:
                await close()
            except BaseException:  # noqa: BLE001 -- cleanup 不得覆盖 composition primary
                logger.error("通知 worker Gmail 资源关闭失败")
        raise


@asynccontextmanager
async def notification_worker_runtime(
    config: NotificationWorkerConfig,
    *,
    transport_factory: Callable[[str], GmailHttpTransport] = GmailApiHttpTransport,
    mode: NotificationRuntimeMode = NotificationRuntimeMode.PRODUCTION,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[NotificationWorkerRuntime]:
    """装配持久 stores、精确双渠道 router 与 health，并对称释放资源。"""
    if not isinstance(config, NotificationWorkerConfig):
        raise TypeError("通知 worker 配置类型无效")
    if not isinstance(mode, NotificationRuntimeMode):
        raise ValidationError("通知运行模式无效")
    health = NotificationHealthState(delivery_mode=mode.value)
    engine = create_engine_from(config.database_url.get_secret_value())
    gmail_transport: GmailHttpTransport | None = None
    server: NotificationHealthServer | None = None
    health_task: asyncio.Task[None] | None = None
    health_started_task: asyncio.Task[None] | None = None
    primary: BaseException | None = None
    health_listening = False
    try:
        if config.email is None and mode is NotificationRuntimeMode.PRODUCTION:
            raise ValidationError("事务通知邮件未配置")
        health.mark_ready("config")
        await assert_database_schema_current(engine)
        health.mark_ready("schema")
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        health.mark_ready("database")
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        jobs = PostgresNotificationJobStore(factory)
        router = NotificationRouter(
            PostgresNotificationDedupStore(factory),
            ControlledInAppRoutingPolicy()
            if mode
            in {
                NotificationRuntimeMode.CONTROLLED_IN_APP,
                NotificationRuntimeMode.LOCAL_IN_APP,
            }
            else NotificationRoutingPolicy(),
        )
        router.register_channel(InAppChannel(PostgresInAppNotificationStore(factory)))
        if mode is NotificationRuntimeMode.PRODUCTION:
            email_channel, gmail_transport = await _compose_email_channel(
                config, factory, transport_factory, now
            )
            router.register_channel(email_channel)
        health.mark_ready("registry")
        server = (
            NotificationHealthServer(health, config.health_port, host="127.0.0.1")
            if mode
            in {
                NotificationRuntimeMode.CONTROLLED_IN_APP,
                NotificationRuntimeMode.LOCAL_IN_APP,
            }
            else NotificationHealthServer(health, config.health_port)
        )
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
        health_listening = True
        yield NotificationWorkerRuntime(
            jobs,
            router,
            FixedNotificationTemplateRenderer(),
            config,
            health,
            mode,
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
            if health_listening and health_task is not None and not health_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(health_task), timeout=5)
                except TimeoutError:
                    pass
                except BaseException as error:  # noqa: BLE001 保留主异常并继续回收owned任务
                    if primary is None and cleanup_error is None:
                        cleanup_error = error
            health_tasks = tuple(
                task for task in (health_started_task, health_task) if task is not None
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
        if gmail_transport is not None:
            close = getattr(gmail_transport, "aclose", None)
            if callable(close):
                try:
                    await close()
                except BaseException as error:  # noqa: BLE001
                    if primary is None and cleanup_error is None:
                        cleanup_error = error
                    else:
                        logger.error("通知 worker Gmail 资源关闭失败")
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
    if (
        runtime.config.email is None
        and runtime.mode is NotificationRuntimeMode.PRODUCTION
    ):
        raise ValidationError("事务通知邮件未配置")
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
