"""触达域服务实现：Campaign 生命周期及后续 Enrollment/抑制共用安全边界。"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from domains.outreach.errors import (
    AccountAlreadyEnrolledError,
    CampaignApprovalRequiredError,
    CampaignNotActiveError,
    CampaignQuotaExceededError,
    ContactNotEligibleError,
    IdempotencyConflictError,
    MessageAttemptConflictError,
    OutreachProviderUnavailableError,
    ReplyAlreadyReceivedError,
    SendingIdentityUnavailableError,
    SequenceStepLimitError,
    SuppressedError,
)
from domains.outreach.models import (
    ActionRecord,
    Campaign,
    CampaignBoundary,
    CampaignState,
    CampaignVersion,
    Enrollment,
    EnrollmentState,
    EnrollmentStopReason,
    MessageAttempt,
    MessageAttemptState,
    SendFailureCategory,
    SequenceStepSpec,
    SuppressionEntry,
    SuppressionReason,
)
from domains.outreach.permissions import (
    Actor,
    AuditLogger,
    OutreachAction,
    OutreachAuthorizer,
    OutreachScope,
)
from domains.outreach.repository import (
    AppendStatus,
    DeliveryCorrelationBindStatus,
    EnrollmentInsertStatus,
    OutreachUnitOfWork,
    OutreachUnitOfWorkFactory,
    QuotaReservationStatus,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignBoundaryView,
    CampaignCreateRequest,
    CampaignView,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    DeliveryCorrelationBinding,
    DeliveryCorrelationLookup,
    DeliveryFeedbackTarget,
    EnrollmentCreateRequest,
    EnrollmentView,
    MessageAttemptView,
    MessageSendPreflight,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    SuppressionRequest,
    SuppressionResult,
    SuppressionTarget,
    SuppressionView,
)
from domains.outreach.service import (
    CampaignApprovalProvider,
    ContactEligibilityProvider,
    ReplyStatusProvider,
    SendingIdentityEligibilityProvider,
)
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    TransientError,
    ValidationError,
)
from shared.events.catalog import MessageSent, SuppressionAdded
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    SendingIdentityId,
    SuppressionId,
    TenantId,
    new_id,
)

_tenant_logger = logging.getLogger("security.tenant_isolation")


class OutreachServiceImpl:
    """以授权优先、单 UoW 写入、提交后 allow 审计实现触达业务。"""

    def __init__(
        self,
        uow_factory: OutreachUnitOfWorkFactory,
        contact_eligibility: ContactEligibilityProvider,
        sending_identity_eligibility: SendingIdentityEligibilityProvider,
        approval_provider: CampaignApprovalProvider,
        reply_status_provider: ReplyStatusProvider,
        authorizer: OutreachAuthorizer,
        audit: AuditLogger,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._contacts = contact_eligibility
        self._senders = sending_identity_eligibility
        self._approvals = approval_provider
        self._replies = reply_status_provider
        self._authorizer = authorizer
        self._audit = audit
        self._now = now if now is not None else lambda: datetime.now(UTC)

    def _deny(self, actor: object, action: OutreachAction, tenant_id: TenantId) -> None:
        typed = actor if isinstance(actor, Actor) else None
        self._audit.log(
            actor=typed.actor_id if typed else "unknown",
            action=action.value,
            tenant_id=tenant_id,
            scope=typed.scope.label if typed else "none",
            rule="deny:authorization",
        )

    def _preauthorize(
        self, actor: object, action: OutreachAction, tenant_id: TenantId
    ) -> tuple[Actor, str]:
        if not isinstance(actor, Actor):
            self._deny(actor, action, tenant_id)
            raise PermissionDenied("Phase 1 触达授权拒绝")
        try:
            rule = self._authorizer.preauthorize(
                actor, action, actor.scope, tenant_id
            )
        except PermissionDenied:
            self._deny(actor, action, tenant_id)
            raise
        return actor, rule

    def _require(
        self,
        actor: Actor,
        action: OutreachAction,
        tenant_id: TenantId,
        campaign_id: CampaignId | None = None,
        account_id: ProspectAccountId | None = None,
        enrollment_id: EnrollmentId | None = None,
        suppression_target: str | None = None,
        suppression_reason: SuppressionReason | None = None,
        attempt_id: MessageAttemptId | None = None,
        sending_identity_id: SendingIdentityId | None = None,
    ) -> str:
        try:
            return self._authorizer.require(
                actor,
                action,
                actor.scope,
                tenant_id,
                campaign_id=campaign_id,
                account_id=account_id,
                enrollment_id=enrollment_id,
                suppression_target=suppression_target,
                suppression_reason=suppression_reason,
                attempt_id=attempt_id,
                sending_identity_id=sending_identity_id,
            )
        except PermissionDenied:
            self._deny(actor, action, tenant_id)
            raise

    def _allow(
        self,
        actor: Actor,
        action: OutreachAction,
        tenant_id: TenantId,
        rule: str,
    ) -> None:
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=actor.scope.label,
            rule=rule,
        )

    def _tenant_violation(
        self, actor: Actor, action: OutreachAction, tenant_id: TenantId
    ) -> None:
        self._deny(actor, action, tenant_id)
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={
                "actor": actor.actor_id,
                "action": action.value,
                "tenant_id": str(tenant_id),
                "scope": actor.scope.label,
                "rule": "deny:tenant_isolation",
            },
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")

    @staticmethod
    def _boundary(request: CampaignCreateRequest) -> CampaignBoundary:
        if not isinstance(request, CampaignCreateRequest):
            raise ValidationError("Campaign request 无效")
        return CampaignBoundary(
            markets=request.markets,
            target_entity_types=request.target_entity_types,
            allowed_categories=request.allowed_categories,
            sender_identity_ids=request.sender_identity_ids,
            steps=tuple(
                SequenceStepSpec(step.step_number, step.intent, step.wait_days)
                for step in request.steps
            ),
            daily_new_contact_limit=request.daily_new_contact_limit,
            daily_total_message_limit=request.daily_total_message_limit,
            handoff_triggers=request.handoff_triggers,
            stop_on_reply=request.stop_on_reply,
        )

    @staticmethod
    def _validate_now(value: datetime) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValidationError("服务时钟必须为 UTC")
        return value

    async def _validate_senders(
        self,
        tenant_id: TenantId,
        boundary: CampaignBoundary,
        actor: Actor,
        action: OutreachAction,
    ) -> None:
        for identity_id in boundary.sender_identity_ids:
            try:
                snapshot = await self._senders.get_sending_identity_eligibility(
                    tenant_id, identity_id
                )
            except (TransientError, OSError, RuntimeError):
                raise OutreachProviderUnavailableError("发件身份资格暂不可用") from None
            if not isinstance(snapshot, SendingIdentityEligibilitySnapshot):
                raise SendingIdentityUnavailableError("Campaign 发件身份不符合资格")
            if snapshot.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if (
                snapshot.identity_id != identity_id
                or snapshot.role is not OutreachSenderRole.COLD_OUTREACH
                or snapshot.authentication_passed is not True
            ):
                raise SendingIdentityUnavailableError("Campaign 发件身份不符合资格")

    async def _view(
        self,
        uow: OutreachUnitOfWork,
        tenant_id: TenantId,
        campaign: Campaign,
        on_day,
    ) -> CampaignView:
        version = await uow.campaigns.get_version(
            tenant_id, campaign.campaign_id, campaign.current_version
        )
        if version is None:
            raise ValidationError("Campaign 当前版本不存在")
        usage = await uow.quotas.get_usage(
            tenant_id, campaign.campaign_id, on_day
        )
        boundary = version.boundary
        return CampaignView(
            tenant_id=tenant_id,
            campaign_id=campaign.campaign_id,
            name=version.name,
            state=campaign.state,
            version=version.version,
            boundary=CampaignBoundaryView(
                markets=tuple(boundary.markets),
                target_entity_types=tuple(boundary.target_entity_types),
                allowed_categories=tuple(boundary.allowed_categories),
                sender_identity_ids=tuple(boundary.sender_identity_ids),
                steps=tuple(
                    SequenceStepRequest(step.step_number, step.intent, step.wait_days)
                    for step in boundary.steps
                ),
                daily_new_contact_limit=boundary.daily_new_contact_limit,
                daily_total_message_limit=boundary.daily_total_message_limit,
                handoff_triggers=tuple(boundary.handoff_triggers),
                stop_on_reply=boundary.stop_on_reply,
            ),
            approval_id=(
                ApprovalId(campaign.approval_id) if campaign.approval_id else None
            ),
            approved_by=campaign.approved_by,
            approved_at=campaign.approved_at,
            paused_reason=campaign.paused_reason,
            created_by=campaign.created_by,
            created_at=campaign.created_at,
            today_new_contacts_reserved=usage.new_contacts_reserved,
            today_messages_reserved=usage.messages_reserved,
        )

    @staticmethod
    def _action(
        tenant_id: TenantId,
        actor: Actor,
        entity_id: str,
        key: str,
        action: OutreachAction,
        occurred_at: datetime,
    ) -> ActionRecord:
        return ActionRecord(
            tenant_id=tenant_id,
            action_id=new_id("act"),
            action_key=key,
            action=action.value,
            entity_id=str(entity_id),
            actor_id=actor.actor_id,
            occurred_at=occurred_at,
        )

    async def create_campaign(
        self, tenant_id: TenantId, request: CampaignCreateRequest, *, actor: Actor
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_CREATE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        boundary = self._boundary(request)
        campaign_id = CampaignId(new_id("cmp"))
        rule = self._require(
            actor, action, tenant_id, campaign_id
        )
        await self._validate_senders(tenant_id, boundary, actor, action)
        now = self._validate_now(self._now())
        campaign = Campaign(
            tenant_id,
            campaign_id,
            CampaignState.DRAFT,
            1,
            EmployeeId(actor.actor_id),
            now,
        )
        version = CampaignVersion(
            tenant_id, campaign_id, 1, request.name, boundary, EmployeeId(actor.actor_id), now
        )
        async with self._uow_factory(tenant_id) as uow:
            await uow.campaigns.add(campaign, version)
            await uow.actions.append(
                self._action(
                    tenant_id,
                    actor,
                    campaign_id,
                    f"campaign:{campaign_id}:v1:create",
                    action,
                    now,
                )
            )
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def _locked_campaign(
        self,
        uow: OutreachUnitOfWork,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        actor: Actor,
        action: OutreachAction,
    ) -> Campaign:
        campaign = await uow.campaigns.get_for_update(tenant_id, campaign_id)
        if campaign is None:
            raise ValidationError("Campaign 不存在")
        if campaign.tenant_id != tenant_id:
            self._tenant_violation(actor, action, tenant_id)
        if campaign.campaign_id != campaign_id:
            raise ValidationError("Campaign 数据损坏")
        return campaign

    async def submit_campaign(
        self, tenant_id: TenantId, campaign_id: CampaignId, *, actor: Actor
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_SUBMIT
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, campaign_id, actor, action
            )
            rule = self._require(actor, action, tenant_id, campaign_id=campaign_id)
            if campaign.state is CampaignState.DRAFT:
                campaign.transition_to(CampaignState.PENDING_APPROVAL)
                await uow.campaigns.update(campaign)
            elif campaign.state is not CampaignState.PENDING_APPROVAL:
                raise InvalidStateTransition("Campaign 当前状态不能提交审批")
            await uow.actions.append(self._action(tenant_id, actor, campaign_id, f"campaign:{campaign_id}:v{campaign.current_version}:submit", action, now))
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def revise_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        request: CampaignCreateRequest,
        *,
        actor: Actor,
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_REVISE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        boundary = self._boundary(request)
        await self._validate_senders(tenant_id, boundary, actor, action)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, campaign_id, actor, action
            )
            rule = self._require(actor, action, tenant_id, campaign_id=campaign_id)
            if campaign.state in {CampaignState.COMPLETED, CampaignState.CANCELLED}:
                raise InvalidStateTransition("终态 Campaign 不能修订")
            next_version = campaign.current_version + 1
            version = CampaignVersion(tenant_id, campaign_id, next_version, request.name, boundary, EmployeeId(actor.actor_id), now)
            await uow.campaigns.append_version(version)
            if campaign.state is not CampaignState.PENDING_APPROVAL:
                campaign.transition_to(CampaignState.PENDING_APPROVAL)
            campaign.current_version = next_version
            campaign.approval_id = None
            campaign.approved_by = None
            campaign.approved_at = None
            campaign.paused_reason = None
            await uow.campaigns.update(campaign)
            await uow.actions.append(self._action(tenant_id, actor, campaign_id, f"campaign:{campaign_id}:v{next_version}:revise", action, now))
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def activate_campaign(
        self, tenant_id: TenantId, campaign_id: CampaignId, *, actor: Actor
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_ACTIVATE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, campaign_id, actor, action
            )
            rule = self._require(actor, action, tenant_id, campaign_id=campaign_id)
            approval = await self._approvals.get_campaign_approval(tenant_id, campaign_id, campaign.current_version)
            if approval is not None and not isinstance(
                approval, CampaignApprovalSnapshot
            ):
                raise CampaignApprovalRequiredError("Campaign 当前版本缺少有效审批")
            if approval is not None and approval.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if (
                approval is None
                or approval.campaign_id != campaign_id
                or approval.version != campaign.current_version
                or approval.state is not CampaignApprovalState.APPROVED
            ):
                raise CampaignApprovalRequiredError("Campaign 当前版本缺少有效审批")
            if campaign.state is CampaignState.ACTIVE:
                if campaign.approval_id != approval.approval_id:
                    raise IdempotencyConflictError("Campaign activation 审批冲突")
            elif campaign.state in {CampaignState.PENDING_APPROVAL, CampaignState.PAUSED}:
                campaign.transition_to(CampaignState.ACTIVE)
                campaign.approval_id = str(approval.approval_id)
                campaign.approved_by = approval.approved_by
                campaign.approved_at = approval.approved_at
                campaign.paused_reason = None
                await uow.campaigns.update(campaign)
            else:
                raise InvalidStateTransition("Campaign 当前状态不能激活")
            await uow.actions.append(self._action(tenant_id, actor, campaign_id, f"campaign:{campaign_id}:v{campaign.current_version}:activate:{approval.approval_id}", action, now))
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def pause_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        reason: str,
        *,
        actor: Actor,
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_PAUSE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        lowered_reason = reason.lower() if isinstance(reason, str) else ""
        if (
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 200
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in reason)
            or "://" in reason
            or "@" in reason
            or any(
                marker in lowered_reason
                for marker in ("bearer", "token", "secret", "password")
            )
        ):
            raise ValidationError("Campaign 暂停原因无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, campaign_id, actor, action
            )
            rule = self._require(actor, action, tenant_id, campaign_id=campaign_id)
            if campaign.state is CampaignState.PAUSED:
                if campaign.paused_reason != reason:
                    raise IdempotencyConflictError("Campaign 暂停原因冲突")
            else:
                campaign.transition_to(CampaignState.PAUSED)
                campaign.paused_reason = reason
                await uow.campaigns.update(campaign)
            await uow.actions.append(self._action(tenant_id, actor, campaign_id, f"campaign:{campaign_id}:v{campaign.current_version}:pause", action, now))
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def cancel_campaign(
        self, tenant_id: TenantId, campaign_id: CampaignId, *, actor: Actor
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_CANCEL
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, campaign_id, actor, action
            )
            rule = self._require(actor, action, tenant_id, campaign_id=campaign_id)
            if campaign.state is not CampaignState.CANCELLED:
                campaign.transition_to(CampaignState.CANCELLED)
                await uow.campaigns.update(campaign)
            await uow.actions.append(self._action(tenant_id, actor, campaign_id, f"campaign:{campaign_id}:v{campaign.current_version}:cancel", action, now))
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def get_campaign(
        self, tenant_id: TenantId, campaign_id: CampaignId, *, actor: Actor
    ) -> CampaignView:
        action = OutreachAction.CAMPAIGN_READ
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await uow.campaigns.get(tenant_id, campaign_id)
            if campaign is None:
                raise ValidationError("Campaign 不存在")
            if campaign.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if campaign.campaign_id != campaign_id:
                raise ValidationError("Campaign 数据损坏")
            rule = self._require(actor, action, tenant_id, campaign_id=campaign_id)
            view = await self._view(uow, tenant_id, campaign, now.date())
        self._allow(actor, action, tenant_id, rule)
        return view

    async def list_campaigns(
        self,
        tenant_id: TenantId,
        scope: OutreachScope,
        *,
        limit: int,
        actor: Actor,
    ) -> list[CampaignView]:
        action = OutreachAction.CAMPAIGN_LIST
        actor, pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(scope, OutreachScope) or scope is not actor.scope:
            self._deny(actor, action, tenant_id)
            raise PermissionDenied("Phase 1 触达授权拒绝")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValidationError("limit 无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaigns = await uow.campaigns.list_scoped(tenant_id, scope, limit)
            views: list[CampaignView] = []
            rules: list[str] = []
            for campaign in campaigns:
                if campaign.tenant_id != tenant_id:
                    self._tenant_violation(actor, action, tenant_id)
                rules.append(self._require(actor, action, tenant_id, campaign_id=campaign.campaign_id))
                views.append(await self._view(uow, tenant_id, campaign, now.date()))
        rule = rules[0] if rules else pre_rule
        self._allow(actor, action, tenant_id, rule)
        return views

    @staticmethod
    def _enrollment_view(enrollment: Enrollment) -> EnrollmentView:
        return EnrollmentView(
            tenant_id=enrollment.tenant_id,
            enrollment_id=enrollment.enrollment_id,
            campaign_id=enrollment.campaign_id,
            campaign_version=enrollment.campaign_version,
            account_id=enrollment.account_id,
            contact_point_id=enrollment.contact_point_id,
            sending_identity_id=enrollment.sending_identity_id,
            state=enrollment.state,
            current_step=enrollment.current_step,
            next_send_at=enrollment.next_send_at,
            enrolled_at=enrollment.enrolled_at,
            stopped_at=enrollment.stopped_at,
            stop_reason=enrollment.stop_reason,
        )

    @staticmethod
    def _attempt_view(attempt: MessageAttempt) -> MessageAttemptView:
        return MessageAttemptView(
            tenant_id=attempt.tenant_id,
            attempt_id=attempt.attempt_id,
            message_id=attempt.message_id,
            campaign_id=attempt.campaign_id,
            enrollment_id=attempt.enrollment_id,
            campaign_version=attempt.campaign_version,
            step_number=attempt.step_number,
            sending_identity_id=attempt.sending_identity_id,
            idempotency_key=attempt.idempotency_key,
            state=attempt.state,
            provider_ref=attempt.provider_ref,
            failure_category=attempt.failure_category,
            created_at=attempt.created_at,
            updated_at=attempt.updated_at,
            send_claimed_at=attempt.send_claimed_at,
            deterministic_message_id=attempt.deterministic_message_id,
            idempotency_header=attempt.idempotency_header,
        )

    async def bind_delivery_correlation(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        binding: DeliveryCorrelationBinding,
        *,
        actor: Actor,
    ) -> MessageAttemptView:
        action = OutreachAction.MESSAGE_DELIVERY_BIND
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(binding, DeliveryCorrelationBinding):
            raise ValidationError("delivery correlation binding 无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            attempt = await uow.attempts.get_for_update(tenant_id, attempt_id)
            if attempt is None:
                raise ValidationError("Message Attempt 不存在")
            if attempt.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if attempt.attempt_id != attempt_id:
                raise ValidationError("Message Attempt 数据损坏")
            rule = self._require(
                actor,
                action,
                tenant_id,
                attempt_id=attempt.attempt_id,
            )
            try:
                attempt.bind_delivery_correlation(binding)
            except InvalidStateTransition as exc:
                raise MessageAttemptConflictError(
                    "Message Attempt delivery correlation 冲突"
                ) from exc
            bound = await uow.attempts.bind_delivery_correlation(attempt)
            if (
                bound.status is DeliveryCorrelationBindStatus.CONFLICT
                or bound.winner is None
            ):
                raise MessageAttemptConflictError(
                    "Message Attempt delivery correlation 冲突"
                )
            winner = bound.winner
            if winner.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if (
                winner.attempt_id != attempt_id
                or winner.deterministic_message_id
                != binding.deterministic_message_id
                or winner.idempotency_header != binding.idempotency_header
            ):
                raise MessageAttemptConflictError(
                    "Message Attempt delivery correlation 冲突"
                )
            if bound.status is DeliveryCorrelationBindStatus.BOUND:
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(attempt_id),
                        f"attempt:{attempt_id}:delivery-bind",
                        action,
                        now,
                    )
                )
            view = self._attempt_view(winner)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def resolve_delivery_feedback(
        self,
        tenant_id: TenantId,
        lookup: DeliveryCorrelationLookup,
        *,
        actor: Actor,
    ) -> DeliveryFeedbackTarget | None:
        action = OutreachAction.DELIVERY_FEEDBACK_RESOLVE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(lookup, DeliveryCorrelationLookup):
            raise ValidationError("delivery correlation lookup 无效")
        async with self._uow_factory(tenant_id) as uow:
            message_attempt = (
                await uow.attempts.find_by_deterministic_message_id(
                    tenant_id, lookup.deterministic_message_id
                )
                if lookup.deterministic_message_id is not None
                else None
            )
            header_attempt = (
                await uow.attempts.find_by_idempotency_header(
                    tenant_id, lookup.idempotency_header
                )
                if lookup.idempotency_header is not None
                else None
            )
            if (
                lookup.deterministic_message_id is not None
                and lookup.idempotency_header is not None
                and (message_attempt is None or header_attempt is None)
            ):
                target = None
                rule = None
            else:
                attempts = tuple(
                    value
                    for value in (message_attempt, header_attempt)
                    if value is not None
                )
                if not attempts:
                    target = None
                    rule = None
                elif any(
                    value.attempt_id != attempts[0].attempt_id
                    for value in attempts[1:]
                ):
                    raise MessageAttemptConflictError(
                        "delivery correlation 指向不同 Message Attempt"
                    )
                else:
                    attempt = attempts[0]
                    if attempt.tenant_id != tenant_id:
                        self._tenant_violation(actor, action, tenant_id)
                    if (
                        lookup.deterministic_message_id is not None
                        and attempt.deterministic_message_id
                        != lookup.deterministic_message_id
                    ) or (
                        lookup.idempotency_header is not None
                        and attempt.idempotency_header
                        != lookup.idempotency_header
                    ):
                        raise ValidationError("delivery correlation 查询结果损坏")
                    if attempt.state is not MessageAttemptState.SENT:
                        raise ValidationError("delivery feedback Attempt 尚未发送")
                    enrollment = await uow.enrollments.get(
                        tenant_id, attempt.enrollment_id
                    )
                    if enrollment is None:
                        raise ValidationError("delivery feedback Enrollment 不存在")
                    if enrollment.tenant_id != tenant_id:
                        self._tenant_violation(actor, action, tenant_id)
                    if (
                        enrollment.enrollment_id != attempt.enrollment_id
                        or enrollment.campaign_id != attempt.campaign_id
                        or enrollment.sending_identity_id
                        != attempt.sending_identity_id
                    ):
                        raise ValidationError("delivery feedback 资源绑定损坏")
                    rule = self._require(
                        actor,
                        action,
                        tenant_id,
                        sending_identity_id=attempt.sending_identity_id,
                    )
                    target = DeliveryFeedbackTarget(
                        tenant_id=tenant_id,
                        attempt_id=attempt.attempt_id,
                        enrollment_id=enrollment.enrollment_id,
                        account_id=enrollment.account_id,
                        contact_point_id=enrollment.contact_point_id,
                        sending_identity_id=attempt.sending_identity_id,
                    )
        if target is None or rule is None:
            return None
        self._allow(actor, action, tenant_id, rule)
        return target

    @staticmethod
    def _validate_provider_event_id(value: object) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValidationError("provider_event_id 无效")
        return value

    async def apply_hard_bounce(
        self,
        tenant_id: TenantId,
        target: DeliveryFeedbackTarget,
        provider_event_id: str,
        occurred_at: datetime,
        *,
        actor: Actor,
    ) -> SuppressionResult:
        action = OutreachAction.HARD_BOUNCE_APPLY
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(target, DeliveryFeedbackTarget):
            raise ValidationError("delivery feedback target 无效")
        event_id = self._validate_provider_event_id(provider_event_id)
        occurred = self._validate_now(occurred_at)
        now = self._validate_now(self._now())
        if occurred > now + timedelta(minutes=5):
            raise ValidationError("delivery feedback occurred_at 超出允许时间")
        suppression_target = SuppressionTarget(
            contact_point_id=target.contact_point_id
        )
        request = SuppressionRequest(
            target=suppression_target,
            reason=SuppressionReason.HARD_BOUNCE,
            occurred_at=occurred,
            source_ref=event_id,
            idempotency_key=IdempotencyKey(f"feedback:{event_id}"),
        )
        entry = SuppressionEntry(
            tenant_id=tenant_id,
            suppression_id=SuppressionId(new_id("sup")),
            target=suppression_target,
            reason=SuppressionReason.HARD_BOUNCE,
            occurred_at=occurred,
            source_ref=event_id,
            idempotency_key=request.idempotency_key,
            created_at=now,
        )
        async with self._uow_factory(tenant_id) as uow:
            attempt = await uow.attempts.get_for_update(
                tenant_id, target.attempt_id
            )
            enrollment = await uow.enrollments.get_for_update(
                tenant_id, target.enrollment_id
            )
            if attempt is None or enrollment is None:
                raise ValidationError("delivery feedback 资源不存在")
            if attempt.tenant_id != tenant_id or enrollment.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if (
                target.tenant_id != tenant_id
                or attempt.attempt_id != target.attempt_id
                or attempt.enrollment_id != target.enrollment_id
                or attempt.sending_identity_id != target.sending_identity_id
                or enrollment.enrollment_id != target.enrollment_id
                or enrollment.account_id != target.account_id
                or enrollment.contact_point_id != target.contact_point_id
                or enrollment.sending_identity_id != target.sending_identity_id
                or attempt.state is not MessageAttemptState.SENT
            ):
                raise ValidationError("delivery feedback 资源绑定不匹配")
            rule = self._require(
                actor,
                action,
                tenant_id,
                sending_identity_id=target.sending_identity_id,
            )
            appended = await uow.suppressions.append_if_absent(entry)
            if appended.status is AppendStatus.CONFLICT or appended.winner is None:
                raise IdempotencyConflictError(
                    "hard bounce feedback 已绑定不同内容"
                )
            winner = appended.winner
            if winner.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if not self._same_suppression_payload(winner, request, tenant_id):
                raise IdempotencyConflictError(
                    "hard bounce feedback 已绑定不同内容"
                )
            if appended.status is AppendStatus.EXISTING:
                result = SuppressionResult(
                    False,
                    self._suppression_view(winner),
                    0,
                )
            else:
                enrollments = await uow.enrollments.lock_matching_active(
                    tenant_id, suppression_target
                )
                ordered = sorted(
                    enrollments, key=lambda value: value.enrollment_id
                )
                for matched in ordered:
                    if matched.tenant_id != tenant_id:
                        self._tenant_violation(actor, action, tenant_id)
                    if matched.contact_point_id != target.contact_point_id:
                        raise ValidationError("hard bounce Enrollment 查询结果损坏")
                    matched.next_send_at = None
                    matched.transition_to(
                        EnrollmentState.STOPPED_BOUNCED,
                        at=now,
                        reason=EnrollmentStopReason.HARD_BOUNCE,
                    )
                    await uow.enrollments.update(matched)
                    await uow.actions.append(
                        self._action(
                            tenant_id,
                            actor,
                            str(matched.enrollment_id),
                            f"feedback:{event_id}:enrollment:{matched.enrollment_id}:stop",
                            action,
                            now,
                        )
                    )
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(winner.suppression_id),
                        f"feedback:{event_id}:hard-bounce",
                        action,
                        now,
                    )
                )
                await uow.bus.publish(
                    SuppressionAdded(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        scope=winner.target.scope.value,
                        target_id=winner.target.canonical_id,
                        reason=winner.reason.value,
                    )
                )
                result = SuppressionResult(
                    True,
                    self._suppression_view(winner),
                    len(ordered),
                )
        if not result.created:
            return result
        self._allow(actor, action, tenant_id, rule)
        return result

    async def _contact_snapshot(
        self,
        tenant_id: TenantId,
        contact_point_id,
        account_id,
    ) -> ContactEligibilitySnapshot:
        try:
            snapshot = await self._contacts.get_contact_eligibility(
                tenant_id, contact_point_id, account_id
            )
        except (TransientError, OSError, RuntimeError):
            raise OutreachProviderUnavailableError(
                "联系人资格暂不可用"
            ) from None
        if not isinstance(snapshot, ContactEligibilitySnapshot):
            raise ContactNotEligibleError("联系人当前资格不满足 Campaign 边界")
        return snapshot

    def _validate_contact_snapshot(
        self,
        snapshot: ContactEligibilitySnapshot,
        *,
        tenant_id: TenantId,
        contact_point_id,
        account_id,
        boundary: CampaignBoundary,
        actor: Actor,
        action: OutreachAction,
    ) -> None:
        if snapshot.tenant_id != tenant_id:
            self._tenant_violation(actor, action, tenant_id)
        if (
            snapshot.contact_point_id != contact_point_id
            or snapshot.account_id != account_id
            or snapshot.verification is not ContactVerificationStatus.VERIFIED
            or snapshot.verified_at is None
            or not isinstance(snapshot.legal_basis, ContactLegalBasis)
            or snapshot.contact_belongs_to_account is not True
            or snapshot.country not in boundary.markets
            or snapshot.entity_type not in boundary.target_entity_types
            or not set(snapshot.qualified_categories).intersection(
                boundary.allowed_categories
            )
        ):
            raise ContactNotEligibleError(
                "联系人当前资格不满足 Campaign 边界"
            )

    async def _sender_snapshot(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        actor: Actor,
        action: OutreachAction,
    ) -> SendingIdentityEligibilitySnapshot:
        try:
            snapshot = await self._senders.get_sending_identity_eligibility(
                tenant_id, identity_id
            )
        except (TransientError, OSError, RuntimeError):
            raise OutreachProviderUnavailableError(
                "发件身份资格暂不可用"
            ) from None
        if not isinstance(snapshot, SendingIdentityEligibilitySnapshot):
            raise SendingIdentityUnavailableError("发件身份当前不可用")
        if snapshot.tenant_id != tenant_id:
            self._tenant_violation(actor, action, tenant_id)
        if snapshot.identity_id != identity_id:
            raise SendingIdentityUnavailableError("发件身份当前不可用")
        return snapshot

    @staticmethod
    def _sender_is_eligible(snapshot: SendingIdentityEligibilitySnapshot) -> bool:
        return (
            snapshot.role is OutreachSenderRole.COLD_OUTREACH
            and snapshot.authentication_passed is True
            and snapshot.sendable is True
            and snapshot.remaining_slots > 0
        )

    async def _has_suppression(
        self,
        uow: OutreachUnitOfWork,
        tenant_id: TenantId,
        contact_point_id,
        account_id,
    ) -> bool:
        contact = await uow.suppressions.find_current(
            tenant_id, SuppressionTarget(contact_point_id=contact_point_id)
        )
        account = await uow.suppressions.find_current(
            tenant_id, SuppressionTarget(account_id=account_id)
        )
        return contact is not None or account is not None

    async def enroll(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        request: EnrollmentCreateRequest,
        *,
        actor: Actor,
    ) -> EnrollmentView:
        action = OutreachAction.ENROLLMENT_CREATE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(request, EnrollmentCreateRequest):
            raise ValidationError("Enrollment request 无效")
        snapshot = await self._contact_snapshot(
            tenant_id, request.contact_point_id, request.account_id
        )
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, campaign_id, actor, action
            )
            version = await uow.campaigns.get_version(
                tenant_id, campaign_id, campaign.current_version
            )
            if version is None:
                raise ValidationError("Campaign 当前版本不存在")
            rule = self._require(
                actor,
                action,
                tenant_id,
                campaign_id=campaign_id,
                account_id=request.account_id,
            )
            if campaign.state is not CampaignState.ACTIVE:
                raise CampaignNotActiveError("Campaign 当前状态不允许入组")
            self._validate_contact_snapshot(
                snapshot,
                tenant_id=tenant_id,
                contact_point_id=request.contact_point_id,
                account_id=request.account_id,
                boundary=version.boundary,
                actor=actor,
                action=action,
            )
            existing = await uow.enrollments.get_by_key(
                tenant_id, request.idempotency_key
            )
            if existing is not None:
                if existing.tenant_id != tenant_id:
                    self._tenant_violation(actor, action, tenant_id)
                if (
                    existing.campaign_id == campaign_id
                    and existing.account_id == request.account_id
                    and existing.contact_point_id == request.contact_point_id
                ):
                    view = self._enrollment_view(existing)
                else:
                    raise IdempotencyConflictError(
                        "Enrollment 幂等键已绑定不同内容"
                    )
            else:
                active = await uow.enrollments.find_active_for_account(
                    tenant_id, request.account_id
                )
                if active is not None:
                    raise AccountAlreadyEnrolledError(
                        "该企业已有活跃 Enrollment"
                    )
                if await self._has_suppression(
                    uow,
                    tenant_id,
                    request.contact_point_id,
                    request.account_id,
                ):
                    raise SuppressedError("联系人或企业已进入全局抑制")
                quota = await uow.quotas.reserve_new_contact(
                    tenant_id,
                    campaign_id,
                    now.date(),
                    version.boundary.daily_new_contact_limit,
                )
                if quota.status is QuotaReservationStatus.CAP_REACHED:
                    raise CampaignQuotaExceededError("Campaign 当日新联系人额度已满")
                senders = tuple(sorted(version.boundary.sender_identity_ids))
                winner: SendingIdentityId | None = None
                winner_index = -1
                for offset in range(1, len(senders) + 1):
                    index = (campaign.round_robin_cursor + offset) % len(senders)
                    candidate = senders[index]
                    candidate_snapshot = await self._sender_snapshot(
                        tenant_id, candidate, actor, action
                    )
                    if self._sender_is_eligible(candidate_snapshot):
                        winner = candidate
                        winner_index = index
                        break
                if winner is None:
                    raise SendingIdentityUnavailableError(
                        "没有满足 Campaign 边界的可用发件身份"
                    )
                enrollment = Enrollment(
                    tenant_id=tenant_id,
                    enrollment_id=EnrollmentId(new_id("enr")),
                    campaign_id=campaign_id,
                    campaign_version=version.version,
                    account_id=request.account_id,
                    contact_point_id=request.contact_point_id,
                    sending_identity_id=winner,
                    state=EnrollmentState.ENROLLED,
                    current_step=0,
                    next_send_at=now,
                    enrolled_at=now,
                    stopped_at=None,
                    stop_reason=None,
                    idempotency_key=request.idempotency_key,
                )
                campaign.round_robin_cursor = winner_index
                await uow.campaigns.update(campaign)
                inserted = await uow.enrollments.insert_if_absent(enrollment)
                if inserted.status is EnrollmentInsertStatus.IDEMPOTENCY_CONFLICT:
                    raise IdempotencyConflictError(
                        "Enrollment 幂等键已绑定不同内容"
                    )
                if inserted.status is EnrollmentInsertStatus.ACCOUNT_CONFLICT:
                    raise AccountAlreadyEnrolledError(
                        "该企业已有活跃 Enrollment"
                    )
                if inserted.winner is None:
                    raise ValidationError("Enrollment 原子写入结果损坏")
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(inserted.winner.enrollment_id),
                        f"enrollment:{inserted.winner.enrollment_id}:create",
                        action,
                        now,
                    )
                )
                view = self._enrollment_view(inserted.winner)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def _locate_enrollment(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> Enrollment:
        async with self._uow_factory(tenant_id) as uow:
            enrollment = await uow.enrollments.get(tenant_id, enrollment_id)
        if enrollment is None:
            raise ValidationError("Enrollment 不存在")
        return enrollment

    async def _locate_attempt(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId
    ) -> MessageAttempt:
        async with self._uow_factory(tenant_id) as uow:
            attempt = await uow.attempts.get_for_update(tenant_id, attempt_id)
        if attempt is None:
            raise ValidationError("Message Attempt 不存在")
        return attempt

    async def _require_approved_version(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        version: int,
        actor: Actor,
        action: OutreachAction,
    ) -> CampaignApprovalSnapshot:
        try:
            approval = await self._approvals.get_campaign_approval(
                tenant_id, campaign_id, version
            )
        except (TransientError, OSError, RuntimeError):
            raise OutreachProviderUnavailableError("Campaign 审批事实暂不可用") from None
        if approval is not None and not isinstance(
            approval, CampaignApprovalSnapshot
        ):
            raise CampaignApprovalRequiredError("Enrollment 版本缺少有效审批")
        if approval is not None and approval.tenant_id != tenant_id:
            self._tenant_violation(actor, action, tenant_id)
        if (
            approval is None
            or approval.campaign_id != campaign_id
            or approval.version != version
            or approval.state is not CampaignApprovalState.APPROVED
        ):
            raise CampaignApprovalRequiredError("Enrollment 版本缺少有效审批")
        return approval

    async def _reply_snapshot(
        self,
        tenant_id: TenantId,
        enrollment: Enrollment,
        actor: Actor,
        action: OutreachAction,
    ) -> ReplyStatusSnapshot:
        try:
            snapshot = await self._replies.get_reply_status(
                tenant_id, enrollment.contact_point_id, enrollment.account_id
            )
        except (TransientError, OSError, RuntimeError):
            raise OutreachProviderUnavailableError("回复状态暂不可用") from None
        if not isinstance(snapshot, ReplyStatusSnapshot):
            raise OutreachProviderUnavailableError("回复状态暂不可用")
        if snapshot.tenant_id != tenant_id:
            self._tenant_violation(actor, action, tenant_id)
        if (
            snapshot.contact_point_id != enrollment.contact_point_id
            or snapshot.account_id != enrollment.account_id
        ):
            raise ValidationError("回复状态资源不匹配")
        return snapshot

    async def prepare_message_attempt(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        *,
        actor: Actor,
    ) -> MessageAttemptView:
        action = OutreachAction.ENROLLMENT_PREPARE_SEND
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        located = await self._locate_enrollment(tenant_id, enrollment_id)
        now = self._validate_now(self._now())
        blocked: Exception | None = None
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, located.campaign_id, actor, action
            )
            enrollment = await uow.enrollments.get_for_update(
                tenant_id, enrollment_id
            )
            if enrollment is None:
                raise ValidationError("Enrollment 不存在")
            if (
                enrollment.tenant_id != tenant_id
                or enrollment.campaign_id != campaign.campaign_id
            ):
                self._tenant_violation(actor, action, tenant_id)
            rule = self._require(
                actor,
                action,
                tenant_id,
                campaign_id=campaign.campaign_id,
                account_id=enrollment.account_id,
                enrollment_id=enrollment.enrollment_id,
            )
            if campaign.state is not CampaignState.ACTIVE:
                raise CampaignNotActiveError("Campaign 当前状态不允许准备消息")
            bound_version = await uow.campaigns.get_version(
                tenant_id, campaign.campaign_id, enrollment.campaign_version
            )
            current_version = await uow.campaigns.get_version(
                tenant_id, campaign.campaign_id, campaign.current_version
            )
            if bound_version is None or current_version is None:
                raise ValidationError("Enrollment Campaign 版本不存在")
            approval = await self._require_approved_version(
                tenant_id,
                campaign.campaign_id,
                enrollment.campaign_version,
                actor,
                action,
            )
            if enrollment.campaign_version == campaign.current_version and (
                campaign.approval_id != approval.approval_id
                or campaign.approved_by != approval.approved_by
                or campaign.approved_at != approval.approved_at
            ):
                raise CampaignApprovalRequiredError(
                    "Enrollment 版本审批绑定不匹配"
                )
            if (
                enrollment.state not in {EnrollmentState.ENROLLED, EnrollmentState.IN_SEQUENCE}
                or enrollment.next_send_at is None
                or enrollment.next_send_at > now
            ):
                raise InvalidStateTransition("Enrollment 当前不可准备消息")
            reply = await self._reply_snapshot(
                tenant_id, enrollment, actor, action
            )
            if reply.state is ReplyState.REPLIED:
                enrollment.transition_to(
                    EnrollmentState.REPLIED,
                    at=now,
                    reason=EnrollmentStopReason.REPLY,
                )
                enrollment.next_send_at = None
                await uow.enrollments.update(enrollment)
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(enrollment.enrollment_id),
                        f"enrollment:{enrollment.enrollment_id}:replied",
                        action,
                        now,
                    )
                )
                blocked = ReplyAlreadyReceivedError(
                    "联系人已经回复，不能继续准备消息"
                )
            elif await self._has_suppression(
                uow,
                tenant_id,
                enrollment.contact_point_id,
                enrollment.account_id,
            ):
                enrollment.transition_to(
                    EnrollmentState.STOPPED_SUPPRESSED,
                    at=now,
                    reason=EnrollmentStopReason.SUPPRESSION,
                )
                enrollment.next_send_at = None
                await uow.enrollments.update(enrollment)
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(enrollment.enrollment_id),
                        f"enrollment:{enrollment.enrollment_id}:suppressed",
                        action,
                        now,
                    )
                )
                blocked = SuppressedError("联系人或企业已进入全局抑制")
            else:
                contact = await self._contact_snapshot(
                    tenant_id,
                    enrollment.contact_point_id,
                    enrollment.account_id,
                )
                self._validate_contact_snapshot(
                    contact,
                    tenant_id=tenant_id,
                    contact_point_id=enrollment.contact_point_id,
                    account_id=enrollment.account_id,
                    boundary=bound_version.boundary,
                    actor=actor,
                    action=action,
                )
                sender = await self._sender_snapshot(
                    tenant_id,
                    enrollment.sending_identity_id,
                    actor,
                    action,
                )
                if not self._sender_is_eligible(sender):
                    raise SendingIdentityUnavailableError("发件身份当前不可用")
                step_number = enrollment.current_step + 1
                if step_number > len(bound_version.boundary.steps):
                    raise SequenceStepLimitError("序列步数超过已批准边界")
                key = IdempotencyKey(
                    f"{tenant_id}:{campaign.campaign_id}:{enrollment.enrollment_id}:"
                    f"v{enrollment.campaign_version}:step{step_number}"
                )
                existing = await uow.attempts.get_by_key(tenant_id, key)
                if existing is not None:
                    if (
                        existing.campaign_id != campaign.campaign_id
                        or existing.enrollment_id != enrollment.enrollment_id
                        or existing.campaign_version != enrollment.campaign_version
                        or existing.step_number != step_number
                        or existing.sending_identity_id
                        != enrollment.sending_identity_id
                    ):
                        raise MessageAttemptConflictError(
                            "Message Attempt 幂等记录损坏"
                        )
                    view = self._attempt_view(existing)
                else:
                    quota = await uow.quotas.reserve_message(
                        tenant_id,
                        campaign.campaign_id,
                        now.date(),
                        current_version.boundary.daily_total_message_limit,
                    )
                    if quota.status is QuotaReservationStatus.CAP_REACHED:
                        raise CampaignQuotaExceededError(
                            "Campaign 当日消息额度已满"
                        )
                    attempt = MessageAttempt(
                        tenant_id=tenant_id,
                        attempt_id=MessageAttemptId(new_id("mat")),
                        message_id=MessageId(new_id("msg")),
                        campaign_id=campaign.campaign_id,
                        enrollment_id=enrollment.enrollment_id,
                        campaign_version=enrollment.campaign_version,
                        step_number=step_number,
                        sending_identity_id=enrollment.sending_identity_id,
                        idempotency_key=key,
                        state=MessageAttemptState.RESERVED,
                        provider_ref=None,
                        failure_category=None,
                        created_at=now,
                        updated_at=now,
                    )
                    created = await uow.attempts.create_if_absent(attempt)
                    if created.status is AppendStatus.CONFLICT or created.winner is None:
                        raise MessageAttemptConflictError(
                            "Message Attempt 幂等记录冲突"
                        )
                    await uow.actions.append(
                        self._action(
                            tenant_id,
                            actor,
                            str(enrollment.enrollment_id),
                            f"attempt:{created.winner.attempt_id}:prepare",
                            action,
                            now,
                        )
                    )
                    view = self._attempt_view(created.winner)
        if blocked is not None:
            raise blocked
        self._allow(actor, action, tenant_id, rule)
        return view

    async def _validate_message_send_current_facts(
        self,
        uow: OutreachUnitOfWork,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        located: MessageAttempt,
        actor: Actor,
        action: OutreachAction,
    ) -> tuple[MessageAttempt, MessageSendPreflight, str]:
        campaign = await self._locked_campaign(
            uow, tenant_id, located.campaign_id, actor, action
        )
        enrollment = await uow.enrollments.get_for_update(
            tenant_id, located.enrollment_id
        )
        attempt = await uow.attempts.get_for_update(tenant_id, attempt_id)
        if enrollment is None or attempt is None:
            raise ValidationError("Message Attempt 资源不存在")
        if (
            attempt.tenant_id != tenant_id
            or enrollment.tenant_id != tenant_id
            or attempt.campaign_id != campaign.campaign_id
            or attempt.enrollment_id != enrollment.enrollment_id
        ):
            self._tenant_violation(actor, action, tenant_id)
        rule = self._require(
            actor,
            action,
            tenant_id,
            campaign_id=campaign.campaign_id,
            account_id=enrollment.account_id,
            enrollment_id=enrollment.enrollment_id,
        )
        version = await uow.campaigns.get_version(
            tenant_id, campaign.campaign_id, attempt.campaign_version
        )
        if version is None or enrollment.campaign_version != attempt.campaign_version:
            raise MessageAttemptConflictError("Message Attempt Campaign 版本不匹配")
        approval = await self._require_approved_version(
            tenant_id,
            campaign.campaign_id,
            attempt.campaign_version,
            actor,
            action,
        )
        if attempt.campaign_version == campaign.current_version and (
            campaign.approval_id != approval.approval_id
            or campaign.approved_by != approval.approved_by
            or campaign.approved_at != approval.approved_at
        ):
            raise CampaignApprovalRequiredError("Message Attempt 审批绑定不匹配")
        preflight = MessageSendPreflight(
            tenant_id=tenant_id,
            attempt_id=attempt.attempt_id,
            campaign_id=attempt.campaign_id,
            enrollment_id=attempt.enrollment_id,
            account_id=enrollment.account_id,
            contact_point_id=enrollment.contact_point_id,
            sending_identity_id=attempt.sending_identity_id,
            campaign_version=attempt.campaign_version,
            step_number=attempt.step_number,
            idempotency_key=attempt.idempotency_key,
        )
        if attempt.state is MessageAttemptState.SENT:
            if enrollment.current_step < attempt.step_number:
                raise MessageAttemptConflictError(
                    "Message Attempt step 与 Enrollment 不匹配"
                )
            return attempt, preflight, rule
        if campaign.state is not CampaignState.ACTIVE:
            raise CampaignNotActiveError("Campaign 当前状态不允许发送")
        if enrollment.state is EnrollmentState.STOPPED_SUPPRESSED:
            raise SuppressedError("联系人或企业已进入全局抑制")
        if enrollment.state not in {
            EnrollmentState.ENROLLED,
            EnrollmentState.IN_SEQUENCE,
        }:
            raise InvalidStateTransition("Enrollment 当前不可发送")
        if attempt.step_number != enrollment.current_step + 1:
            raise MessageAttemptConflictError("Message Attempt step 与 Enrollment 不匹配")
        if attempt.state not in {
            MessageAttemptState.RESERVED,
            MessageAttemptState.FAILED_TRANSIENT,
            MessageAttemptState.SENDING,
        }:
            raise MessageAttemptConflictError("Message Attempt 当前状态不可发送")
        reply = await self._reply_snapshot(tenant_id, enrollment, actor, action)
        if reply.state is ReplyState.REPLIED:
            raise ReplyAlreadyReceivedError("联系人已经回复，不能继续发送")
        if await self._has_suppression(
            uow,
            tenant_id,
            enrollment.contact_point_id,
            enrollment.account_id,
        ):
            raise SuppressedError("联系人或企业已进入全局抑制")
        contact = await self._contact_snapshot(
            tenant_id,
            enrollment.contact_point_id,
            enrollment.account_id,
        )
        self._validate_contact_snapshot(
            contact,
            tenant_id=tenant_id,
            contact_point_id=enrollment.contact_point_id,
            account_id=enrollment.account_id,
            boundary=version.boundary,
            actor=actor,
            action=action,
        )
        if (
            attempt.sending_identity_id != enrollment.sending_identity_id
            or attempt.sending_identity_id not in version.boundary.sender_identity_ids
        ):
            raise SendingIdentityUnavailableError("发件身份不在 Campaign 允许集合")
        sender = await self._sender_snapshot(
            tenant_id, attempt.sending_identity_id, actor, action
        )
        if not self._sender_is_eligible(sender):
            raise SendingIdentityUnavailableError("发件身份当前不可用")
        return (
            attempt,
            preflight,
            rule,
        )

    async def preflight_message_send(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        *,
        actor: Actor,
    ) -> MessageSendPreflight:
        action = OutreachAction.ENROLLMENT_PREPARE_SEND
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        located = await self._locate_attempt(tenant_id, attempt_id)
        async with self._uow_factory(tenant_id) as uow:
            _attempt, preflight, rule = await self._validate_message_send_current_facts(
                uow, tenant_id, attempt_id, located, actor, action
            )
        self._allow(actor, action, tenant_id, rule)
        return preflight

    async def claim_message_send(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        *,
        actor: Actor,
    ) -> MessageAttemptView:
        action = OutreachAction.ENROLLMENT_PREPARE_SEND
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        located = await self._locate_attempt(tenant_id, attempt_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            attempt, _preflight, rule = await self._validate_message_send_current_facts(
                uow, tenant_id, attempt_id, located, actor, action
            )
            if attempt.state is not MessageAttemptState.SENDING:
                attempt.transition_to(MessageAttemptState.SENDING, at=now)
                await uow.attempts.update(attempt)
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(attempt.enrollment_id),
                        f"attempt:{attempt.attempt_id}:sending",
                        action,
                        now,
                    )
                )
            view = self._attempt_view(attempt)
        self._allow(actor, action, tenant_id, rule)
        return view

    @staticmethod
    def _safe_provider_ref(provider_ref: object) -> str:
        if not isinstance(provider_ref, str):
            raise ValidationError("provider_ref 无效")
        lowered = provider_ref.lower()
        if (
            not 1 <= len(provider_ref) <= 200
            or provider_ref != provider_ref.strip()
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in provider_ref
            )
            or "://" in provider_ref
            or "@" in provider_ref
            or any(
                marker in lowered
                for marker in ("bearer", "token", "secret", "password")
            )
        ):
            raise ValidationError("provider_ref 无效")
        return provider_ref

    async def record_sent(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        provider_ref: str,
        *,
        actor: Actor,
    ) -> MessageAttemptView:
        action = OutreachAction.ENROLLMENT_RECORD_SENT
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        safe_ref = self._safe_provider_ref(provider_ref)
        located = await self._locate_attempt(tenant_id, attempt_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, located.campaign_id, actor, action
            )
            enrollment = await uow.enrollments.get_for_update(
                tenant_id, located.enrollment_id
            )
            attempt = await uow.attempts.get_for_update(tenant_id, attempt_id)
            if enrollment is None or attempt is None:
                raise ValidationError("Message Attempt 资源不存在")
            if (
                attempt.tenant_id != tenant_id
                or enrollment.tenant_id != tenant_id
                or attempt.campaign_id != campaign.campaign_id
                or attempt.enrollment_id != enrollment.enrollment_id
            ):
                self._tenant_violation(actor, action, tenant_id)
            rule = self._require(
                actor,
                action,
                tenant_id,
                campaign_id=campaign.campaign_id,
                account_id=enrollment.account_id,
                enrollment_id=enrollment.enrollment_id,
            )
            if attempt.state is MessageAttemptState.SENT:
                if attempt.provider_ref != safe_ref:
                    raise MessageAttemptConflictError(
                        "Message Attempt provider ref 冲突"
                    )
                view = self._attempt_view(attempt)
            else:
                if attempt.state is not MessageAttemptState.SENDING:
                    raise MessageAttemptConflictError(
                        "Message Attempt 尚未取得发送 claim"
                    )
                version = await uow.campaigns.get_version(
                    tenant_id, campaign.campaign_id, attempt.campaign_version
                )
                if version is None:
                    raise ValidationError("Message Attempt Campaign 版本不存在")
                if attempt.step_number != enrollment.current_step + 1:
                    raise MessageAttemptConflictError(
                        "Message Attempt step 与 Enrollment 不匹配"
                    )
                attempt.transition_to(
                    MessageAttemptState.SENT,
                    at=now,
                    provider_ref=safe_ref,
                )
                enrollment.current_step = attempt.step_number
                if attempt.step_number == len(version.boundary.steps):
                    enrollment.next_send_at = None
                    enrollment.transition_to(
                        EnrollmentState.COMPLETED, at=now, reason=None
                    )
                else:
                    enrollment.transition_to(
                        EnrollmentState.IN_SEQUENCE, at=now, reason=None
                    )
                    next_step = version.boundary.steps[attempt.step_number]
                    enrollment.next_send_at = now + timedelta(
                        days=next_step.wait_days
                    )
                await uow.attempts.update(attempt)
                await uow.enrollments.update(enrollment)
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(enrollment.enrollment_id),
                        f"attempt:{attempt.attempt_id}:sent",
                        action,
                        now,
                    )
                )
                await uow.bus.publish(
                    MessageSent(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        message_id=attempt.message_id,
                        campaign_id=attempt.campaign_id,
                        sending_identity_id=attempt.sending_identity_id,
                    )
                )
                view = self._attempt_view(attempt)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def record_send_failure(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        category: SendFailureCategory,
        *,
        actor: Actor,
    ) -> MessageAttemptView:
        action = OutreachAction.ENROLLMENT_RECORD_FAILURE
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(category, SendFailureCategory):
            raise ValidationError("发送失败类别无效")
        located = await self._locate_attempt(tenant_id, attempt_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, located.campaign_id, actor, action
            )
            enrollment = await uow.enrollments.get_for_update(
                tenant_id, located.enrollment_id
            )
            attempt = await uow.attempts.get_for_update(tenant_id, attempt_id)
            if enrollment is None or attempt is None:
                raise ValidationError("Message Attempt 资源不存在")
            if (
                attempt.tenant_id != tenant_id
                or enrollment.tenant_id != tenant_id
                or attempt.campaign_id != campaign.campaign_id
                or attempt.enrollment_id != enrollment.enrollment_id
            ):
                self._tenant_violation(actor, action, tenant_id)
            rule = self._require(
                actor,
                action,
                tenant_id,
                campaign_id=campaign.campaign_id,
                account_id=enrollment.account_id,
                enrollment_id=enrollment.enrollment_id,
            )
            if attempt.state is MessageAttemptState.SENT:
                raise MessageAttemptConflictError("已发送 Attempt 不能记录失败")
            if attempt.state in {
                MessageAttemptState.FAILED_TRANSIENT,
                MessageAttemptState.FAILED_PERMANENT,
            }:
                if attempt.failure_category is not category:
                    raise MessageAttemptConflictError(
                        "Message Attempt failure category 冲突"
                    )
            else:
                if attempt.state is not MessageAttemptState.SENDING:
                    raise MessageAttemptConflictError(
                        "Message Attempt 尚未取得发送 claim"
                    )
                target = (
                    MessageAttemptState.FAILED_TRANSIENT
                    if category
                    in {
                        SendFailureCategory.RATE_LIMITED,
                        SendFailureCategory.PROVIDER_TRANSIENT,
                        SendFailureCategory.PROVIDER_AUTH_REQUIRED,
                    }
                    else MessageAttemptState.FAILED_PERMANENT
                )
                attempt.transition_to(
                    target,
                    at=now,
                    failure_category=category,
                )
                await uow.attempts.update(attempt)
                if category is SendFailureCategory.IDENTITY_UNAVAILABLE:
                    enrollment.next_send_at = None
                    enrollment.transition_to(
                        EnrollmentState.STOPPED_IDENTITY_UNAVAILABLE,
                        at=now,
                        reason=EnrollmentStopReason.IDENTITY_UNAVAILABLE,
                    )
                    await uow.enrollments.update(enrollment)
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(enrollment.enrollment_id),
                        f"attempt:{attempt.attempt_id}:failure:{category.value}",
                        action,
                        now,
                    )
                )
            view = self._attempt_view(attempt)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def stop_enrollment(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        reason: EnrollmentStopReason,
        *,
        actor: Actor,
    ) -> EnrollmentView:
        action = OutreachAction.ENROLLMENT_STOP
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if reason is not EnrollmentStopReason.MANUAL:
            raise ValidationError("人工停止只接受 manual reason")
        located = await self._locate_enrollment(tenant_id, enrollment_id)
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            campaign = await self._locked_campaign(
                uow, tenant_id, located.campaign_id, actor, action
            )
            enrollment = await uow.enrollments.get_for_update(
                tenant_id, enrollment_id
            )
            if enrollment is None:
                raise ValidationError("Enrollment 不存在")
            if (
                enrollment.tenant_id != tenant_id
                or enrollment.campaign_id != campaign.campaign_id
            ):
                self._tenant_violation(actor, action, tenant_id)
            rule = self._require(
                actor,
                action,
                tenant_id,
                campaign_id=campaign.campaign_id,
                account_id=enrollment.account_id,
                enrollment_id=enrollment.enrollment_id,
            )
            if enrollment.state is not EnrollmentState.STOPPED_MANUAL:
                enrollment.next_send_at = None
                enrollment.transition_to(
                    EnrollmentState.STOPPED_MANUAL,
                    at=now,
                    reason=EnrollmentStopReason.MANUAL,
                )
                await uow.enrollments.update(enrollment)
            await uow.actions.append(
                self._action(
                    tenant_id,
                    actor,
                    str(enrollment.enrollment_id),
                    f"enrollment:{enrollment.enrollment_id}:manual-stop",
                    action,
                    now,
                )
            )
            view = self._enrollment_view(enrollment)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def get_enrollment(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        *,
        actor: Actor,
    ) -> EnrollmentView:
        action = OutreachAction.ENROLLMENT_READ
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        async with self._uow_factory(tenant_id) as uow:
            enrollment = await uow.enrollments.get(tenant_id, enrollment_id)
            if enrollment is None:
                raise ValidationError("Enrollment 不存在")
            if enrollment.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            rule = self._require(
                actor,
                action,
                tenant_id,
                campaign_id=enrollment.campaign_id,
                account_id=enrollment.account_id,
                enrollment_id=enrollment.enrollment_id,
            )
            view = self._enrollment_view(enrollment)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def list_enrollments(
        self,
        tenant_id: TenantId,
        scope: OutreachScope,
        *,
        limit: int,
        actor: Actor,
    ) -> list[EnrollmentView]:
        action = OutreachAction.ENROLLMENT_LIST
        actor, pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(scope, OutreachScope) or scope is not actor.scope:
            self._deny(actor, action, tenant_id)
            raise PermissionDenied("Phase 1 触达授权拒绝")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValidationError("limit 无效")
        async with self._uow_factory(tenant_id) as uow:
            enrollments = await uow.enrollments.list_scoped(
                tenant_id, scope, limit
            )
            views: list[EnrollmentView] = []
            rules: list[str] = []
            for enrollment in enrollments:
                if enrollment.tenant_id != tenant_id:
                    self._tenant_violation(actor, action, tenant_id)
                rules.append(
                    self._require(
                        actor,
                        action,
                        tenant_id,
                        campaign_id=enrollment.campaign_id,
                        account_id=enrollment.account_id,
                        enrollment_id=enrollment.enrollment_id,
                    )
                )
                views.append(self._enrollment_view(enrollment))
        self._allow(actor, action, tenant_id, rules[0] if rules else pre_rule)
        return views

    @staticmethod
    def _suppression_view(entry: SuppressionEntry) -> SuppressionView:
        return SuppressionView(
            tenant_id=entry.tenant_id,
            suppression_id=entry.suppression_id,
            target=entry.target,
            reason=entry.reason,
            occurred_at=entry.occurred_at,
            source_ref=entry.source_ref,
            idempotency_key=entry.idempotency_key,
            created_at=entry.created_at,
        )

    @staticmethod
    def _same_suppression_payload(
        entry: SuppressionEntry, request: SuppressionRequest, tenant_id: TenantId
    ) -> bool:
        return (
            entry.tenant_id == tenant_id
            and entry.target == request.target
            and entry.reason is request.reason
            and entry.occurred_at == request.occurred_at
            and entry.source_ref == request.source_ref
            and entry.idempotency_key == request.idempotency_key
        )

    async def add_suppression(
        self,
        tenant_id: TenantId,
        request: SuppressionRequest,
        *,
        actor: Actor,
    ) -> SuppressionResult:
        action = OutreachAction.SUPPRESSION_ADD
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(request, SuppressionRequest):
            raise ValidationError("Suppression request 无效")
        rule = self._require(
            actor,
            action,
            tenant_id,
            suppression_target=request.target.canonical_id,
            suppression_reason=request.reason,
        )
        now = self._validate_now(self._now())
        if request.occurred_at > now + timedelta(minutes=5):
            raise ValidationError("suppression occurred_at 超出允许时间")
        entry = SuppressionEntry(
            tenant_id=tenant_id,
            suppression_id=SuppressionId(new_id("sup")),
            target=request.target,
            reason=request.reason,
            occurred_at=request.occurred_at,
            source_ref=request.source_ref,
            idempotency_key=request.idempotency_key,
            created_at=now,
        )
        async with self._uow_factory(tenant_id) as uow:
            appended = await uow.suppressions.append_if_absent(entry)
            if appended.status is AppendStatus.CONFLICT or appended.winner is None:
                raise IdempotencyConflictError(
                    "Suppression 幂等键已绑定不同内容"
                )
            winner = appended.winner
            if winner.tenant_id != tenant_id:
                self._tenant_violation(actor, action, tenant_id)
            if not self._same_suppression_payload(
                winner, request, tenant_id
            ):
                raise IdempotencyConflictError(
                    "Suppression 幂等键已绑定不同内容"
                )
            if appended.status is AppendStatus.EXISTING:
                result = SuppressionResult(
                    False, self._suppression_view(winner), 0
                )
            else:
                enrollments = await uow.enrollments.lock_matching_active(
                    tenant_id, request.target
                )
                ordered = sorted(
                    enrollments, key=lambda value: value.enrollment_id
                )
                for enrollment in ordered:
                    if enrollment.tenant_id != tenant_id:
                        self._tenant_violation(actor, action, tenant_id)
                    enrollment.next_send_at = None
                    enrollment.transition_to(
                        EnrollmentState.STOPPED_SUPPRESSED,
                        at=now,
                        reason=EnrollmentStopReason.SUPPRESSION,
                    )
                    await uow.enrollments.update(enrollment)
                    await uow.actions.append(
                        self._action(
                            tenant_id,
                            actor,
                            str(enrollment.enrollment_id),
                            f"enrollment:{enrollment.enrollment_id}:"
                            f"suppression:{winner.suppression_id}",
                            action,
                            now,
                        )
                    )
                await uow.actions.append(
                    self._action(
                        tenant_id,
                        actor,
                        str(winner.suppression_id),
                        f"suppression:{winner.suppression_id}:add",
                        action,
                        now,
                    )
                )
                await uow.bus.publish(
                    SuppressionAdded(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        scope=winner.target.scope.value,
                        target_id=winner.target.canonical_id,
                        reason=winner.reason.value,
                    )
                )
                result = SuppressionResult(
                    True,
                    self._suppression_view(winner),
                    len(ordered),
                )
        self._allow(actor, action, tenant_id, rule)
        return result

    async def is_suppressed(
        self,
        tenant_id: TenantId,
        target: SuppressionTarget,
        *,
        actor: Actor,
    ) -> SuppressionView | None:
        action = OutreachAction.SUPPRESSION_READ
        actor, _pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(target, SuppressionTarget):
            raise ValidationError("suppression target 无效")
        rule = self._require(
            actor,
            action,
            tenant_id,
            suppression_target=target.canonical_id,
        )
        async with self._uow_factory(tenant_id) as uow:
            entry = await uow.suppressions.find_current(tenant_id, target)
            if entry is None:
                view = None
            else:
                if entry.tenant_id != tenant_id:
                    self._tenant_violation(actor, action, tenant_id)
                if entry.target != target:
                    raise ValidationError("suppression 查询结果资源不匹配")
                self._require(
                    actor,
                    action,
                    tenant_id,
                    suppression_target=entry.target.canonical_id,
                    suppression_reason=entry.reason,
                )
                view = self._suppression_view(entry)
        self._allow(actor, action, tenant_id, rule)
        return view

    async def list_suppressions(
        self,
        tenant_id: TenantId,
        scope: OutreachScope,
        *,
        limit: int,
        actor: Actor,
    ) -> list[SuppressionView]:
        action = OutreachAction.SUPPRESSION_LIST
        actor, pre_rule = self._preauthorize(actor, action, tenant_id)
        if not isinstance(scope, OutreachScope) or scope is not actor.scope:
            self._deny(actor, action, tenant_id)
            raise PermissionDenied("Phase 1 触达授权拒绝")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 200
        ):
            raise ValidationError("limit 无效")
        async with self._uow_factory(tenant_id) as uow:
            entries = await uow.suppressions.list_scoped(
                tenant_id, scope, limit
            )
            views: list[SuppressionView] = []
            rules: list[str] = []
            for entry in entries:
                if entry.tenant_id != tenant_id:
                    self._tenant_violation(actor, action, tenant_id)
                rules.append(
                    self._require(
                        actor,
                        action,
                        tenant_id,
                        suppression_target=entry.target.canonical_id,
                        suppression_reason=entry.reason,
                    )
                )
                views.append(self._suppression_view(entry))
        self._allow(actor, action, tenant_id, rules[0] if rules else pre_rule)
        return views
