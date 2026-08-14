"""scheduler 的完整订阅注册与资源生命周期装配原语。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Generator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from socket import socket
from typing import Protocol, cast

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from connectors.dns_auth.client import (
    AsyncTxtResolver,
    DnsAuthenticationConnector,
    DnsPythonAsyncResolver,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.opportunities.permissions import ScopeLevel as OpportunityScopeLevel
from domains.opportunities.service import OpportunityService
from domains.sending_identity.permissions import (
    Phase1SendingIdentityAuthorizer,
    StandardAuditLogger,
)
from domains.sending_identity.service import (
    SendingIdentityService,
    SendingIdentityUnitOfWorkFactory,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.schema import assert_database_schema_current
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from infra.secrets import EnvironmentSecretResolver
from shared.errors import ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import (
    ApprovalDecided,
    CommitmentOverdue,
    DomainEvent,
    HandoffQueueBacklogged,
    HandoffRequested,
    ReputationThresholdBreached,
    SendingIdentitySuspended,
)
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.dns_auth import (
    MANIFEST as DNS_AUTH_MANIFEST,
)
from tool_gateway.handlers.dns_auth import (
    DnsAuthenticationCheckHandler,
    ToolGatewayDnsAuthenticationChecker,
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
from workflows.engine.runner import WorkflowEngine
from workflows.human_handoff.flow import (
    HumanHandoffEmployeeReader,
    build_human_handoff_step_handlers,
    register_human_handoff,
)
from workflows.sending_identity_auth.flow import (
    DnsAuthenticationStep,
    register_sending_identity_auth,
)

from .config import SchedulerWorkerConfig
from .main import (
    OutboxDrainer,
    SchedulerConfig,
    SchedulerRuntime,
    WorkflowPoller,
)
from .notification_projection import (
    NotificationAudienceResolver,
    NotificationJobHandoffNotifier,
    NotificationProjectionHandler,
)


class CompleteOutboxRegistry(Protocol):
    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


_HEALTH_CHECKPOINTS = frozenset({"config", "schema", "database", "registry"})


class _NoSignalUvicornServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self._listening = asyncio.Event()

    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield

    async def startup(self, sockets: list[socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            self._listening.set()

    async def wait_started(self) -> None:
        await self._listening.wait()


@dataclass
class SchedulerHealthState:
    _ready: set[str] = field(default_factory=set, repr=False)

    @property
    def is_ready(self) -> bool:
        return self._ready == _HEALTH_CHECKPOINTS

    def mark_ready(self, checkpoint: str) -> None:
        if checkpoint not in _HEALTH_CHECKPOINTS:
            raise ValidationError("scheduler health checkpoint 无效")
        self._ready.add(checkpoint)


def _health_app(state: SchedulerHealthState) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.router.redirect_slashes = False

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        return JSONResponse(
            {"status": "ready" if state.is_ready else "not_ready"},
            status_code=200 if state.is_ready else 503,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(
        _request: object, error: StarletteHTTPException
    ) -> JSONResponse:
        if error.status_code == 405:
            return JSONResponse(
                {"code": "method_not_allowed", "message": "方法不允许"},
                status_code=405,
            )
        return JSONResponse(
            {"code": "not_found", "message": "资源不存在"}, status_code=404
        )

    return app


class SchedulerHealthServer:
    def __init__(self, state: SchedulerHealthState, port: int) -> None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValidationError("scheduler health port 无效")
        self._server = _NoSignalUvicornServer(
            uvicorn.Config(
                _health_app(state),
                host="0.0.0.0",
                port=port,
                access_log=False,
                log_config=None,
            )
        )

    async def serve(self) -> None:
        await self._server.serve()

    async def wait_started(self) -> None:
        await self._server.wait_started()

    async def close(self) -> None:
        self._server.should_exit = True


@dataclass(frozen=True)
class SchedulerDomainDependencies:
    """尚未标准化为配置的 typed 业务依赖；禁止传 repository/raw payload。"""

    opportunity_service: OpportunityService
    employee_service: HumanHandoffEmployeeReader
    notification_audience: NotificationAudienceResolver

    def __post_init__(self) -> None:
        required = (
            getattr(self.opportunity_service, "record_handoff_escalation", None),
            getattr(self.employee_service, "get_employee", None),
            getattr(self.notification_audience, "recipients_for", None),
        )
        if any(not callable(item) for item in required):
            raise ValidationError("scheduler domain dependencies 无效")


class _DnsTenantCheck:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        if ctx.tenant_id != self._tenant_id:
            return CheckRejection(
                self.name, "tenant:mismatch", "DNS 工具租户绑定无效"
            )
        return None


class _DnsReadRateLimitCheck:
    name = "rate_limit"

    def __init__(
        self,
        *,
        max_calls: int = 60,
        window_seconds: int = 60,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(max_calls) is not int
            or max_calls < 1
            or type(window_seconds) is not int
            or window_seconds < 1
            or not callable(now)
        ):
            raise ValidationError("DNS rate limit 配置无效")
        self._max_calls = max_calls
        self._window = timedelta(seconds=window_seconds)
        self._window_seconds = window_seconds
        self._now = now
        self._windows: dict[TenantId, tuple[datetime, int]] = {}
        self._lock = asyncio.Lock()

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if state.prepared is None:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
        now = self._now()
        if not isinstance(now, datetime) or now.tzinfo is not UTC:
            raise ToolGatewayError(
                ToolErrorCategory.PROVIDER_TRANSIENT,
                retry_after_seconds=self._window_seconds,
            )
        async with self._lock:
            started, count = self._windows.get(ctx.tenant_id, (now, 0))
            if now < started:
                raise ToolGatewayError(
                    ToolErrorCategory.PROVIDER_TRANSIENT,
                    retry_after_seconds=self._window_seconds,
                )
            if now - started >= self._window:
                started, count = now, 0
            if count >= self._max_calls:
                raise ToolGatewayError(
                    ToolErrorCategory.RATE_LIMITED,
                    retry_after_seconds=self._window_seconds,
                )
            self._windows[ctx.tenant_id] = (started, count + 1)
        return None


def register_complete_scheduler(
    engine: WorkflowEngine,
    registry: CompleteOutboxRegistry,
    *,
    notification_handler: EventHandler[DomainEvent],
    t1: timedelta,
    t2: timedelta,
) -> None:
    """一次性注册全部已支持 workflow 与投影，防止 partial delivery。"""
    register_human_handoff(engine, registry, t1=t1, t2=t2)
    for event_type, name in (
        (HandoffRequested, "notification.handoff_requested"),
        (HandoffQueueBacklogged, "notification.handoff_queue_backlogged"),
        (SendingIdentitySuspended, "notification.sending_identity_suspended"),
        (
            ReputationThresholdBreached,
            "notification.reputation_threshold_breached",
        ),
        (CommitmentOverdue, "notification.commitment_overdue"),
        (ApprovalDecided, "notification.approval_decided"),
    ):
        registry.register_handler(event_type, name, notification_handler)
    register_sending_identity_auth(engine, registry)


class SchedulerRuntimeFactory:
    """生产 composition root：只注入 typed 业务依赖，其余资源在此构造/释放。"""

    def __init__(
        self,
        environ: Mapping[str, str],
        dependencies: SchedulerDomainDependencies,
        *,
        resolver_factory: Callable[[], AsyncTxtResolver] = DnsPythonAsyncResolver,
        health_server_factory: Callable[
            [SchedulerHealthState, int], SchedulerHealthServer
        ] = SchedulerHealthServer,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(environ, Mapping) or not isinstance(
            dependencies, SchedulerDomainDependencies
        ):
            raise ValidationError("scheduler runtime factory 依赖无效")
        self._environ = environ
        self._dependencies = dependencies
        self._resolver_factory = resolver_factory
        self._health_server_factory = health_server_factory
        self._now = now

    def __call__(self):
        return self._resources()

    @asynccontextmanager
    async def _resources(self) -> AsyncIterator[SchedulerRuntime]:
        config = SchedulerWorkerConfig.from_environ(self._environ)
        health = SchedulerHealthState()
        health.mark_ready("config")
        engine = create_engine_from(config.database_url)
        health_server: SchedulerHealthServer | None = None
        health_task: asyncio.Task[None] | None = None
        primary: BaseException | None = None
        try:
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            await assert_database_schema_current(engine)
            health.mark_ready("schema")
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            health.mark_ready("database")

            jobs = PostgresNotificationJobStore(factory, now=self._now)
            notification_handler = NotificationProjectionHandler(
                tenant_id=config.tenant_id,
                audience=self._dependencies.notification_audience,
                jobs=jobs,
                now=self._now,
            )
            handoff_handlers = build_human_handoff_step_handlers(
                opportunity_service=self._dependencies.opportunity_service,
                employee_service=self._dependencies.employee_service,
                notifier=NotificationJobHandoffNotifier(jobs, now=self._now),
                opportunity_system_actor=OpportunityActor(
                    "system:scheduler",
                    OpportunityScope(level=OpportunityScopeLevel.SYSTEM),
                    "system",
                ),
                employee_system_actor=EmployeeActor(
                    "system:scheduler", EmployeeScope.SYSTEM, "system"
                ),
                t1=timedelta(seconds=config.handoff_t1_seconds),
                t2=timedelta(seconds=config.handoff_t2_seconds),
                now=self._now,
            )
            sending_uow_factory = cast(
                SendingIdentityUnitOfWorkFactory,
                lambda tenant: SqlAlchemySendingIdentityUnitOfWork(
                    factory, tenant, now=self._now
                ),
            )
            sending: SendingIdentityService = SendingIdentityServiceImpl(
                sending_uow_factory,
                Phase1SendingIdentityAuthorizer(config.tenant_id),
                StandardAuditLogger(),
                now=self._now,
            )
            secrets = EnvironmentSecretResolver(self._environ)
            fingerprint_key = secrets.resolve(config.fingerprint_key_ref).encode(
                "utf-8"
            )
            fingerprints = HmacFingerprintProvider(
                config.fingerprint_key_version, fingerprint_key
            )
            connector = DnsAuthenticationConnector(
                self._resolver_factory(), now=self._now
            )
            dns_handler = DnsAuthenticationCheckHandler(connector, fingerprints)
            tool_registry = ToolRegistry()
            tool_registry.register(DNS_AUTH_MANIFEST, dns_handler)
            tool_user = UserId(new_id("usr"))

            async def authorize(
                ctx: ToolCallContext, state: ToolInvocationState
            ) -> bool:
                del state
                return (
                    ctx.tenant_id == config.tenant_id
                    and ctx.user_id == tool_user
                    and ctx.tool_id == DNS_AUTH_MANIFEST.tool_id
                )

            checks: dict[str, CheckStage] = {
                "tenant": _DnsTenantCheck(config.tenant_id),
                "permission": PermissionCheck(authorize),
                "idempotency": IdempotencyCheck(),
                "rate_limit": _DnsReadRateLimitCheck(now=self._now),
            }

            def tool_uow(tenant: TenantId) -> ToolGatewayUnitOfWork:
                return cast(
                    ToolGatewayUnitOfWork,
                    SqlAlchemyToolGatewayUnitOfWork(
                        factory, tenant, now=self._now
                    ),
                )

            gateway = ToolGateway(
                tool_registry,  # type: ignore[arg-type]
                checks,
                cast(ToolGatewayUnitOfWorkFactory, tool_uow),
                lease_duration=timedelta(seconds=config.tool_lease_seconds),
                lease_owner="scheduler_dns_auth",
                now=self._now,
                id_factory=new_id,
            )
            auth_step = DnsAuthenticationStep(
                sending,
                ToolGatewayDnsAuthenticationChecker(gateway, tool_user),
                dkim_selector=config.dkim_selector,
            )
            workflow = PostgresWorkflowEngine(
                factory,
                {
                    **handoff_handlers,
                    "sending_identity_auth.check": auth_step,
                },
                now=self._now,
            )
            outbox = OutboxDeliverer(
                factory,
                config.tenant_id,
                now=self._now,
                max_attempts=config.outbox_max_attempts,
            )
            register_complete_scheduler(
                workflow,
                outbox,
                notification_handler=notification_handler,
                t1=timedelta(seconds=config.handoff_t1_seconds),
                t2=timedelta(seconds=config.handoff_t2_seconds),
            )
            if tuple(item.tool_id for item in tool_registry.list_manifests()) != (
                DNS_AUTH_MANIFEST.tool_id,
            ):
                raise ValidationError("scheduler DNS registry 无效")
            health.mark_ready("registry")
            health_server = self._health_server_factory(health, config.health_port)
            health_task = asyncio.create_task(health_server.serve())
            await asyncio.wait_for(health_server.wait_started(), timeout=10)
            yield SchedulerRuntime(
                engine,
                outbox,
                workflow,
                config.tenant_id,
                SchedulerConfig(
                    config.interval_seconds,
                    config.batch_limit,
                    config.lock_key,
                ),
            )
        except BaseException as error:
            primary = error
            raise
        finally:
            cleanup_error: BaseException | None = None
            if health_server is not None:
                try:
                    await health_server.close()
                    if health_task is not None:
                        await health_task
                except BaseException as error:  # noqa: BLE001 - preserve primary
                    cleanup_error = error
            try:
                await engine.dispose()
            except BaseException as error:  # noqa: BLE001 - preserve primary
                if cleanup_error is None:
                    cleanup_error = error
            if primary is None and cleanup_error is not None:
                raise cleanup_error


@asynccontextmanager
async def configured_scheduler_runtime(
    config: SchedulerWorkerConfig,
    *,
    engine: AsyncEngine,
    outbox: OutboxDrainer,
    workflow: WorkflowEngine | WorkflowPoller,
    notification_handler: EventHandler[DomainEvent],
) -> AsyncIterator[SchedulerRuntime]:
    """验证 schema/DB 后注册完整集合，并在所有退出路径释放引擎。"""
    try:
        await assert_database_schema_current(engine)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        register_complete_scheduler(
            workflow,  # type: ignore[arg-type]
            outbox,  # type: ignore[arg-type]
            notification_handler=notification_handler,
            t1=timedelta(seconds=config.handoff_t1_seconds),
            t2=timedelta(seconds=config.handoff_t2_seconds),
        )
        yield SchedulerRuntime(
            engine,
            outbox,
            workflow,
            config.tenant_id,
            SchedulerConfig(
                config.interval_seconds,
                config.batch_limit,
                config.lock_key,
            ),
        )
    finally:
        await engine.dispose()
