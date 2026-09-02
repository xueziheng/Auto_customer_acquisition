"""Phase 1 API 的唯一正式依赖装配。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.trade_manager import (
    StructuredTradeManagerModelPort,
    TradeManagerAgent,
)
from artifact_store.repository import ArtifactUnitOfWorkFactory
from artifact_store.service_impl import GeneratedArtifactStoreImpl, RawArtifactStoreImpl
from connectors.gmail.client import (
    GmailConnector,
    GmailSendRequest,
    GmailSendResult,
    SecretResolver,
)
from connectors.gmail.transport import GmailHttpTransport
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from connectors.object_store.quote_pdf import S3QuotePdfObjectBlobTransport
from connectors.openai import OpenAIJsonModelClient
from domains.approvals.service import ApprovalService, ApprovalState, ApprovalType
from domains.approvals.service_impl import ApprovalServiceImpl
from domains.commitments.service_impl import CommitmentServiceImpl
from domains.compliance.permissions import Phase1ComplianceAuthorizer
from domains.compliance.service_impl import ComplianceServiceImpl
from domains.conversations.service_impl import ConversationServiceImpl
from domains.costing.permissions import Phase1CostingAuthorizer
from domains.costing.service_impl import CostingServiceImpl
from domains.demand.service import DemandService
from domains.demand.service_impl import DemandServiceImpl
from domains.directives.service_impl import DirectiveServiceImpl
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
from domains.organization.permissions import Phase1OrganizationAuthorizer
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
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    MessageSendPreflight,
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
from domains.products.permissions import Phase2ProductAuthorizer
from domains.products.service_impl import ProductServiceImpl
from domains.prospecting.service import ContactValueHasher
from domains.prospecting.service_impl import ProspectingServiceImpl
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
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.service import CandidateEvidenceSnapshot
from domains.sourcing.service_impl import SourcingServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.commitment_uow import SqlAlchemyCommitmentUnitOfWork
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.directive_uow import SqlAlchemyDirectiveUnitOfWork
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
from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.db.quote_evidence_context import SqlAlchemyQuoteEvidenceContextReader
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.repositories.in_app_notifications import PostgresInAppNotificationStore
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import OutreachCampaignRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from infra.db.work_intake_uow import SqlAlchemyWorkIntakeUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from infra.quote_document_store import (
    GeneratedStoreDocumentAdapter,
    GeneratedStoreDocumentMetadataReader,
)
from infra.quote_evidence_artifacts import RawQuoteEvidenceAdapter
from infra.quote_file_artifacts import GeneratedStoreQuoteArtifactReader
from notification_gateway.channels.structured_log import StructuredLogChannel
from notification_gateway.inbox import (
    InAppNotificationService,
    InAppNotificationServiceImpl,
)
from notification_gateway.jobs import NotificationContext, NotificationKind
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
    ApprovalId,
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
from tool_gateway.provider_readiness import (
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
)
from workflows.account_discovery.flow import build_account_discovery_definition
from workflows.country_policy_change import build_country_policy_change_definition
from workflows.demand_discovery.flow import build_demand_discovery_definition
from workflows.email_feedback.repository import FeedbackPageUnitOfWork
from workflows.email_feedback.unsubscribe import (
    FeedbackPageUnitOfWorkFactory,
    UnsubscribeKeyRing,
    UnsubscribeService,
    UnsubscribeServiceImpl,
)
from workflows.employee_work_intake.repository import WorkIntakeUnitOfWorkFactory
from workflows.employee_work_intake.service_impl import WorkIntakeServiceImpl
from workflows.engine.audit import Phase1RunAuditAuthorizer, RunAuditService
from workflows.engine.runner import WorkflowEngine, WorkflowRun
from workflows.human_handoff.flow import (
    HandoffEscalationNotice,
    build_human_handoff_step_handlers,
    register_human_handoff,
)
from workflows.playbook_change import build_playbook_change_definition
from workflows.quote_approval.application import QuoteApprovalApplication
from workflows.quote_approval.flow import (
    build_quote_approval_handlers,
    register_quote_approval,
)
from workflows.quote_approval.run_reader import WorkflowQuoteRunReader
from workflows.quote_approval.runtime_readers import CurrentQuotationActorReader
from workflows.sourcing_case.application import (
    DirectiveSourcingAdmissionPolicyReader,
    SourcingAdmissionApplication,
    SourcingAdmissionStarter,
    SourcingCaseApplication,
)
from workflows.sourcing_case.flow import build_sourcing_case_definition
from workflows.sourcing_case.steps import (
    AwaitPublicPlanStep,
    AwaitReviewStep,
    FixedWaitStep,
    HandoffCostingStep,
)

from ..dependencies import (
    CampaignScopeResolver,
    ConfiguredApiDependencies,
    EmployeeServiceScope,
    ToolGatewayInvoker,
)
from ..runtime_config import Phase1RuntimeSettings
from .demand_radar import (
    AuthorizedDemandRadarService,
    ProspectingDemandAccountNames,
)
from .quotations import (
    build_need_unit_authorizer,
    build_quotation_domains,
    build_quotation_evidence,
    build_quotation_http,
)
from .quote_notifications import RuntimeQuoteApprovalNotifier
from .work_uploads import WorkUploadApplicationServiceImpl


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


class RequestScopedDirectiveEmployeeReader:
    """用员工域公开服务为指令域提供老板校验与展示名。"""

    def __init__(
        self,
        scope: EmployeeServiceScope,
        actor: EmployeeActor,
    ) -> None:
        self._scope = scope
        self._actor = actor

    async def is_active_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> bool:
        async with self._scope(tenant_id) as service:
            employees = await service.list_active(tenant_id, actor=self._actor)
        return any(
            employee.employee_id == employee_id and employee.role == "boss"
            for employee in employees
        )

    async def names_for(
        self, tenant_id: TenantId, employee_ids: tuple[EmployeeId, ...]
    ) -> dict[EmployeeId, str]:
        async with self._scope(tenant_id) as service:
            employees = await service.list_active(tenant_id, actor=self._actor)
        wanted = set(employee_ids)
        return {
            employee.employee_id: employee.name
            for employee in employees
            if employee.employee_id in wanted
        }


class _DomainSeparatedContactValueHasher(ContactValueHasher):
    """复用运行期 HMAC key，但用固定域标签隔离联系方式指纹语义。"""

    def __init__(self, provider: HmacFingerprintProvider) -> None:
        self._provider = provider

    def fingerprint(self, canonical_value: str) -> str:
        digest, _version = self._provider.fingerprint(
            (b"contact-value-v1", canonical_value.encode("utf-8"))
        )
        return digest


class _StartOnlyWorkflowHandler:
    """API 只创建 run；若误执行步骤则固定失败关闭。"""

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, object]]:
        del run
        raise TransientError("workflow 步骤只能由 scheduler 执行")


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
                context=NotificationContext(
                    NotificationKind.HANDOFF_ESCALATION,
                    str(notice.handoff_id),
                    None,
                    reason_code=notice.level,
                    level=None,
                ),
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


class _UnavailableCandidateEvidenceReader:
    """API 未暴露候选写入口时的失败关闭 Evidence 边界。

    公开计划、运行和审核不会调用它；若未来有人错误地把候选写入口挂到 API，
    领域服务会把这里的固定不可用结果收敛为结构化校验失败，而不会伪造证据。
    """

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: object
    ) -> CandidateEvidenceSnapshot:
        del tenant_id, artifact_id
        raise ValidationError("候选证据读取未配置")


class _ServiceBackedCampaignApprovalProvider:
    """把审批域公共视图适配为触达域所需的版本审批事实。"""

    def __init__(
        self,
        approvals: ApprovalService,
        fallback: CampaignApprovalProvider,
    ) -> None:
        self._approvals = approvals
        self._fallback = fallback

    async def get_campaign_approval(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        version: int,
    ) -> CampaignApprovalSnapshot | None:
        change_set_ref = f"campaign:{campaign_id}:v{version}"
        view = await self._approvals.get_by_change_set(tenant_id, change_set_ref)
        if view is None:
            fallback = await self._fallback.get_campaign_approval(
                tenant_id, campaign_id, version
            )
            return cast(CampaignApprovalSnapshot | None, fallback)
        if view.approval_type != ApprovalType.CAMPAIGN_BOUNDARY_CHANGE.value:
            raise ValidationError("Campaign 审批类型不匹配")
        states = {
            ApprovalState.PENDING.value: CampaignApprovalState.PENDING,
            ApprovalState.APPROVED.value: CampaignApprovalState.APPROVED,
            ApprovalState.APPLIED.value: CampaignApprovalState.APPROVED,
            ApprovalState.REJECTED.value: CampaignApprovalState.REJECTED,
            ApprovalState.EXPIRED.value: CampaignApprovalState.EXPIRED,
            ApprovalState.APPLY_FAILED.value: CampaignApprovalState.REJECTED,
        }
        state = states.get(view.state)
        if state is None:
            raise ValidationError("Campaign 审批状态无效")
        approved = state is CampaignApprovalState.APPROVED
        return CampaignApprovalSnapshot(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            version=version,
            approval_id=ApprovalId(view.approval_id),
            state=state,
            approved_by=(
                EmployeeId(view.decided_by_employee)
                if approved and view.decided_by_employee is not None
                else None
            ),
            approved_at=view.decided_at if approved else None,
        )


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
        async with SqlAlchemyOutreachUnitOfWork(self._factory, self._tenant_id) as uow:
            attempt = await uow.attempts.get_for_update(
                self._tenant_id, MessageAttemptId(attempt_id)
            )
            if attempt is None:
                return None
            campaign = await uow.campaigns.get(self._tenant_id, attempt.campaign_id)
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

    async def authorize(self, ctx: ToolCallContext, _state: object) -> bool:
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


class PostgresCampaignScopeResolver:
    """从 outreach_campaigns.created_by 解析员工可见 Campaign 集合（tenant-bound）。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id

    async def campaign_ids_for(
        self, *, created_by: frozenset[str]
    ) -> frozenset[CampaignId]:
        if not created_by:
            return frozenset()
        async with self._factory() as session:
            rows = await session.execute(
                select(OutreachCampaignRow.campaign_id).where(
                    OutreachCampaignRow.tenant_id == self._tenant_id,
                    OutreachCampaignRow.created_by.in_(created_by),
                )
            )
        return frozenset(CampaignId(value) for value in rows.scalars().all())


def build_phase1_dependencies(
    settings: Phase1RuntimeSettings,
    factory: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime],
    manual_send: ManualSendComposition | None = None,
    secret_resolver: SecretResolver | None = None,
    object_store_settings: S3ObjectStoreSettings | None = None,
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
    resolved_secret_resolver = secret_resolver or (
        manual_send.secret_resolver if manual_send is not None else None
    )
    if resolved_secret_resolver is None:
        raise TypeError("API 退订密钥依赖未完整配置")
    fingerprint_key = resolved_secret_resolver.resolve(
        settings.tool_call_fingerprint_key_ref
    )
    if not isinstance(fingerprint_key, str):
        raise TypeError("API 指纹依赖未完整配置")
    fingerprint_provider = HmacFingerprintProvider(
        settings.tool_call_fingerprint_key_version, fingerprint_key.encode("utf-8")
    )
    engine_ref: WorkflowEngine | None = None
    quote_domain = None
    quote_evidence = None
    generated_documents = None
    generated_metadata = None
    work_uploads = None
    if object_store_settings is not None:
        object_transport = DeferredS3ObjectBlobTransport(
            object_store_settings, resolved_secret_resolver
        )
        bounded_raw = (
            None
            if settings.quotation is None
            else S3BoundedObjectBlobTransport(
                object_store_settings,
                resolved_secret_resolver,
                limits=settings.quotation.evidence.object_read,
            )
        )
        raw_artifacts = RawArtifactStoreImpl(
            cast(
                ArtifactUnitOfWorkFactory,
                lambda requested_tenant: SqlAlchemyArtifactUnitOfWork(
                    factory, requested_tenant
                ),
            ),
            object_transport,
            object_store_settings.raw_max_bytes,
            now,
            new_id,
            bounded_transport=bounded_raw,
        )
        work_intake = WorkIntakeServiceImpl(
            cast(
                WorkIntakeUnitOfWorkFactory,
                lambda requested_tenant: SqlAlchemyWorkIntakeUnitOfWork(
                    factory, requested_tenant
                ),
            ),
            now=now,
            id_generator=new_id,
        )
        work_uploads = WorkUploadApplicationServiceImpl(
            raw_artifacts, work_intake, object_store_settings.raw_max_bytes
        )
        if settings.quotation is not None:
            quote_settings = settings.quotation
            artifact_reader = None
            if quote_settings.files is not None:
                file_limits = quote_settings.files
                generated_store = GeneratedArtifactStoreImpl(
                    cast(
                        ArtifactUnitOfWorkFactory,
                        lambda requested_tenant: SqlAlchemyArtifactUnitOfWork(
                            factory, requested_tenant
                        ),
                    ),
                    S3QuotePdfObjectBlobTransport(
                        object_store_settings,
                        resolved_secret_resolver,
                        limits=file_limits.object_write,
                    ),
                    file_limits.maximum_bytes,
                    now,
                    new_id,
                    bounded_transport=S3BoundedObjectBlobTransport(
                        object_store_settings,
                        resolved_secret_resolver,
                        limits=file_limits.object_read,
                    ),
                )
                generated_documents = GeneratedStoreDocumentAdapter(
                    generated_store, generated_store
                )
                generated_metadata = GeneratedStoreDocumentMetadataReader(
                    generated_store
                )
                artifact_reader = GeneratedStoreQuoteArtifactReader(generated_store)
            quote_evidence = build_quotation_evidence(
                factory,
                quote_settings,
                tenant_id=tenant,
                raw=RawQuoteEvidenceAdapter(raw_artifacts),
                uploads=work_intake,
                need_authorizer=build_need_unit_authorizer(
                    factory, quote_settings, tenant_id=tenant
                ),
                fingerprints=fingerprint_provider,
                lease_duration=settings.tool_lease,
                lease_owner="api-quote-source",
                now=lambda: datetime.now(UTC),
            )
            quote_domain = build_quotation_domains(
                factory,
                quote_settings,
                tenant_id=tenant,
                evidence=quote_evidence,
                run_reader=WorkflowQuoteRunReader(lambda: engine_ref),
                artifact_reader=artifact_reader,
                now=now,
            )
    approvals = ApprovalServiceImpl(
        lambda requested_tenant: SqlAlchemyApprovalUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, requested_tenant, now=now
        ),
        now=now,
        quote_access=None if quote_domain is None else quote_domain.approval_access,
    )
    organization = OrganizationServiceImpl(
        lambda requested_tenant: SqlAlchemyOrganizationUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, requested_tenant
        ),
        Phase1OrganizationAuthorizer(tenant),
        now=now,
    )
    compliance = ComplianceServiceImpl(
        lambda requested_tenant: SqlAlchemyComplianceUnitOfWork(
            factory, requested_tenant, now=now
        ),
        Phase1ComplianceAuthorizer(tenant),
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
    campaign_approvals = _ServiceBackedCampaignApprovalProvider(
        approvals,
        (
            manual_send.campaign_approvals
            if manual_send is not None
            else unavailable_send_sources  # type: ignore[arg-type]
        ),
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
    prospecting = ProspectingServiceImpl(
        lambda requested_tenant: SqlAlchemyProspectingUnitOfWork(
            factory, requested_tenant, now=now
        ),
        _DomainSeparatedContactValueHasher(fingerprint_provider),
        now=now,
    )
    demand = cast(
        DemandService,
        DemandServiceImpl(
            lambda requested_tenant: SqlAlchemyDemandUnitOfWork(  # type: ignore[arg-type, return-value]
                factory,
                requested_tenant,
                now=now,
            ),
            now=now,
            account_names=ProspectingDemandAccountNames(prospecting),
        ),
    )
    demand_radar = AuthorizedDemandRadarService(
        demand,
        employee_authorizer,
    )
    conversations = ConversationServiceImpl(
        lambda requested_tenant: SqlAlchemyConversationsUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, requested_tenant, now=now
        ),
        now=now,
    )
    commitments = CommitmentServiceImpl(
        lambda requested_tenant: SqlAlchemyCommitmentUnitOfWork(  # type: ignore[arg-type]
            factory,
            requested_tenant,
            now=now,
        ),
        now=now,
    )
    employee_system_actor = EmployeeActor(
        "system:phase1-handoff",
        EmployeeScope.SYSTEM,
        "system",
    )
    directive_employees = RequestScopedDirectiveEmployeeReader(
        employees, employee_system_actor
    )
    directives = DirectiveServiceImpl(
        lambda requested_tenant: SqlAlchemyDirectiveUnitOfWork(  # type: ignore[arg-type, return-value]
            factory,
            requested_tenant,
            now=now,
        ),
        directive_employees,
        now=now,
    )
    model_client = OpenAIJsonModelClient(
        settings.openai_api_key_ref,
        resolved_secret_resolver,
    )
    trade_manager = TradeManagerAgent(
        settings.trade_manager_model,
        StructuredTradeManagerModelPort(
            model_client,
            settings.trade_manager_model,
        ),
        None,
        CredentialMarkerGuard(),
    )
    if manual_send is None:
        manual_gateway: ToolGatewayInvoker = _UnavailableToolGateway()
        delivery_materials: DeliveryMaterialProvider = unavailable_send_sources
    else:
        gmail = _LazyGmailConnector(
            manual_send.gmail_transport,
            manual_send.secret_resolver,
            settings.gmail_oauth_token_ref,
        )
        handler = EmailSendHandler(
            gmail,
            manual_send.delivery_materials,
            unsubscribe_links,
            fingerprint_provider,
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
                "suppression": SuppressionCheck(outreach, _outreach_system_actor),
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
    campaign_scope_resolver: CampaignScopeResolver = PostgresCampaignScopeResolver(
        factory, tenant
    )
    in_app_notifications: InAppNotificationService = InAppNotificationServiceImpl(
        PostgresInAppNotificationStore(factory), now=now
    )
    dedup = PostgresNotificationDedupStore(factory, now=now)
    router = NotificationRouter(dedup, StructuredLogOnlyPolicy())
    router.register_channel(StructuredLogChannel())
    opportunity_system_actor = OpportunityActor(
        "system:phase1-handoff",
        OpportunityScope(level=ScopeLevel.SYSTEM),
        "system",
    )
    sourcing = SourcingServiceImpl(
        cast(
            Any,
            lambda requested_tenant: SqlAlchemySourcingUnitOfWork(
                factory, requested_tenant
            ),
        ),
        Phase2SourcingAuthorizer(tenant),
        _UnavailableCandidateEvidenceReader(),
        now=now,
    )
    sourcing_system_actor = SourcingActor(
        "system:api-sourcing-handoff",
        tenant,
        SourcingScope.SYSTEM,
        "system",
    )
    sourcing_definition = build_sourcing_case_definition()
    handlers = dict(
        build_human_handoff_step_handlers(
            opportunity_service=opportunities,
            employee_service=RequestScopedHandoffEmployeeReader(employees),
            notifier=RuntimeHandoffNotifier(router),
            opportunity_system_actor=opportunity_system_actor,
            employee_system_actor=employee_system_actor,
            t1=settings.t1,
            t2=settings.t2,
            now=now,
        )
    )
    scheduler_owned_wait = FixedWaitStep("scheduler_owned")
    handlers.update(
        {
            "sourcing_case.v2.check_ladder": scheduler_owned_wait,
            "sourcing_case.v2.await_public_plan": AwaitPublicPlanStep(),
            "sourcing_case.v2.public_search": scheduler_owned_wait,
            "sourcing_case.v2.verify_candidates": scheduler_owned_wait,
            "sourcing_case.v2.prepare_candidates": scheduler_owned_wait,
            "sourcing_case.v2.await_product_cards": scheduler_owned_wait,
            "sourcing_case.v2.await_review": AwaitReviewStep(),
            "sourcing_case.v2.handoff_costing": HandoffCostingStep(
                opportunities=opportunities,
                sourcing=sourcing,
                opportunity_actor=opportunity_system_actor,
                sourcing_actor=sourcing_system_actor,
            ),
        }
    )
    account_definition = build_account_discovery_definition()
    demand_definition = build_demand_discovery_definition()
    research_definition = build_demand_discovery_definition(version=2)
    playbook_definition = build_playbook_change_definition()
    country_policy_definition = build_country_policy_change_definition()
    start_only_handler = _StartOnlyWorkflowHandler()
    for step in (
        *account_definition.steps,
        *demand_definition.steps,
        *research_definition.steps,
        *playbook_definition.steps,
        *country_policy_definition.steps,
    ):
        handlers[step.handler_ref] = start_only_handler
    if quote_domain is not None and settings.quotation is not None:
        handlers.update(
            build_quote_approval_handlers(
                QuoteApprovalApplication(
                    quote_domain.quotations,
                    approvals,
                    quote_domain.context_provider,
                    CurrentQuotationActorReader(
                        SqlAlchemyQuoteEvidenceContextReader(
                            factory,
                            statement_timeout_ms=settings.quotation.core.statement_timeout_ms,
                        )
                    ),
                    now=now,
                ),
                RuntimeQuoteApprovalNotifier(router, tenant_id=tenant),
            )
        )
    workflow = PostgresWorkflowEngine(factory, handlers, now=now)
    engine_ref = workflow
    run_audit = RunAuditService(
        PostgresRunAuditRepository(factory),
        Phase1RunAuditAuthorizer(tenant),
    )
    costing = CostingServiceImpl(
        lambda requested_tenant: SqlAlchemyCostingUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, requested_tenant, now=now
        ),
        Phase1CostingAuthorizer(tenant),
        now=now,
    )
    outbox = OutboxDeliverer(
        factory,
        tenant,
        now=now,
        max_attempts=settings.outbox_max_attempts,
    )
    register_human_handoff(workflow, outbox, t1=settings.t1, t2=settings.t2)
    quotation = None
    if (
        quote_domain is not None
        and quote_evidence is not None
        and settings.quotation is not None
    ):
        register_quote_approval(workflow, outbox, approvals)
        quotation = build_quotation_http(
            quote_domain,
            factory=factory,
            approvals=approvals,
            engine=workflow,
            settings=settings.quotation,
            evidence=quote_evidence,
            generated=generated_documents,
            metadata_only=generated_metadata,
            fingerprints=fingerprint_provider,
            now=now,
        )
    workflow.register(account_definition)
    workflow.register(demand_definition)
    workflow.register(research_definition)
    workflow.register(playbook_definition)
    workflow.register(country_policy_definition)
    workflow.register(sourcing_definition)
    provider_readiness_actor = ProviderReadinessActor(
        actor_id="system:api-provider-readiness",
        tenant_id=tenant,
        permissions=frozenset({ProviderReadinessPermission.READ}),
    )
    provider_readiness = ProviderReadinessServiceImpl(
        lambda requested_tenant: SqlAlchemyProviderReadinessUnitOfWork(
            factory, requested_tenant, now=now
        ),
        runtime_actor=provider_readiness_actor,
        now=now,
    )
    from infra.db.discovery_execution import PostgresDiscoveryExecutionReader
    from infra.db.research_evidence import PostgresResearchEvidenceReader
    from infra.db.search_quota import PostgresSearchQuotaRepository

    from ..research import ResearchAccessService

    research_access = ResearchAccessService(
        tenant,
        PostgresSearchQuotaRepository(factory, tenant, now=now),
        configured=bool(settings.tavily_api_key_ref)
        and settings.tavily_exclusive_account_confirmed,
    )
    products = ProductServiceImpl(
        cast(
            Any,
            lambda requested_tenant: SqlAlchemyProductsUnitOfWork(
                factory, requested_tenant
            ),
        ),
        Phase2ProductAuthorizer(tenant),
        now=now,
    )
    sourcing_application = SourcingCaseApplication(
        sourcing=sourcing,
        quota=PostgresSearchQuotaRepository(factory, tenant, now=now),
        engine=workflow,
    )
    sourcing_admission_starter = SourcingAdmissionStarter(
        sourcing=sourcing,
        engine=workflow,
        tenant_id=tenant,
        sourcing_actor=sourcing_system_actor,
        now=now,
    )
    sourcing_admission_application = SourcingAdmissionApplication(
        sourcing=sourcing,
        policy=DirectiveSourcingAdmissionPolicyReader(directives),
        starter=sourcing_admission_starter,
        tenant_id=tenant,
        sourcing_actor=sourcing_system_actor,
        lease_duration=settings.tool_lease,
        now=now,
    )
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
        outreach_authorizer=Phase1OutreachAuthorizer(tenant),
        sending_identity_authorizer=Phase1SendingIdentityAuthorizer(tenant),
        campaign_scope_resolver=campaign_scope_resolver,
        in_app_notifications=in_app_notifications,
        employee_lookup_actor=employee_system_actor,
        provider_readiness=provider_readiness,
        provider_readiness_actor=provider_readiness_actor,
        prospecting=prospecting,
        demand_radar=demand_radar,
        directives=directives,
        trade_manager=trade_manager,
        approvals=approvals,
        organization=organization,
        compliance=compliance,
        conversations=conversations,
        commitments=commitments,
        costing=costing,
        work_uploads=work_uploads,
        run_audit=run_audit,
        research_access=research_access,
        research_execution=PostgresDiscoveryExecutionReader(factory),
        research_evidence=PostgresResearchEvidenceReader(factory),
        quotation=quotation,
        sourcing=sourcing,
        sourcing_application=sourcing_application,
        sourcing_admission_application=sourcing_admission_application,
        products=products,
    )
