"""以 Conversation/Outreach 持久事实验证客户回复证据。"""

from __future__ import annotations

from collections.abc import Callable

from domains.conversations.schemas import ReplyCategory
from domains.conversations.service import ConversationsUnitOfWork
from domains.demand.schemas import (
    CustomerReplyEvidenceClaim,
    VerifiedCustomerReplyEvidence,
)
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    DeliveryCorrelationLookup,
    DeliveryFeedbackTarget,
    EnrollmentView,
)
from domains.outreach.service import OutreachService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import TenantId


class TenantBoundCustomerReplyEvidenceVerifier:
    """核对入站消息、分类、会话、投递和 Enrollment 的完整租户绑定链。"""

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        conversations_uow_factory: Callable[[TenantId], ConversationsUnitOfWork],
        outreach: OutreachService,
    ) -> None:
        if not callable(conversations_uow_factory):
            raise ValidationError("客户回复证据依赖无效")
        self._tenant_id = tenant_id
        self._uow_factory = conversations_uow_factory
        self._outreach = outreach

    async def verify(
        self,
        tenant_id: TenantId,
        claim: CustomerReplyEvidenceClaim,
    ) -> VerifiedCustomerReplyEvidence:
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("跨租户数据隔离违规")

        async with self._uow_factory(tenant_id) as uow:
            message = await uow.messages.get(tenant_id, claim.source_message_id)
            if message is None:
                raise ValidationError("客户回复消息不存在")
            if getattr(message, "tenant_id", None) != tenant_id:
                raise TenantIsolationViolation("跨租户数据隔离违规")
            if getattr(message, "message_id", None) != claim.source_message_id:
                raise ValidationError("客户回复消息关联不匹配")
            direction = getattr(getattr(message, "direction", None), "value", None)
            if direction != "inbound":
                raise ValidationError("客户回复证据来自非入站消息")
            if getattr(message, "outbound_message_id", None) != claim.outbound_message_id:
                raise ValidationError("客户回复出站关联不匹配")
            classification = await uow.classifications.get(
                tenant_id, claim.source_message_id
            )
            if classification is None:
                raise ValidationError("客户回复分类不存在")
            if getattr(classification, "tenant_id", None) != tenant_id:
                raise TenantIsolationViolation("跨租户数据隔离违规")
            if getattr(classification, "message_id", None) != claim.source_message_id:
                raise ValidationError("客户回复分类关联不匹配")
            conversation = await uow.conversations.get(
                tenant_id, message.conversation_id
            )
            if conversation is None:
                raise ValidationError("客户回复会话不存在")
            if getattr(conversation, "tenant_id", None) != tenant_id:
                raise TenantIsolationViolation("跨租户数据隔离违规")
            if (
                getattr(conversation, "conversation_id", None)
                != message.conversation_id
            ):
                raise ValidationError("客户回复会话关联不匹配")
            if getattr(conversation, "account_id", None) != claim.account_id:
                raise ValidationError("客户回复企业关联不匹配")

        actor = Actor(
            "system:reply-evidence-verifier",
            OutreachScope(
                level=ScopeLevel.SYSTEM,
                allowed_enrollment_ids=frozenset({claim.enrollment_id}),
            ),
            "system",
        )
        enrollment = await self._outreach.get_enrollment(
            tenant_id, claim.enrollment_id, actor=actor
        )
        if (
            not isinstance(enrollment, EnrollmentView)
            or enrollment.tenant_id != tenant_id
            or enrollment.enrollment_id != claim.enrollment_id
            or enrollment.account_id != claim.account_id
            or enrollment.contact_point_id != claim.contact_point_id
        ):
            raise ValidationError("客户回复 Enrollment 关联不匹配")
        if enrollment.source_hypothesis_id != claim.hypothesis_id:
            raise ValidationError("客户回复来源假设不匹配")
        target = await self._outreach.resolve_delivery_feedback(
            tenant_id,
            DeliveryCorrelationLookup(
                deterministic_message_id=str(claim.outbound_message_id)
            ),
            actor=Actor(
                "system:reply-evidence-verifier",
                OutreachScope(
                    level=ScopeLevel.SYSTEM,
                    allowed_sending_identity_ids=frozenset(
                        {enrollment.sending_identity_id}
                    ),
                ),
                "system",
            ),
        )
        if (
            not isinstance(target, DeliveryFeedbackTarget)
            or target.tenant_id != tenant_id
            or target.enrollment_id != claim.enrollment_id
            or target.account_id != claim.account_id
            or target.contact_point_id != claim.contact_point_id
            or target.sending_identity_id != enrollment.sending_identity_id
        ):
            raise ValidationError("客户回复投递关联不匹配")

        level = _reply_evidence_level(classification.category)
        return VerifiedCustomerReplyEvidence(
            tenant_id=tenant_id,
            hypothesis_id=claim.hypothesis_id,
            source_message_id=claim.source_message_id,
            account_id=claim.account_id,
            evidence_level=level,
            classified_by=classification.classified_by,
            classified_at=classification.classified_at,
        )


def _reply_evidence_level(category: ReplyCategory) -> EvidenceLevel:
    if category is ReplyCategory.PROVIDES_SPECIFICATION:
        return EvidenceLevel.CUSTOMER_SPECIFICATION
    if category in {
        ReplyCategory.CLEAR_INTEREST,
        ReplyCategory.WILLING_TO_CONTINUE,
        ReplyCategory.REQUESTS_MATERIALS,
        ReplyCategory.REQUESTS_QUOTE,
        ReplyCategory.REQUESTS_SAMPLE,
    }:
        return EvidenceLevel.CUSTOMER_INTEREST_REPLY
    raise ValidationError("客户回复分类不能作为需求验证证据")
