"""触达域服务实现：Campaign 生命周期及后续 Enrollment/抑制共用安全边界。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from domains.outreach.errors import (
    CampaignApprovalRequiredError,
    IdempotencyConflictError,
    OutreachProviderUnavailableError,
    SendingIdentityUnavailableError,
)
from domains.outreach.models import (
    ActionRecord,
    Campaign,
    CampaignBoundary,
    CampaignState,
    CampaignVersion,
    SequenceStepSpec,
)
from domains.outreach.permissions import (
    Actor,
    AuditLogger,
    OutreachAction,
    OutreachAuthorizer,
    OutreachScope,
)
from domains.outreach.repository import OutreachUnitOfWork, OutreachUnitOfWorkFactory
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignBoundaryView,
    CampaignCreateRequest,
    CampaignView,
    OutreachSenderRole,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
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
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    EmployeeId,
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
    ) -> str:
        try:
            return self._authorizer.require(
                actor,
                action,
                actor.scope,
                tenant_id,
                campaign_id=campaign_id,
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
        campaign_id: CampaignId,
        key: str,
        action: OutreachAction,
        occurred_at: datetime,
    ) -> ActionRecord:
        return ActionRecord(
            tenant_id=tenant_id,
            action_id=new_id("act"),
            action_key=key,
            action=action.value,
            entity_id=str(campaign_id),
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
