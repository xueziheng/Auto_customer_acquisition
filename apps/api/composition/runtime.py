"""Phase 1 API 的唯一正式依赖装配。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.gmail.client import (
    GmailConnector,
    GmailSendRequest,
    GmailSendResult,
    SecretResolver,
)
from connectors.gmail.transport import GmailHttpTransport
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    AuditLogger as EmployeeAuditLogger,
)
from domains.employees.permissions import (
    EmployeeAuthorizer,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
)
from domains.employees.permissions import (
    StandardAuditLogger as EmployeeStandardAuditLogger,
)
from domains.employees.schemas import EmployeeView
from domains.employees.service import EmployeeService
from domains.employees.service_impl import EmployeeServiceImpl
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    Phase1OpportunityAuthorizer,
    ScopeLevel,
)
from domains.opportunities.permissions import (
    StandardAuditLogger as OpportunityStandardAuditLogger,
)
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.service_impl import OpportunityServiceImpl
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
from domains.outreach.schemas import MessageSendPreflight
from domains.outreach.service import (
    CampaignApprovalProvider,
    ContactEligibilityProvider,
    OutreachService,
    OutreachUnitOfWorkFactory,
    ReplyStatusProvider,
    SendingIdentityEligibilityProvider,
)
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
from domains.sending_identity.permissions import (
    StandardAuditLogger as SendingIdentityStandardAuditLogger,
)
from domains.sending_identity.service import (
    SendingIdentityService,
    SendingIdentityUnitOfWorkFactory,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.email_feedback_uow import (
    AuditSink as FeedbackAuditSink,
)
from infra.db.email_feedback_uow import (
    OutreachServiceBuilder,
    SendingIdentityServiceBuilder,
    SqlAlchemyFeedbackPageUnitOfWork,
)
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from notification_gateway.channels.structured_log import StructuredLogChannel
from notification_gateway.models import (
    Notification,
    NotificationChannel,
    NotificationPriority,
)
from notification_gateway.router import NotificationRouter
from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    SendingIdentityId,
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
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_send import (
    DeliveryMaterial,
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
from tool_gateway.pipeline import ToolCallContext, ToolCallResult, ToolGateway
from workflows.email_feedback.repository import FeedbackPageUnitOfWork
from workflows.email_feedback.unsubscribe import (
    FeedbackPageUnitOfWorkFactory,
    UnsubscribeKeyRing,
    UnsubscribeService,
    UnsubscribeServiceImpl,
)
from workflows.human_handoff.flow import (
    HandoffEscalationNotice,
    build_human_handoff_step_handlers,
    register_human_handoff,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    EmployeeServiceScope,
    ToolGatewayInvoker,
)
from ..runtime_config import Phase1RuntimeSettings


@asynccontextmanager
async def employee_service_scope(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    now: Callable[[], datetime],
    authorizer: EmployeeAuthorizer,
    audit: EmployeeAuditLogger,
) -> AsyncIterator[EmployeeService]:
    """为一次调用创建独立员工服务事务，异常回滚且总是关闭会话。"""
    session = factory()
    try:
        employees = EmployeeRepositoryImpl(session, tenant_id)
        yield EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=now,
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=authorizer,
            audit=audit,
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


class RequestScopedHandoffEmployeeReader:
    """让 workflow 的每次员工读取使用独立 service scope。"""

    def __init__(self, scope: EmployeeServiceScope) -> None:
        self._scope = scope

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView:
        async with self._scope(tenant_id) as service:
            return await service.get_employee(tenant_id, employee_id, actor=actor)


class StructuredLogOnlyPolicy:
    """Phase 1 仅允许唯一的结构化日志通知渠道。"""

    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        del notification
        if len(available) != 1 or available[0].name != "structured_log":
            raise PolicyViolation("Phase 1 通知渠道配置无效")
        return list(available)


class RuntimeHandoffNotifier:
    """把 workflow 的最小通知 DTO 转成安全的统一通知。"""

    def __init__(self, router: NotificationRouter) -> None:
        self._router = router

    async def notify(self, notice: HandoffEscalationNotice) -> None:
        await self._router.dispatch(
            Notification(
                tenant_id=notice.tenant_id,
                recipient=notice.recipient_id,
                priority=NotificationPriority.URGENT,
                title="人工接管提醒",
                context={
                    "kind": "handoff_escalation",
                    "level": notice.level,
                },
                source_event="HandoffRequested",
                dedup_key=notice.dedup_key,
                next_step="处理人工接管任务",
                due_at=notice.sla_due_at,
                link=f"/crm/handoffs/{notice.handoff_id}",
            )
        )


class _UnavailableManualSendSources:
    """仓库尚无正式联系人材料源时的显式失败关闭边界。"""

    async def get_contact_eligibility(self, *args: object) -> object:
        del args
        raise TransientError("发送当前事实未配置")

    async def get_sending_identity_eligibility(self, *args: object) -> object:
        del args
        raise TransientError("发送当前事实未配置")

    async def get_campaign_approval(self, *args: object) -> object:
        del args
        raise TransientError("发送当前事实未配置")

    async def get_reply_status(self, *args: object) -> object:
        del args
        raise TransientError("发送当前事实未配置")

    async def resolve(self, *args: object) -> DeliveryMaterial:
        del args
        raise TransientError("发送材料未配置")

    async def build(self, *args: object) -> UnsubscribeLink:
        del args
        raise TransientError("退订链接未配置")


class _UnavailableToolGateway:
    """不构造空 registry；只返回固定 provider-auth 不可用结果。"""

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        del ctx
        return ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.FAILED_PERMANENT,
            error_category=ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
        )


@dataclass(frozen=True)
class ManualSendComposition:
    """部署层显式提供的发送事实、材料、凭证与 Gmail 传输。"""

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
            raise TypeError("API 手工发送依赖未完整配置")


class _BoundGmailSecretResolver:
    def __init__(self, resolver: SecretResolver, configured_ref: str) -> None:
        self._resolver = resolver
        self._configured_ref = configured_ref

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "GMAIL_OAUTH_TOKEN_REF":
            raise ValidationError("Gmail 凭证引用无效")
        return self._resolver.resolve(self._configured_ref)


class _LazyGmailConnector(GmailConnector):
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


class _UnsubscribeLinkAdapter:
    def __init__(self, service: UnsubscribeService) -> None:
        self._service = service

    async def build(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink:
        return await self._service.issue(tenant_id, preflight)


def _unsubscribe_actor(contact_point_id: ContactPointId) -> OutreachActor:
    return OutreachActor(
        "system:one-click-unsubscribe",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_suppression_targets=frozenset({str(contact_point_id)}),
        ),
        "system",
    )


def _delivery_binding_actor(attempt_id: MessageAttemptId) -> OutreachActor:
    return OutreachActor(
        "system:manual-email-send",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_attempt_ids=frozenset({attempt_id}),
        ),
        "system",
    )


@dataclass(frozen=True)
class _ResolvedBinding:
    attempt_id: MessageAttemptId
    campaign_id: CampaignId
    enrollment_id: EnrollmentId
    sending_identity_id: SendingIdentityId
    idempotency_key: IdempotencyKey
    campaign_created_by: EmployeeId


class ResolvedManualSendGateway:
    """把 API 的最小请求绑定到当前数据库事实后再进入 Tool Gateway。"""

    def __init__(
        self,
        gateway: ToolGateway | None,
        factory: async_sessionmaker[AsyncSession],
        employees: EmployeeServiceScope,
        employee_lookup_actor: EmployeeActor,
        tenant_id: TenantId,
    ) -> None:
        self._gateway = gateway
        self._factory = factory
        self._employees = employees
        self._employee_lookup_actor = employee_lookup_actor
        self._tenant_id = tenant_id

    def bind_gateway(self, gateway: ToolGateway) -> None:
        if self._gateway is not None or not isinstance(gateway, ToolGateway):
            raise TypeError("API 手工发送 Gateway 装配无效")
        self._gateway = gateway

    @staticmethod
    def _failure(category: ToolErrorCategory) -> ToolCallResult:
        return ToolCallResult(
            tool_id="email.send",
            status=ToolCallStatus.FAILED_PERMANENT,
            error_category=category,
        )

    async def _binding(self, attempt_id: str) -> _ResolvedBinding | None:
        async with SqlAlchemyOutreachUnitOfWork(
            self._factory, self._tenant_id
        ) as uow:
            attempt = await uow.attempts.get_for_update(
                self._tenant_id, MessageAttemptId(attempt_id)
            )
            if attempt is None:
                return None
            campaign = await uow.campaigns.get(
                self._tenant_id, attempt.campaign_id
            )
        if campaign is None:
            return None
        return _ResolvedBinding(
            attempt.attempt_id,
            attempt.campaign_id,
            attempt.enrollment_id,
            attempt.sending_identity_id,
            attempt.idempotency_key,
            campaign.created_by,
        )

    async def _authorized(
        self,
        ctx: ToolCallContext,
        binding: _ResolvedBinding,
    ) -> bool:
        try:
            async with self._employees(self._tenant_id) as service:
                employee = await service.get_employee(
                    self._tenant_id,
                    EmployeeId(str(ctx.user_id)),
                    actor=self._employee_lookup_actor,
                )
                if not employee.is_active:
                    return False
                if employee.role == "boss":
                    return True
                if employee.role == "sales":
                    return employee.employee_id == binding.campaign_created_by
                if employee.role != "manager":
                    return False
                creator = await service.get_employee(
                    self._tenant_id,
                    binding.campaign_created_by,
                    actor=self._employee_lookup_actor,
                )
                return creator.is_active and creator.manager_id == employee.employee_id
        except (PermissionDenied, ValidationError, TransientError):
            return False

    async def authorize(
        self, ctx: ToolCallContext, _state: object
    ) -> bool:
        attempt_id = ctx.params.get("attempt_id")
        if not isinstance(attempt_id, str):
            return False
        binding = await self._binding(attempt_id)
        return bool(
            binding is not None
            and ctx.campaign_ref == str(binding.campaign_id)
            and ctx.idempotency_key == binding.idempotency_key
            and await self._authorized(ctx, binding)
        )

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        if (
            ctx.tenant_id != self._tenant_id
            or ctx.tool_id != "email.send"
            or ctx.idempotency_key is not None
            or ctx.campaign_ref is not None
            or set(ctx.params) != {"attempt_id", "subject", "body"}
        ):
            return self._failure(ToolErrorCategory.VALIDATION)
        attempt_id = ctx.params.get("attempt_id")
        subject = ctx.params.get("subject")
        body = ctx.params.get("body")
        if (
            not isinstance(attempt_id, str)
            or not isinstance(subject, str)
            or not isinstance(body, str)
        ):
            return self._failure(ToolErrorCategory.VALIDATION)
        binding = await self._binding(attempt_id)
        if binding is None:
            return self._failure(ToolErrorCategory.VALIDATION)
        if not await self._authorized(ctx, binding):
            return self._failure(ToolErrorCategory.PERMISSION_DENIED)
        enriched = ToolCallContext(
            tenant_id=ctx.tenant_id,
            user_id=UserId(str(ctx.user_id)),
            tool_id=ctx.tool_id,
            params={
                "attempt_id": attempt_id,
                "subject": subject,
                "body": body,
                "enrollment_id": str(binding.enrollment_id),
            },
            run_id=ctx.run_id,
            idempotency_key=binding.idempotency_key,
            campaign_ref=str(binding.campaign_id),
        )
        if self._gateway is None:
            return self._failure(ToolErrorCategory.UNEXPECTED)
        return await self._gateway.invoke(enriched)


def _outreach_system_actor(ctx: ToolCallContext) -> OutreachActor:
    enrollment_id = ctx.params.get("enrollment_id")
    if not isinstance(enrollment_id, str):
        raise ValidationError("发送 enrollment 绑定无效")
    typed_id = EnrollmentId(enrollment_id)
    return OutreachActor(
        "system:manual-email-send",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({typed_id}),
        ),
        "system",
    )


def _sending_system_actor(
    _ctx: ToolCallContext, preflight: object
) -> SendingIdentityActor:
    from domains.outreach.schemas import MessageSendPreflight

    if not isinstance(preflight, MessageSendPreflight):
        raise ValidationError("发送 preflight 无效")
    identity_id = SendingIdentityId(str(preflight.sending_identity_id))
    return SendingIdentityActor(
        "system:manual-email-send",
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


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
        output_schema={"type": "object"},
        redact_fields=("subject", "body"),
    )


def build_phase1_dependencies(
    settings: Phase1RuntimeSettings,
    factory: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime],
    manual_send: ManualSendComposition | None = None,
    secret_resolver: SecretResolver | None = None,
) -> ConfiguredApiDependencies:
    """装配真实 Postgres、领域服务、workflow、outbox 与通知出口。"""
    tenant = TenantId(settings.tenant_id)
    opportunity_authorizer = Phase1OpportunityAuthorizer(tenant)
    employee_authorizer = Phase1EmployeeAuthorizer(tenant)
    opportunity_audit = OpportunityStandardAuditLogger()
    employee_audit = EmployeeStandardAuditLogger()
    employees = partial(
        employee_service_scope,
        factory,
        now=now,
        authorizer=employee_authorizer,
        audit=employee_audit,
    )
    opportunities = OpportunityServiceImpl(
        # 可写 Protocol 属性不协变；具体 UoW 的仓储/总线逐项实现同一公共契约。
        lambda: SqlAlchemyOpportunityUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, tenant
        ),
        OpportunityScorerImpl(settings.scoring_policy),
        settings.handoff_policy,
        authorizer=opportunity_authorizer,
        audit=opportunity_audit,
        now=now,
    )
    unavailable_send_sources = _UnavailableManualSendSources()
    contact_eligibility = (
        manual_send.contact_eligibility
        if manual_send is not None
        else unavailable_send_sources
    )
    sender_eligibility = (
        manual_send.sending_identity_eligibility
        if manual_send is not None
        else unavailable_send_sources
    )
    campaign_approvals = (
        manual_send.campaign_approvals
        if manual_send is not None
        else unavailable_send_sources
    )
    reply_status = (
        manual_send.reply_status
        if manual_send is not None
        else unavailable_send_sources
    )
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
    sending_identities = SendingIdentityServiceImpl(
        lambda requested_tenant: SqlAlchemySendingIdentityUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, requested_tenant, now=now
        ),
        Phase1SendingIdentityAuthorizer(tenant),
        SendingIdentityStandardAuditLogger(),
        now=now,
    )
    resolved_secret_resolver = secret_resolver or (
        manual_send.secret_resolver if manual_send is not None else None
    )
    if resolved_secret_resolver is None:
        raise TypeError("API 退订密钥依赖未完整配置")
    unsubscribe_keys: dict[str, bytes] = {}
    for reference in settings.unsubscribe_key_refs:
        raw_key = resolved_secret_resolver.resolve(reference.secret_ref)
        if not isinstance(raw_key, str):
            raise TypeError("API 退订密钥依赖未完整配置")
        unsubscribe_keys[reference.key_id] = raw_key.encode("utf-8")
    key_ring = UnsubscribeKeyRing(
        settings.unsubscribe_active_key_id,
        unsubscribe_keys,
    )

    def build_feedback_outreach(
        factory: OutreachUnitOfWorkFactory,
        audit: FeedbackAuditSink,
    ) -> OutreachService:
        return OutreachServiceImpl(
            factory,
            contact_eligibility,  # type: ignore[arg-type]
            sender_eligibility,  # type: ignore[arg-type]
            campaign_approvals,  # type: ignore[arg-type]
            reply_status,  # type: ignore[arg-type]
            Phase1OutreachAuthorizer(tenant),
            audit,  # type: ignore[arg-type]
            now=now,
        )

    def build_feedback_sending_identity(
        factory: SendingIdentityUnitOfWorkFactory,
        audit: FeedbackAuditSink,
    ) -> SendingIdentityService:
        return SendingIdentityServiceImpl(
            factory,
            Phase1SendingIdentityAuthorizer(tenant),
            audit,  # type: ignore[arg-type]
            now=now,
        )

    outreach_builder: OutreachServiceBuilder = build_feedback_outreach
    sending_builder: SendingIdentityServiceBuilder = build_feedback_sending_identity

    def build_feedback_uow(
        tenant_id: TenantId,
    ) -> FeedbackPageUnitOfWork:
        return cast(
            FeedbackPageUnitOfWork,
            SqlAlchemyFeedbackPageUnitOfWork(
                factory,
                tenant_id,
                outreach_builder=outreach_builder,
                sending_identity_builder=sending_builder,
                audit_sink=OutreachStandardAuditLogger(),
                now=now,
            ),
        )

    feedback_uow_factory: FeedbackPageUnitOfWorkFactory = build_feedback_uow
    unsubscribe_service = UnsubscribeServiceImpl(
        tenant_id=tenant,
        uow_factory=feedback_uow_factory,
        key_ring=key_ring,
        base_url=settings.unsubscribe_base_url,
        actor_factory=_unsubscribe_actor,
        now=now,
    )
    unsubscribe_links: UnsubscribeLinkProvider = _UnsubscribeLinkAdapter(
        unsubscribe_service
    )
    employee_system_actor = EmployeeActor(
        "system:phase1-handoff",
        EmployeeScope.SYSTEM,
        "system",
    )
    if manual_send is None:
        manual_gateway: ToolGatewayInvoker = _UnavailableToolGateway()
        delivery_materials: DeliveryMaterialProvider = unavailable_send_sources
    else:
        fingerprint_key = manual_send.secret_resolver.resolve(
            settings.tool_call_fingerprint_key_ref
        )
        if not isinstance(fingerprint_key, str):
            raise TypeError("API 手工发送依赖未完整配置")
        gmail = _LazyGmailConnector(
            manual_send.gmail_transport,
            manual_send.secret_resolver,
            settings.gmail_oauth_token_ref,
        )
        handler = EmailSendHandler(
            gmail,
            manual_send.delivery_materials,
            unsubscribe_links,
            HmacFingerprintProvider(
                settings.tool_call_fingerprint_key_version,
                fingerprint_key.encode("utf-8"),
            ),
            outreach=outreach,
            outreach_actor_factory=_delivery_binding_actor,
            route_id=settings.email_feedback_route_id,
        )
        registry = ToolRegistry()
        registry.register(_email_send_manifest(), handler)
        resolved_gateway = ResolvedManualSendGateway(
            None,
            factory,
            employees,
            employee_system_actor,
            tenant,
        )
        rate_limit = RateLimitCheck(
            outreach,
            sending_identities,
            _outreach_system_actor,
            _sending_system_actor,
        )
        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            {
                "tenant": TenantCheck(),
                "permission": PermissionCheck(resolved_gateway.authorize),
                "suppression": SuppressionCheck(
                    outreach, _outreach_system_actor
                ),
                "approval": ApprovalCheck(),
                "idempotency": IdempotencyCheck(),
                "rate_limit": rate_limit,
            },
            lambda requested_tenant: SqlAlchemyToolGatewayUnitOfWork(  # type: ignore[arg-type, return-value]
                factory, requested_tenant, now=now
            ),
            lease_duration=settings.tool_lease,
            lease_owner="api-manual-send",
            now=now,
            id_factory=new_id,
        )
        resolved_gateway.bind_gateway(gateway)
        manual_gateway = resolved_gateway
        delivery_materials = manual_send.delivery_materials
    dedup = PostgresNotificationDedupStore(factory, now=now)
    router = NotificationRouter(dedup, StructuredLogOnlyPolicy())
    router.register_channel(StructuredLogChannel())
    opportunity_system_actor = OpportunityActor(
        "system:phase1-handoff",
        OpportunityScope(level=ScopeLevel.SYSTEM),
        "system",
    )
    handlers = build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=RequestScopedHandoffEmployeeReader(employees),
        notifier=RuntimeHandoffNotifier(router),
        opportunity_system_actor=opportunity_system_actor,
        employee_system_actor=employee_system_actor,
        t1=settings.t1,
        t2=settings.t2,
        now=now,
    )
    workflow = PostgresWorkflowEngine(factory, handlers, now=now)
    outbox = OutboxDeliverer(
        factory,
        tenant,
        now=now,
        max_attempts=settings.outbox_max_attempts,
    )
    register_human_handoff(workflow, outbox, t1=settings.t1, t2=settings.t2)
    return ConfiguredApiDependencies(
        opportunities=opportunities,
        outreach=outreach,
        sending_identities=sending_identities,
        tool_gateway=manual_gateway,
        delivery_materials=delivery_materials,
        unsubscribe_links=unsubscribe_links,
        unsubscribe_service=unsubscribe_service,
        employees=employees,
        opportunity_authorizer=opportunity_authorizer,
        employee_authorizer=employee_authorizer,
        workflow_engine=workflow,
        outbox_deliverer=outbox,
        notification_router=router,
        notification_dedup_store=dedup,
        employee_lookup_actor=employee_system_actor,
    )
