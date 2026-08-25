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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_runtime.qualification_agent.agent import ReplyClassifier
from connectors.dns_auth.client import (
    AsyncTxtResolver,
    DnsAuthenticationConnector,
    DnsPythonAsyncResolver,
)
from connectors.gmail.client import (
    GmailConnector,
    GmailSendRequest,
    GmailSendResult,
    SecretResolver,
)
from connectors.gmail.transport import GmailHttpTransport
from domains.approvals.service import ApprovalService
from domains.approvals.service_impl import ApprovalServiceImpl
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from domains.conversations.service import ConversationService
from domains.demand.service import DemandService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.service import EmployeeService
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.opportunities.permissions import ScopeLevel as OpportunityScopeLevel
from domains.opportunities.service import OpportunityService
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.service import OrganizationService
from domains.organization.service_impl import OrganizationServiceImpl
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
    Phase1OutreachAuthorizer,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.permissions import (
    StandardAuditLogger as OutreachStandardAuditLogger,
)
from domains.outreach.service import (
    CampaignApprovalProvider,
    ContactEligibilityProvider,
    OutreachService,
    OutreachUnitOfWorkFactory,
    ReplyStatusProvider,
    SendingIdentityEligibilityProvider,
)
from domains.outreach.service_impl import OutreachServiceImpl
from domains.prospecting.service import ProspectingService
from domains.sending_identity.permissions import (
    Phase1SendingIdentityAuthorizer,
    StandardAuditLogger,
)
from domains.sending_identity.service import (
    SendingIdentityService,
    SendingIdentityUnitOfWorkFactory,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.email_feedback_uow import (
    AuditSink as FeedbackAuditSink,
)
from infra.db.email_feedback_uow import (
    OutreachServiceBuilder,
    SendingIdentityServiceBuilder,
    SqlAlchemyFeedbackPageUnitOfWork,
)
from infra.db.organization_uow import SqlAlchemyOrganizationUnitOfWork
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
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
    InboundMessageStored,
    ReplyReceived,
    ReputationThresholdBreached,
    SendingIdentityActivated,
    SendingIdentitySuspended,
)
from shared.schemas.identifiers import (
    ContactPointId,
    MessageAttemptId,
    NeedHypothesisId,
    RunId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.approval import ApprovalCheck
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.checks.rate_limit import RateLimitCheck
from tool_gateway.checks.suppression import SuppressionCheck
from tool_gateway.checks.tenant import TenantCheck
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.dns_auth import (
    MANIFEST as DNS_AUTH_MANIFEST,
)
from tool_gateway.handlers.dns_auth import (
    DnsAuthenticationCheckHandler,
    ToolGatewayDnsAuthenticationChecker,
)
from tool_gateway.handlers.email_send import (
    DeliveryMaterialProvider,
    EmailSendHandler,
    UnsubscribeLink,
    UnsubscribeLinkProvider,
)
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
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
from workflows.account_discovery.flow import (
    build_account_discovery_handlers,
    register_account_discovery,
)
from workflows.account_discovery.ports import (
    AccountDiscoveryActorResolver,
    AccountDiscoveryCapability,
    AccountDiscoveryTaskReader,
    ContactEnricher,
    ContactVerifier,
)
from workflows.country_policy_change import (
    build_country_policy_change_handlers,
    register_country_policy_change,
)
from workflows.demand_discovery.flow import (
    build_demand_discovery_handlers,
    register_demand_discovery,
)
from workflows.demand_discovery.ports import (
    AccountDiscoveryQueue,
    DemandDiscoveryTaskReader,
    DemandIntelligenceCapability,
)
from workflows.email_feedback.unsubscribe import (
    FeedbackPageUnitOfWorkFactory,
    UnsubscribeKeyRing,
    UnsubscribeService,
    UnsubscribeServiceImpl,
)
from workflows.engine.runner import StepHandler, WorkflowEngine
from workflows.human_handoff.flow import (
    HumanHandoffEmployeeReader,
    build_human_handoff_step_handlers,
    register_human_handoff,
)
from workflows.outreach_campaign.flow import (
    build_outreach_campaign_handlers,
    register_outreach_campaign,
)
from workflows.playbook_change import (
    build_playbook_change_handlers,
    register_playbook_change,
)
from workflows.reply_qualification.flow import (
    build_reply_qualification_handlers,
    register_reply_qualification,
)
from workflows.reply_qualification.ports import (
    InputContentGuard,
    MessageContentReader,
    ReplyActionPorts,
)
from workflows.sending_identity_auth.flow import (
    DnsAuthenticationStep,
    register_sending_identity_auth,
)

from .account_discovery import ComplianceCountryPolicyDecisionReader
from .campaign_driver import (
    CampaignSendDriver,
    SchedulerCampaignPermissionCheck,
    SchedulerCampaignSender,
    driver_actor,
    outreach_actor_for,
    sending_actor_for,
)
from .campaign_events import CampaignEventHandlers
from .config import SchedulerWorkerConfig
from .hunter_contacts import (
    HunterContactComposition,
    build_hunter_contact_tools,
)
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
from .reply_events import ReplyQualificationEventHandlers
from .web_discovery import (
    WebDiscoveryToolComposition,
    build_web_discovery_tools,
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
class CampaignMessagingComposition:
    """部署层显式提供的发送事实、材料、凭证与 Gmail 传输（镜像 API 手工发送）。"""

    contact_eligibility: ContactEligibilityProvider
    sending_identity_eligibility: SendingIdentityEligibilityProvider
    campaign_approvals: CampaignApprovalProvider
    reply_status: ReplyStatusProvider
    delivery_materials: DeliveryMaterialProvider
    secret_resolver: SecretResolver
    gmail_transport: GmailHttpTransport

    def __post_init__(self) -> None:
        providers = (
            (self.contact_eligibility, ContactEligibilityProvider),
            (self.sending_identity_eligibility, SendingIdentityEligibilityProvider),
            (self.campaign_approvals, CampaignApprovalProvider),
            (self.reply_status, ReplyStatusProvider),
            (self.delivery_materials, DeliveryMaterialProvider),
            (self.secret_resolver, SecretResolver),
            (self.gmail_transport, GmailHttpTransport),
        )
        if any(not isinstance(value, contract) for value, contract in providers):
            raise ValidationError("scheduler Campaign 发送依赖未完整配置")


@dataclass(frozen=True)
class ReplyQualificationComposition:
    """部署层显式提供的 reply_qualification 业务链端口（镜像 Campaign 组合）。

    classifier/content_reader/input_guard/conversations/outreach 均为窄端口；
    runtime 只做注册与接线，不构造模型/provider/凭证。生产 main 未注入真实
    ``ReplyModelPort`` 适配器前，不提供本组合即不启用 reply flow。
    """

    classifier: ReplyClassifier
    content_reader: MessageContentReader
    input_guard: InputContentGuard
    conversations: ConversationService
    outreach: OutreachService
    action_ports: ReplyActionPorts | None = None

    def __post_init__(self) -> None:
        # 浅域实现可能只实现部分 Protocol 方法；按 reply 链实际消费的
        # 方法做 callable 存在性校验（SchedulerDomainDependencies 同款风格），
        # 不做整份 Protocol isinstance。
        required = (
            (self.classifier, "classify"),
            (self.classifier, "model"),
            (self.content_reader, "load"),
            (self.input_guard, "check"),
            (self.conversations, "record_classification"),
            (self.outreach, "stop_enrollment"),
            (self.outreach, "add_suppression"),
        )
        if any(
            not callable(getattr(value, name, None))
            for value, name in required
            if name != "model"
        ):
            raise ValidationError("scheduler reply_qualification 依赖未完整配置")
        # ``model`` 是 classified_by 留痕属性（非 callable）：必须为非空 str，
        # 否则分类 provenance 断裂（reply 链实际消费它）。
        model_value = getattr(self.classifier, "model", None)
        if not isinstance(model_value, str) or not model_value.strip():
            raise ValidationError("scheduler reply_qualification 依赖未完整配置")
        if self.action_ports is not None and not isinstance(
            self.action_ports, ReplyActionPorts
        ):
            raise ValidationError("scheduler reply_qualification 动作依赖未完整配置")


@dataclass(frozen=True)
class AccountDiscoveryComposition:
    """账户发现端口；联系人能力只能显式选择自定义实现或 Hunter。"""

    task_reader: AccountDiscoveryTaskReader
    capability: AccountDiscoveryCapability
    prospecting: ProspectingService
    employees: EmployeeService
    actor_resolver: AccountDiscoveryActorResolver
    enricher: ContactEnricher | None = None
    verifier: ContactVerifier | None = None
    hunter: HunterContactComposition | None = None

    def __post_init__(self) -> None:
        required = (
            (self.task_reader, "load"),
            (self.capability, "run"),
            (self.prospecting, "resolve_account"),
            (self.prospecting, "record_discovered_contact"),
            (self.employees, "resolve_owner"),
            (self.actor_resolver, "resolve"),
        )
        if any(not callable(getattr(value, name, None)) for value, name in required):
            raise ValidationError("scheduler account_discovery 依赖未完整配置")
        custom_contacts = self.enricher is not None or self.verifier is not None
        if custom_contacts:
            if (
                self.enricher is None
                or self.verifier is None
                or self.hunter is not None
                or not callable(getattr(self.enricher, "find_contacts", None))
                or not callable(getattr(self.verifier, "verify", None))
            ):
                raise ValidationError("scheduler 联系人发现实现配置冲突")
        elif not isinstance(self.hunter, HunterContactComposition):
            raise ValidationError("scheduler 联系人发现实现未配置")


@dataclass(frozen=True)
class DemandDiscoveryComposition:
    """需求探索端口；Provider 工具必须走显式 Web Tool Gateway 组合。"""

    task_reader: DemandDiscoveryTaskReader
    capability: DemandIntelligenceCapability
    demand: DemandService
    prospecting: ProspectingService
    web_tools: WebDiscoveryToolComposition

    def __post_init__(self) -> None:
        required = (
            (self.task_reader, "load_confirmed"),
            (self.capability, "run"),
            (self.demand, "capture_signal"),
            (self.demand, "create_hypothesis"),
            (self.demand, "get_confidence"),
            (self.prospecting, "resolve_account"),
        )
        if (
            any(not callable(getattr(value, name, None)) for value, name in required)
            or not isinstance(self.web_tools, WebDiscoveryToolComposition)
        ):
            raise ValidationError("scheduler demand_discovery 依赖未完整配置")


@dataclass(frozen=True)
class PlaybookChangeComposition:
    """生产 scheduler 的 Playbook 变更服务与最小 SYSTEM actor。"""

    organization: OrganizationService
    approvals: ApprovalService
    system_actor: OrganizationActor

    def __post_init__(self) -> None:
        required = (
            getattr(self.organization, "get_change_snapshot", None),
            getattr(self.organization, "activate_playbook", None),
            getattr(self.approvals, "submit", None),
            getattr(self.approvals, "get", None),
            getattr(self.approvals, "expire_overdue", None),
            getattr(self.approvals, "mark_applied", None),
            getattr(self.approvals, "mark_apply_failed", None),
        )
        if (
            any(not callable(value) for value in required)
            or not isinstance(self.system_actor, OrganizationActor)
            or self.system_actor.role != "system"
            or self.system_actor.scope.level is not OrganizationScopeLevel.SYSTEM
        ):
            raise ValidationError("scheduler Playbook 变更依赖未完整配置")


@dataclass(frozen=True)
class SchedulerDomainDependencies:
    """尚未标准化为配置的 typed 业务依赖；禁止传 repository/raw payload。"""

    opportunity_service: OpportunityService
    employee_service: HumanHandoffEmployeeReader
    notification_audience: NotificationAudienceResolver
    campaign_messaging: CampaignMessagingComposition | None = None
    reply_qualification: ReplyQualificationComposition | None = None
    account_discovery: AccountDiscoveryComposition | None = None
    demand_discovery: DemandDiscoveryComposition | None = None

    def __post_init__(self) -> None:
        required = (
            getattr(self.opportunity_service, "record_handoff_escalation", None),
            getattr(self.employee_service, "get_employee", None),
            getattr(self.notification_audience, "recipients_for", None),
        )
        if any(not callable(item) for item in required):
            raise ValidationError("scheduler domain dependencies 无效")
        if self.campaign_messaging is not None and not isinstance(
            self.campaign_messaging, CampaignMessagingComposition
        ):
            raise ValidationError("scheduler Campaign 发送依赖未完整配置")
        if self.reply_qualification is not None and not isinstance(
            self.reply_qualification, ReplyQualificationComposition
        ):
            raise ValidationError("scheduler reply_qualification 依赖未完整配置")
        if self.account_discovery is not None and not isinstance(
            self.account_discovery, AccountDiscoveryComposition
        ):
            raise ValidationError("scheduler account_discovery 依赖未完整配置")
        if self.demand_discovery is not None and (
            not isinstance(self.demand_discovery, DemandDiscoveryComposition)
            or self.account_discovery is None
            or self.demand_discovery.prospecting
            is not self.account_discovery.prospecting
        ):
            raise ValidationError("scheduler demand_discovery 依赖未完整配置")


class _AccountDiscoveryWorkflowQueue(AccountDiscoveryQueue):
    """延迟绑定同一 composition root 创建的 workflow engine。"""

    def __init__(self) -> None:
        self._engine: WorkflowEngine | None = None

    def bind(self, engine: WorkflowEngine) -> None:
        if self._engine is not None:
            raise ValidationError("账户发现 workflow queue 重复绑定")
        self._engine = engine

    async def start(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        *,
        campaign_id: str,
        acting_user: UserId,
        role_hints: tuple[str, ...],
        assessment_ref: str,
    ) -> RunId:
        if self._engine is None:
            raise ValidationError("账户发现 workflow queue 尚未绑定")
        return await self._engine.start(
            tenant_id,
            "account_discovery",
            str(hypothesis_id),
            {
                "hypothesis_id": str(hypothesis_id),
                "campaign_id": campaign_id,
                "acting_user_id": str(acting_user),
                "role_hints": list(role_hints),
                "assessment_ref": assessment_ref,
            },
            f"demand-discovery:{hypothesis_id}:{campaign_id}",
        )


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


def _email_send_manifest() -> ToolManifest:
    return ToolManifest(
        tool_id="email.send",
        version="v1",
        description="发送当前已批准 Campaign 的单封邮件",
        risk_level=RiskLevel.HIGH,
        cost_class=CostClass.LOW,
        requires_approval=False,
        idempotency=IdempotencyRequirement.REQUIRED,
        required_permissions=("outreach:message_send",),
        checks=(
            "tenant",
            "permission",
            "suppression",
            "approval",
            "idempotency",
            "rate_limit",
        ),
        input_schema={
            "type": "object",
            "required": ("attempt_id", "subject", "body"),
        },
        output_schema={
            "type": "object",
            "required": ("provider_ref", "already_existed"),
            "properties": {
                "provider_ref": {"type": "string"},
                "already_existed": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        redact_fields=("subject", "body"),
    )


def _scheduler_delivery_binding_actor(attempt_id: MessageAttemptId) -> OutreachActor:
    return OutreachActor(
        "system:scheduler-campaign-send",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_attempt_ids=frozenset({attempt_id}),
        ),
        "system",
    )


def _unsubscribe_actor(contact_point_id: ContactPointId) -> OutreachActor:
    return OutreachActor(
        "system:scheduler-campaign-send",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_suppression_targets=frozenset({str(contact_point_id)}),
        ),
        "system",
    )


class _SchedulerUnsubscribeLinkAdapter:
    """退订链接 provider：与 API 同一 UnsubscribeService 签发机制。"""

    def __init__(self, service: UnsubscribeService) -> None:
        self._service = service

    async def build(
        self, tenant_id: TenantId, preflight: object
    ) -> UnsubscribeLink:
        from domains.outreach.schemas import MessageSendPreflight

        if not isinstance(preflight, MessageSendPreflight):
            raise ValidationError("发送 preflight 无效")
        return await self._service.issue(tenant_id, preflight)


class _BoundGmailSecretResolver:
    def __init__(self, resolver: SecretResolver, configured_ref: str) -> None:
        self._resolver = resolver
        self._configured_ref = configured_ref

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "GMAIL_OAUTH_TOKEN_REF":
            raise ValidationError("Gmail 凭证引用无效")
        return self._resolver.resolve(self._configured_ref)


class _SchedulerLazyGmailConnector(GmailConnector):
    """首次外发前只配置一次凭证；构造 runtime 不接触 secret value。"""

    def __init__(
        self,
        transport: GmailHttpTransport,
        resolver: SecretResolver,
        configured_ref: str,
    ) -> None:
        super().__init__(transport)
        self._runtime_resolver = _BoundGmailSecretResolver(resolver, configured_ref)
        self._configure_lock = asyncio.Lock()

    async def _ensure_configured(self) -> None:
        if not await self.health_check():
            async with self._configure_lock:
                if not await self.health_check():
                    await self.configure(self._runtime_resolver)

    async def send_once(self, request: GmailSendRequest) -> GmailSendResult:
        await self._ensure_configured()
        return await super().send_once(request)

    async def reconcile_once(self, request: GmailSendRequest) -> GmailSendResult:
        await self._ensure_configured()
        return await super().reconcile_once(request)


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
            change_approvals = ApprovalServiceImpl(
                lambda requested_tenant: SqlAlchemyApprovalUnitOfWork(  # type: ignore[arg-type, return-value]
                    factory, requested_tenant, now=self._now
                ),
                now=self._now,
            )
            playbook_organization = OrganizationServiceImpl(
                lambda requested_tenant: SqlAlchemyOrganizationUnitOfWork(  # type: ignore[arg-type, return-value]
                    factory, requested_tenant
                ),
                Phase1OrganizationAuthorizer(config.tenant_id),
                now=self._now,
            )
            playbook_change = PlaybookChangeComposition(
                organization=playbook_organization,
                approvals=change_approvals,
                system_actor=OrganizationActor(
                    "system:playbook-change",
                    OrganizationScope(
                        level=OrganizationScopeLevel.SYSTEM,
                        tenant_id=config.tenant_id,
                    ),
                    "system",
                ),
            )
            playbook_handlers = build_playbook_change_handlers(
                playbook_change.organization,
                playbook_change.approvals,
                playbook_change.system_actor,
            )
            country_policy = ComplianceServiceImpl(
                lambda requested_tenant: SqlAlchemyComplianceUnitOfWork(
                    factory, requested_tenant, now=self._now
                ),
                Phase1ComplianceAuthorizer(config.tenant_id),
                now=self._now,
            )
            country_policy_reader = ComplianceCountryPolicyDecisionReader(
                country_policy,
                ComplianceActor(
                    actor_id="system:scheduler-country-policy",
                    tenant_id=config.tenant_id,
                    scope=ComplianceScope.SYSTEM,
                    role="system",
                ),
            )
            country_policy_handlers = build_country_policy_change_handlers(
                country_policy,
                change_approvals,
                ComplianceActor(
                    actor_id="system:country-policy-change",
                    tenant_id=config.tenant_id,
                    scope=ComplianceScope.SYSTEM,
                    role="system",
                ),
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
            campaign_handlers: dict[str, StepHandler] = {}
            campaign_outreach: OutreachService | None = None
            if self._dependencies.campaign_messaging is not None:
                campaign_handlers, campaign_outreach = self._build_campaign_messaging(
                    factory,
                    config,
                    self._dependencies.campaign_messaging,
                    sending,
                    fingerprints,
                    secrets,
                    tool_user,
                )
            reply_handlers: dict[str, StepHandler] = {}
            if self._dependencies.reply_qualification is not None:
                reply_composition = self._dependencies.reply_qualification
                reply_handlers = build_reply_qualification_handlers(
                    classifier=reply_composition.classifier,
                    content_reader=reply_composition.content_reader,
                    input_guard=reply_composition.input_guard,
                    conversations=reply_composition.conversations,
                    outreach=reply_composition.outreach,
                    tenant_id=config.tenant_id,
                    now=self._now,
                    action_ports=reply_composition.action_ports,
                )
            account_handlers: dict[str, StepHandler] = {}
            if self._dependencies.account_discovery is not None:
                if campaign_outreach is None:
                    raise ValidationError(
                        "account_discovery 必须配置 Campaign 发送链"
                    )
                account = self._dependencies.account_discovery
                if account.hunter is not None:
                    contact_tools = build_hunter_contact_tools(
                        factory=factory,
                        tenant_id=config.tenant_id,
                        tool_user=tool_user,
                        fingerprints=fingerprints,
                        outreach=campaign_outreach,
                        prospecting=account.prospecting,
                        country_policy=country_policy_reader,
                        composition=account.hunter,
                        lease_duration=timedelta(seconds=config.tool_lease_seconds),
                        now=self._now,
                    )
                    enricher = contact_tools.enricher
                    verifier = contact_tools.verifier
                else:
                    if account.enricher is None or account.verifier is None:
                        raise ValidationError("scheduler 联系人发现实现未配置")
                    enricher = account.enricher
                    verifier = account.verifier
                account_handlers = build_account_discovery_handlers(
                    task_reader=account.task_reader,
                    capability=account.capability,
                    prospecting=account.prospecting,
                    enricher=enricher,
                    verifier=verifier,
                    employees=account.employees,
                    outreach=campaign_outreach,
                    actor_resolver=account.actor_resolver,
                    now=self._now,
                )
            demand_handlers: dict[str, StepHandler] = {}
            account_queue: _AccountDiscoveryWorkflowQueue | None = None
            if self._dependencies.demand_discovery is not None:
                demand_discovery = self._dependencies.demand_discovery
                web_tools = build_web_discovery_tools(
                    factory=factory,
                    tenant_id=config.tenant_id,
                    tool_user=tool_user,
                    fingerprints=fingerprints,
                    composition=demand_discovery.web_tools,
                    country_policy=country_policy_reader,
                    lease_duration=timedelta(
                        seconds=config.tool_lease_seconds
                    ),
                    now=self._now,
                )
                account_queue = _AccountDiscoveryWorkflowQueue()
                demand_handlers = build_demand_discovery_handlers(
                    task_reader=demand_discovery.task_reader,
                    searcher=web_tools.searcher,
                    page_reader=web_tools.page_reader,
                    capability=demand_discovery.capability,
                    demand=demand_discovery.demand,
                    prospecting=demand_discovery.prospecting,
                    account_queue=account_queue,
                )
            workflow = PostgresWorkflowEngine(
                factory,
                {
                    **handoff_handlers,
                    "sending_identity_auth.check": auth_step,
                    **campaign_handlers,
                    **reply_handlers,
                    **account_handlers,
                    **demand_handlers,
                    **playbook_handlers,
                    **country_policy_handlers,
                },
                now=self._now,
            )
            if account_queue is not None:
                account_queue.bind(workflow)
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
            register_playbook_change(workflow, outbox, change_approvals)
            register_country_policy_change(workflow, outbox, change_approvals)
            campaign_driver: CampaignSendDriver | None = None
            if campaign_outreach is not None:
                register_outreach_campaign(
                    workflow,
                    timedelta(seconds=config.campaign_retry_interval_seconds),
                )
                campaign_driver = CampaignSendDriver(
                    outreach=campaign_outreach,
                    engine=workflow,
                    factory=factory,
                    tenant_id=config.tenant_id,
                    scan_actor=driver_actor(),
                    batch_limit=config.batch_limit,
                )
                # 生产事件接线：回复停序列 + 唤醒、身份激活唤醒（非测试直投）
                campaign_events = CampaignEventHandlers(
                    outreach=campaign_outreach,
                    engine=workflow,
                    factory=factory,
                    tenant_id=config.tenant_id,
                )
                outbox.register_handler(
                    ReplyReceived,
                    "outreach_campaign.reply_received",
                    campaign_events,
                )
                outbox.register_handler(
                    SendingIdentityActivated,
                    "outreach_campaign.sending_identity_activated",
                    campaign_events,
                )

            if self._dependencies.reply_qualification is not None:
                register_reply_qualification(workflow)
                reply_events = ReplyQualificationEventHandlers(
                    engine=workflow,
                    factory=factory,
                    tenant_id=config.tenant_id,
                )
                outbox.register_handler(
                    InboundMessageStored,
                    "reply_qualification.inbound_stored",
                    reply_events,
                )

            if self._dependencies.account_discovery is not None:
                register_account_discovery(workflow)

            if self._dependencies.demand_discovery is not None:
                register_demand_discovery(workflow)

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
                campaign_driver=campaign_driver,
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

    def _build_campaign_messaging(
        self,
        factory: async_sessionmaker[AsyncSession],
        config: SchedulerWorkerConfig,
        composition: CampaignMessagingComposition,
        sending: SendingIdentityService,
        fingerprints: HmacFingerprintProvider,
        secrets: EnvironmentSecretResolver,
        tool_user: UserId,
    ) -> tuple[dict[str, StepHandler], OutreachService]:
        """装配 Campaign 发送链路：outreach 服务、退订链接、email.send 网关。

        镜像 API 手工发送的完整检查管线（current-facts preflight/claim/额度/
        发送/记账），返回流程 handler 与 outreach 服务供驱动使用。
        """
        from workflows.email_feedback.repository import FeedbackPageUnitOfWork

        tenant = config.tenant_id
        now = self._now
        contact_eligibility = composition.contact_eligibility
        sender_eligibility = composition.sending_identity_eligibility
        campaign_approvals = composition.campaign_approvals
        reply_status = composition.reply_status
        outreach = OutreachServiceImpl(
            lambda requested_tenant: SqlAlchemyOutreachUnitOfWork(  # type: ignore[arg-type, return-value]
                factory, requested_tenant, now=now
            ),
            contact_eligibility,  # type: ignore[arg-type]
            sender_eligibility,  # type: ignore[arg-type]
            campaign_approvals,  # type: ignore[arg-type]
            reply_status,  # type: ignore[arg-type]
            Phase1OutreachAuthorizer(tenant),
            OutreachStandardAuditLogger(),
            now=now,
        )
        unsubscribe_keys: dict[str, bytes] = {}
        for reference in config.unsubscribe_key_refs:
            raw_key = secrets.resolve(reference.secret_ref)
            if not isinstance(raw_key, str):
                raise ValidationError("scheduler 退订密钥依赖未完整配置")
            unsubscribe_keys[reference.key_id] = raw_key.encode("utf-8")
        key_ring = UnsubscribeKeyRing(
            config.unsubscribe_active_key_id, unsubscribe_keys
        )

        def build_feedback_outreach(
            uow_factory: OutreachUnitOfWorkFactory,
            audit: FeedbackAuditSink,
        ) -> OutreachService:
            return OutreachServiceImpl(
                uow_factory,
                contact_eligibility,  # type: ignore[arg-type]
                sender_eligibility,  # type: ignore[arg-type]
                campaign_approvals,  # type: ignore[arg-type]
                reply_status,  # type: ignore[arg-type]
                Phase1OutreachAuthorizer(tenant),
                audit,  # type: ignore[arg-type]
                now=now,
            )

        def build_feedback_sending(
            uow_factory: SendingIdentityUnitOfWorkFactory,
            audit: FeedbackAuditSink,
        ) -> SendingIdentityService:
            return SendingIdentityServiceImpl(
                uow_factory,
                Phase1SendingIdentityAuthorizer(tenant),
                audit,  # type: ignore[arg-type]
                now=now,
            )

        def build_feedback_uow(
            tenant_id: TenantId,
        ) -> FeedbackPageUnitOfWork:
            return cast(
                FeedbackPageUnitOfWork,
                SqlAlchemyFeedbackPageUnitOfWork(
                    factory,
                    tenant_id,
                    outreach_builder=cast(
                        OutreachServiceBuilder, build_feedback_outreach
                    ),
                    sending_identity_builder=cast(
                        SendingIdentityServiceBuilder, build_feedback_sending
                    ),
                    audit_sink=OutreachStandardAuditLogger(),
                    now=now,
                ),
            )

        feedback_uow_factory: FeedbackPageUnitOfWorkFactory = build_feedback_uow
        unsubscribe_service = UnsubscribeServiceImpl(
            tenant_id=tenant,
            uow_factory=feedback_uow_factory,
            key_ring=key_ring,
            base_url=config.unsubscribe_base_url,
            actor_factory=_unsubscribe_actor,
            now=now,
        )
        unsubscribe_links: UnsubscribeLinkProvider = (
            _SchedulerUnsubscribeLinkAdapter(unsubscribe_service)
        )
        gmail = _SchedulerLazyGmailConnector(
            composition.gmail_transport,
            composition.secret_resolver,
            config.gmail_oauth_token_ref,
        )
        handler = EmailSendHandler(
            gmail,
            composition.delivery_materials,
            unsubscribe_links,
            fingerprints,
            outreach=outreach,
            outreach_actor_factory=_scheduler_delivery_binding_actor,
            route_id=config.email_feedback_route_id,
        )
        campaign_registry = ToolRegistry()
        campaign_registry.register(_email_send_manifest(), handler)
        if tuple(
            item.tool_id for item in campaign_registry.list_manifests()
        ) != ("email.send",):
            raise ValidationError("scheduler Campaign registry 无效")
        permission = SchedulerCampaignPermissionCheck(factory, tenant)
        rate_limit = RateLimitCheck(
            outreach, sending, outreach_actor_for, sending_actor_for
        )
        campaign_gateway = ToolGateway(
            campaign_registry,  # type: ignore[arg-type]
            {
                "tenant": TenantCheck(),
                "permission": PermissionCheck(permission.authorize),
                "suppression": SuppressionCheck(outreach, outreach_actor_for),
                "approval": ApprovalCheck(),
                "idempotency": IdempotencyCheck(),
                "rate_limit": rate_limit,
            },
            cast(
                ToolGatewayUnitOfWorkFactory,
                lambda requested_tenant: SqlAlchemyToolGatewayUnitOfWork(
                    factory, requested_tenant, now=now
                ),
            ),
            lease_duration=timedelta(seconds=config.tool_lease_seconds),
            lease_owner="scheduler_campaign_send",
            now=now,
            id_factory=new_id,
        )
        sender = SchedulerCampaignSender(campaign_gateway, tenant, tool_user)
        return build_outreach_campaign_handlers(outreach, sender, now), outreach


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
