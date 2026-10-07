"""邮件反馈 worker 的单副本循环、退避与安全可观测性。"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from connectors.gmail.client import GmailConnector, GmailHttpTransport, SecretResolver
from connectors.gmail.transport import GmailApiHttpTransport
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
    Phase1OutreachAuthorizer,
    StandardAuditLogger,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.service import OutreachService
from domains.outreach.service_impl import OutreachServiceImpl
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    Phase1SendingIdentityAuthorizer,
    SendingIdentityScope,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.service import SendingIdentityService
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.advisory_lock import (
    PostgresAdvisoryLock,
    derive_advisory_lock_key,
)
from infra.db.email_feedback_uow import (
    AuditSink,
    OutreachServiceBuilder,
    SendingIdentityServiceBuilder,
    SqlAlchemyFeedbackPageUnitOfWork,
)
from infra.db.repositories.email_feedback import FeedbackCursorRepositoryImpl
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.secrets import EnvironmentSecretResolver
from shared.errors import TradeOSError, ValidationError
from shared.schemas.email_feedback import EmailFeedbackKind, EmailFeedbackResult
from shared.schemas.identifiers import SendingIdentityId, TenantId, UserId, new_id
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_feedback import (
    MANIFEST as FEEDBACK_MANIFEST,
)
from tool_gateway.handlers.email_feedback import (
    EmailFeedbackFetchHandler,
    FeedbackPageSlot,
    ToolEmailFeedbackReader,
    ToolGatewayEmailFeedbackReader,
    _GmailProviderEmailFeedbackReader,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    CheckStage,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import (
    ToolGatewayUnitOfWork,
    ToolGatewayUnitOfWorkFactory,
)
from workflows.email_feedback.flow import (
    FeedbackPageProcessor,
    FeedbackPageResult,
    FeedbackPageUnitOfWorkFactory,
)
from workflows.email_feedback.repository import FeedbackPageUnitOfWork

from .config import EmailFeedbackWorkerConfig, EmailFeedbackWorkerSettings
from .health import EmailFeedbackHealthServer, EmailFeedbackHealthState

logger = logging.getLogger("apps.email_feedback_worker")
_BACKOFF_SECONDS = (5, 10, 20, 40, 80, 160, 300)


class WorkerRunStatus(str, Enum):
    STARTED = "started"
    DISABLED = "disabled"
    LOCK_NOT_ACQUIRED = "lock_not_acquired"
    LOCK_LOST = "lock_lost"


@dataclass(frozen=True)
class WorkerRunResult:
    status: WorkerRunStatus
    cycles_completed: int


@dataclass(frozen=True)
class EmailFeedbackRuntime:
    lock_engine: AsyncEngine
    reader: ToolEmailFeedbackReader
    processor: FeedbackPageProcessor
    config: EmailFeedbackWorkerConfig
    health: EmailFeedbackHealthState


class EmailFeedbackMetricName(str, Enum):
    CURSOR_LAG = "cursor_lag"
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    QUARANTINED = "quarantined"
    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    COMPLAINT = "complaint"
    PAGE_ROLLBACK = "page_rollback"
    CONSECUTIVE_FAILURE = "consecutive_failure"


class EmailFeedbackMetrics(Protocol):
    def record(
        self,
        name: EmailFeedbackMetricName,
        *,
        tenant_id: TenantId,
        mailbox_alias: str,
        value: int = 1,
        kind: EmailFeedbackKind | None = None,
        result: EmailFeedbackResult | None = None,
    ) -> None: ...


class FeedbackCursorReader(Protocol):
    async def get_cursor(
        self, tenant_id: TenantId, mailbox_alias: str
    ) -> str | None: ...


class HealthServer(Protocol):
    async def serve(self) -> None: ...

    async def close(self) -> None: ...


class AdvisoryLock(Protocol):
    async def acquire(self) -> bool: ...

    async def heartbeat(self) -> bool: ...

    async def close(self) -> None: ...


class SafeLogMetrics:
    """默认低基数 metrics sink；不接受自由 label。"""

    def record(
        self,
        name: EmailFeedbackMetricName,
        *,
        tenant_id: TenantId,
        mailbox_alias: str,
        value: int = 1,
        kind: EmailFeedbackKind | None = None,
        result: EmailFeedbackResult | None = None,
    ) -> None:
        if not isinstance(name, EmailFeedbackMetricName):
            raise TypeError("邮件反馈 metric 无效")
        logger.info(
            "邮件反馈指标",
            extra={
                "metric_name": name.value,
                "tenant_id": str(tenant_id),
                "mailbox_alias": mailbox_alias,
                "metric_value": value,
                "feedback_kind": kind.value if kind is not None else None,
                "feedback_result": result.value if result is not None else None,
            },
        )


WaitForNextCycle = Callable[[int, asyncio.Event], Awaitable[None]]
LockFactory = Callable[[AsyncEngine, int], AdvisoryLock]


@dataclass(frozen=True)
class EmailFeedbackWorkerApplication:
    runtime: EmailFeedbackRuntime
    cursor_reader: FeedbackCursorReader
    metrics: EmailFeedbackMetrics
    health_server: HealthServer
    registered_tool_ids: tuple[str, ...] = field(default=())


class RuntimeFactory(Protocol):
    def __call__(
        self,
    ) -> AbstractAsyncContextManager[EmailFeedbackWorkerApplication]: ...


class _DatabaseCursorReader:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._factory = factory
        self._now = now

    async def get_cursor(
        self, tenant_id: TenantId, mailbox_alias: str
    ) -> str | None:
        async with self._factory() as session:
            cursor = await FeedbackCursorRepositoryImpl(
                session, tenant_id, now=self._now
            ).get(tenant_id, mailbox_alias)
            return cursor.provider_cursor if cursor is not None else None


@dataclass(frozen=True, repr=False)
class _BoundFeedbackSecretResolver:
    _resolver: SecretResolver = field(repr=False)
    _configured_ref: str = field(repr=False)

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "GMAIL_OAUTH_TOKEN_REF":
            raise ValidationError("Gmail 凭证引用无效")
        return self._resolver.resolve(self._configured_ref)


class _FeedbackTenantCheck:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        if ctx.tenant_id != self._tenant_id:
            return CheckRejection(
                self.name, "tenant:mismatch", "工具租户绑定无效"
            )
        return None


def _outreach_actor(identity_id: SendingIdentityId) -> OutreachActor:
    return OutreachActor(
        "system:feedback",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _sending_actor(identity_id: SendingIdentityId) -> SendingIdentityActor:
    return SendingIdentityActor(
        "system:feedback",
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _outreach_builder(
    tenant_id: TenantId, now: Callable[[], datetime]
) -> OutreachServiceBuilder:
    def build(factory: object, audit: AuditSink) -> OutreachService:
        unused = object()
        return OutreachServiceImpl(
            factory,  # type: ignore[arg-type]
            unused,  # type: ignore[arg-type]
            unused,  # type: ignore[arg-type]
            unused,  # type: ignore[arg-type]
            unused,  # type: ignore[arg-type]
            Phase1OutreachAuthorizer(tenant_id),
            audit,
            now=now,
        )

    return cast(OutreachServiceBuilder, build)


def _sending_builder(
    tenant_id: TenantId, now: Callable[[], datetime]
) -> SendingIdentityServiceBuilder:
    def build(factory: object, audit: AuditSink) -> SendingIdentityService:
        return SendingIdentityServiceImpl(
            factory,  # type: ignore[arg-type]
            Phase1SendingIdentityAuthorizer(tenant_id),
            audit,
            now=now,
        )

    return cast(SendingIdentityServiceBuilder, build)


class EmailFeedbackRuntimeFactory:
    """生产 composition root；上下文拥有 engine、transport 与 health。"""

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        transport_factory: Callable[[str], GmailHttpTransport] = GmailApiHttpTransport,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._environ = environ
        self._transport_factory = transport_factory
        self._now = now

    def __call__(self) -> AbstractAsyncContextManager[EmailFeedbackWorkerApplication]:
        return self._resources()

    @asynccontextmanager
    async def _resources(self) -> AsyncIterator[EmailFeedbackWorkerApplication]:
        settings = EmailFeedbackWorkerSettings.from_environ(self._environ)
        config = settings.config
        health = EmailFeedbackHealthState(disabled=not config.enabled)
        health.mark_ready("config")
        engine = create_engine_from(settings.database_url.get_secret_value())
        transport: GmailHttpTransport | None = None
        primary: BaseException | None = None
        try:
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            if config.enabled:
                await assert_database_schema_current(engine)
                health.mark_ready("schema")
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                health.mark_ready("database")

            secrets = EnvironmentSecretResolver(self._environ)
            fingerprint_key = secrets.resolve(
                settings.tool_call_fingerprint_key_ref
            ).encode("utf-8")
            fingerprints = HmacFingerprintProvider(
                settings.tool_call_fingerprint_key_version, fingerprint_key
            )
            transport = self._transport_factory(settings.gmail_base_url)
            slot = FeedbackPageSlot()
            provider_reader = _GmailProviderEmailFeedbackReader(
                lambda requested_tenant: self._connector(
                    requested_tenant, config.tenant_id, transport, self._now
                ),
                _BoundFeedbackSecretResolver(
                    secrets, settings.gmail_oauth_token_ref
                ),
            )
            handler = EmailFeedbackFetchHandler(provider_reader, slot, fingerprints)
            registry = ToolRegistry()
            registry.register(FEEDBACK_MANIFEST, handler)
            user_id = UserId(new_id("usr"))

            async def authorize(
                ctx: ToolCallContext, _state: ToolInvocationState
            ) -> bool:
                return (
                    ctx.tenant_id == config.tenant_id
                    and ctx.user_id == user_id
                    and ctx.tool_id == FEEDBACK_MANIFEST.tool_id
                )

            checks: dict[str, CheckStage] = {
                "tenant": _FeedbackTenantCheck(config.tenant_id),
                "permission": PermissionCheck(authorize),
            }

            def tool_uow(requested_tenant: TenantId) -> ToolGatewayUnitOfWork:
                return cast(
                    ToolGatewayUnitOfWork,
                    SqlAlchemyToolGatewayUnitOfWork(
                        factory, requested_tenant, now=self._now
                    ),
                )

            tool_uow_factory = cast(ToolGatewayUnitOfWorkFactory, tool_uow)

            gateway = ToolGateway(
                registry,  # type: ignore[arg-type]
                checks,
                tool_uow_factory,
                lease_duration=timedelta(seconds=120),
                lease_owner="email_feedback_worker",
                now=self._now,
                id_factory=new_id,
            )
            reader = ToolGatewayEmailFeedbackReader(gateway, slot, user_id)
            audit = StandardAuditLogger()

            def feedback_uow(requested_tenant: TenantId) -> FeedbackPageUnitOfWork:
                return cast(
                    FeedbackPageUnitOfWork,
                    SqlAlchemyFeedbackPageUnitOfWork(
                        factory,
                        requested_tenant,
                        outreach_builder=_outreach_builder(
                            config.tenant_id, self._now
                        ),
                        sending_identity_builder=_sending_builder(
                            config.tenant_id, self._now
                        ),
                        audit_sink=audit,
                        now=self._now,
                    ),
                )

            feedback_uow_factory = cast(
                FeedbackPageUnitOfWorkFactory, feedback_uow
            )
            processor = FeedbackPageProcessor(
                feedback_uow_factory,
                route_id=config.feedback_route_id,
                outreach_actor_factory=_outreach_actor,
                sending_identity_actor_factory=_sending_actor,
                now=self._now,
            )
            health.mark_ready("registry")
            runtime = EmailFeedbackRuntime(engine, reader, processor, config, health)
            yield EmailFeedbackWorkerApplication(
                runtime,
                _DatabaseCursorReader(factory, now=self._now),
                SafeLogMetrics(),
                EmailFeedbackHealthServer(health, config.health_port),
                tuple(item.tool_id for item in registry.list_manifests()),
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            cleanup_failure: BaseException | None = None
            if transport is not None:
                close = getattr(transport, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except BaseException as error:  # noqa: BLE001 - cleanup preserves primary
                        if primary is None:
                            cleanup_failure = error
                        else:
                            logger.error("邮件反馈 worker HTTP 资源关闭失败")
            try:
                await engine.dispose()
            except BaseException as error:  # noqa: BLE001 - cleanup preserves primary
                if primary is None and cleanup_failure is None:
                    cleanup_failure = error
                else:
                    logger.error("邮件反馈 worker 数据库资源关闭失败")
            if cleanup_failure is not None:
                raise cleanup_failure

    @staticmethod
    def _connector(
        requested_tenant: TenantId,
        configured_tenant: TenantId,
        transport: GmailHttpTransport,
        now: Callable[[], datetime],
    ) -> GmailConnector:
        if requested_tenant != configured_tenant:
            raise ValueError("feedback connector tenant 无效")
        return GmailConnector(transport, now=now)


def _error_category(error: BaseException) -> str:
    if isinstance(error, ToolGatewayError):
        return error.category.value
    if isinstance(error, TradeOSError):
        return "transient" if error.is_retryable else "permanent"
    return "unexpected"


def log_worker_failure(
    *,
    phase: str,
    category: str,
    tenant_id: TenantId,
    mailbox_alias: str,
    error: BaseException,
) -> None:
    """只记录固定消息、固定分类和异常类型，不记录异常对象。"""
    logger.error(
        "邮件反馈 worker 阶段失败",
        extra={
            "worker_phase": phase,
            "error_category": category,
            "error_type": type(error).__name__,
            "tenant_id": str(tenant_id),
            "mailbox_alias": mailbox_alias,
        },
    )


async def _default_wait(seconds: int, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def install_stop_signals(stop_event: asyncio.Event) -> Callable[[], None]:
    """SIGINT/SIGTERM 只设置 stop flag，不取消在途 page。"""
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.warning(
                "邮件反馈 worker 信号处理器不可用",
                extra={"signal_name": signum.name},
            )
        else:
            installed.append(signum)

    def cleanup() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return cleanup


def _retry_after(error: BaseException, failures: int) -> int:
    override = getattr(error, "retry_after_seconds", None)
    if isinstance(override, int) and not isinstance(override, bool) and 1 <= override <= 3600:
        return override
    return _BACKOFF_SECONDS[min(failures - 1, len(_BACKOFF_SECONDS) - 1)]


def _record_result(
    metrics: EmailFeedbackMetrics,
    config: EmailFeedbackWorkerConfig,
    result: FeedbackPageResult,
) -> None:
    values = (
        (EmailFeedbackMetricName.PROCESSED, result.processed),
        (EmailFeedbackMetricName.DUPLICATE, result.duplicates),
        (EmailFeedbackMetricName.QUARANTINED, result.quarantined),
        (EmailFeedbackMetricName.HARD_BOUNCE, result.hard_bounces),
        (EmailFeedbackMetricName.SOFT_BOUNCE, result.soft_bounces),
        (EmailFeedbackMetricName.COMPLAINT, result.complaints),
        (EmailFeedbackMetricName.CURSOR_LAG, 0),
        (EmailFeedbackMetricName.CONSECUTIVE_FAILURE, 0),
    )
    for name, value in values:
        metrics.record(
            name,
            tenant_id=config.tenant_id,
            mailbox_alias=config.mailbox_alias,
            value=value,
        )


async def run_email_feedback_worker(
    runtime: EmailFeedbackRuntime,
    *,
    cursor_reader: FeedbackCursorReader,
    metrics: EmailFeedbackMetrics | None = None,
    lock_factory: LockFactory = PostgresAdvisoryLock,
    stop_event: asyncio.Event | None = None,
    wait: WaitForNextCycle | None = None,
    install_signal_handlers: bool = True,
) -> WorkerRunResult:
    """单锁循环读取并整页提交；锁丢失时丢弃已取 page。"""
    config = runtime.config
    if not config.enabled:
        runtime.health.disabled = True
        return WorkerRunResult(WorkerRunStatus.DISABLED, 0)

    stop = stop_event if stop_event is not None else asyncio.Event()
    wait_next = wait if wait is not None else _default_wait
    metric_sink = metrics if metrics is not None else SafeLogMetrics()
    lock = lock_factory(
        runtime.lock_engine,
        derive_advisory_lock_key(config.tenant_id, config.mailbox_alias),
    )
    if not await lock.acquire():
        await lock.close()
        return WorkerRunResult(WorkerRunStatus.LOCK_NOT_ACQUIRED, 0)

    cleanup_signals = (
        install_stop_signals(stop) if install_signal_handlers else lambda: None
    )
    cycles = 0
    failures = 0
    primary: BaseException | None = None
    try:
        while not stop.is_set():
            if not await lock.heartbeat():
                return WorkerRunResult(WorkerRunStatus.LOCK_LOST, cycles)
            if stop.is_set():
                break
            phase = "cursor"
            try:
                cursor = await cursor_reader.get_cursor(
                    config.tenant_id, config.mailbox_alias
                )
                phase = "fetch"
                page = await runtime.reader.fetch(
                    config.tenant_id,
                    config.mailbox_alias,
                    cursor,
                    config.page_limit,
                )
                if not await lock.heartbeat():
                    return WorkerRunResult(WorkerRunStatus.LOCK_LOST, cycles)
                phase = "process"
                result = await runtime.processor.process(
                    config.tenant_id,
                    config.mailbox_alias,
                    config.sending_identity_id,
                    cursor,
                    page,
                )
            except Exception as error:  # noqa: BLE001 - page 保留旧 cursor 并统一重试
                failures += 1
                category = _error_category(error)
                log_worker_failure(
                    phase=phase,
                    category=category,
                    tenant_id=config.tenant_id,
                    mailbox_alias=config.mailbox_alias,
                    error=error,
                )
                if phase == "fetch":
                    runtime.health.mark_provider_degraded()
                metric_sink.record(
                    EmailFeedbackMetricName.PAGE_ROLLBACK,
                    tenant_id=config.tenant_id,
                    mailbox_alias=config.mailbox_alias,
                )
                metric_sink.record(
                    EmailFeedbackMetricName.CONSECUTIVE_FAILURE,
                    tenant_id=config.tenant_id,
                    mailbox_alias=config.mailbox_alias,
                    value=failures,
                )
                if stop.is_set():
                    break
                await wait_next(_retry_after(error, failures), stop)
                continue

            cycles += 1
            failures = 0
            runtime.health.mark_provider_ok()
            _record_result(metric_sink, config, result)
            if stop.is_set():
                break
            await wait_next(config.poll_interval_seconds, stop)
        return WorkerRunResult(WorkerRunStatus.STARTED, cycles)
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_signals()
        try:
            await lock.close()
        except BaseException:
            if primary is None:
                raise
            logger.error("邮件反馈 worker 锁资源清理失败")


async def run_email_feedback_application(
    application: EmailFeedbackWorkerApplication,
    *,
    stop_event: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> WorkerRunResult:
    """并行托管 health；disabled 只服务 health，绝不进入 worker IO。"""
    stop = stop_event if stop_event is not None else asyncio.Event()
    cleanup_signals = (
        install_stop_signals(stop) if install_signal_handlers else lambda: None
    )
    health_task: asyncio.Task[None] | None = None
    worker_task: asyncio.Task[WorkerRunResult] | None = None
    stop_task: asyncio.Task[bool] | None = None
    primary: BaseException | None = None
    try:
        health_task = asyncio.create_task(application.health_server.serve())
        await asyncio.sleep(0)
        if application.runtime.config.enabled:
            worker_task = asyncio.create_task(
                run_email_feedback_worker(
                    application.runtime,
                    cursor_reader=application.cursor_reader,
                    metrics=application.metrics,
                    stop_event=stop,
                    install_signal_handlers=False,
                )
            )
            active_tasks = {
                cast(asyncio.Task[object], health_task),
                cast(asyncio.Task[object], worker_task),
            }
            done, _pending = await asyncio.wait(
                active_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if health_task in done:
                await health_task
                raise RuntimeError("邮件反馈 worker health 服务意外退出")
            return await worker_task
        application.runtime.health.disabled = True
        stop_task = asyncio.create_task(stop.wait())
        disabled_tasks = {
            cast(asyncio.Task[object], health_task),
            cast(asyncio.Task[object], stop_task),
        }
        done, _pending = await asyncio.wait(
            disabled_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if health_task in done:
            await health_task
            raise RuntimeError("邮件反馈 worker health 服务意外退出")
        await stop_task
        return WorkerRunResult(WorkerRunStatus.DISABLED, 0)
    except BaseException as error:
        primary = error
        stop.set()
        if worker_task is not None and not worker_task.done():
            try:
                await asyncio.shield(worker_task)
            except BaseException:  # noqa: BLE001 - application primary wins
                logger.error("邮件反馈 worker 停机等待失败")
        raise
    finally:
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except BaseException:  # noqa: BLE001 - expected internal cancellation
                logger.debug("邮件反馈 worker 内部 stop wait 已取消")
        cleanup_signals()
        if health_task is not None:
            try:
                await application.health_server.close()
                await health_task
            except BaseException:
                health_task.cancel()
                if primary is None:
                    raise
                logger.error("邮件反馈 worker health 资源关闭失败")
