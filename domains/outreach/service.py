"""触达域服务与跨域只读事实提供者的公共契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from domains.outreach.errors import (
    MessageAttemptConflictError as _MessageAttemptConflictError,
)
from domains.outreach.models import (
    EnrollmentStopReason,
    SendFailureCategory,
)
from domains.outreach.models import (
    SuppressionReason as _SuppressionReason,
)
from domains.outreach.permissions import Actor, OutreachScope
from domains.outreach.repository import (
    OutreachUnitOfWorkFactory as _OutreachUnitOfWorkFactory,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignCreateRequest,
    CampaignView,
    ContactEligibilitySnapshot,
    DeliveryCorrelationBinding,
    DeliveryCorrelationLookup,
    DeliveryFeedbackTarget,
    EnrollmentCreateRequest,
    EnrollmentView,
    MessageAttemptView,
    MessageSendPreflight,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SuppressionRequest,
    SuppressionResult,
    SuppressionTarget,
    SuppressionView,
)
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)

MessageAttemptConflictError = _MessageAttemptConflictError
OutreachUnitOfWorkFactory = _OutreachUnitOfWorkFactory
SuppressionReason = _SuppressionReason


@runtime_checkable
class ContactEligibilityProvider(Protocol):
    """提供联系人可触达性的不可变事实快照。"""

    async def get_contact_eligibility(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ContactEligibilitySnapshot: ...


@runtime_checkable
class SendingIdentityEligibilityProvider(Protocol):
    """提供发件身份在当前时刻的发送资格快照。"""

    async def get_sending_identity_eligibility(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
    ) -> SendingIdentityEligibilitySnapshot: ...


@runtime_checkable
class CampaignApprovalProvider(Protocol):
    """提供指定 Campaign 版本的审批事实。"""

    async def get_campaign_approval(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        version: int,
    ) -> CampaignApprovalSnapshot | None: ...


@runtime_checkable
class ReplyStatusProvider(Protocol):
    """提供发送前必须现查的回复事实。"""

    async def get_reply_status(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ReplyStatusSnapshot: ...


@runtime_checkable
class OutreachService(Protocol):
    """触达域的唯一公共业务入口。"""

    async def create_campaign(
        self,
        tenant_id: TenantId,
        request: CampaignCreateRequest,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def submit_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def revise_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        request: CampaignCreateRequest,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def activate_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def pause_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        reason: str,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def cancel_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def get_campaign(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        *,
        actor: Actor,
    ) -> CampaignView: ...

    async def list_campaigns(
        self,
        tenant_id: TenantId,
        scope: OutreachScope,
        *,
        limit: int,
        actor: Actor,
    ) -> list[CampaignView]: ...

    async def enroll(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        request: EnrollmentCreateRequest,
        *,
        actor: Actor,
    ) -> EnrollmentView: ...

    async def prepare_message_attempt(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        *,
        actor: Actor,
    ) -> MessageAttemptView: ...

    async def preflight_message_send(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        *,
        actor: Actor,
    ) -> MessageSendPreflight: ...

    async def claim_message_send(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        *,
        actor: Actor,
    ) -> MessageAttemptView: ...

    async def record_sent(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        provider_ref: str,
        *,
        actor: Actor,
    ) -> MessageAttemptView: ...

    async def record_send_failure(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        category: SendFailureCategory,
        *,
        actor: Actor,
    ) -> MessageAttemptView: ...

    async def stop_enrollment(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        reason: EnrollmentStopReason,
        *,
        actor: Actor,
    ) -> EnrollmentView: ...

    async def get_enrollment(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        *,
        actor: Actor,
    ) -> EnrollmentView: ...

    async def list_enrollments(
        self,
        tenant_id: TenantId,
        scope: OutreachScope,
        *,
        limit: int,
        actor: Actor,
    ) -> list[EnrollmentView]: ...

    async def add_suppression(
        self,
        tenant_id: TenantId,
        request: SuppressionRequest,
        *,
        actor: Actor,
    ) -> SuppressionResult: ...

    async def is_suppressed(
        self,
        tenant_id: TenantId,
        target: SuppressionTarget,
        *,
        actor: Actor,
    ) -> SuppressionView | None: ...

    async def list_suppressions(
        self,
        tenant_id: TenantId,
        scope: OutreachScope,
        *,
        limit: int,
        actor: Actor,
    ) -> list[SuppressionView]: ...

    async def bind_delivery_correlation(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        binding: DeliveryCorrelationBinding,
        *,
        actor: Actor,
    ) -> MessageAttemptView: ...

    async def resolve_delivery_feedback(
        self,
        tenant_id: TenantId,
        lookup: DeliveryCorrelationLookup,
        *,
        actor: Actor,
    ) -> DeliveryFeedbackTarget | None: ...

    async def apply_hard_bounce(
        self,
        tenant_id: TenantId,
        target: DeliveryFeedbackTarget,
        provider_event_id: str,
        occurred_at: datetime,
        *,
        actor: Actor,
    ) -> SuppressionResult: ...

    async def apply_complaint(
        self,
        tenant_id: TenantId,
        target: DeliveryFeedbackTarget,
        provider_event_id: str,
        occurred_at: datetime,
        *,
        actor: Actor,
    ) -> SuppressionResult: ...
