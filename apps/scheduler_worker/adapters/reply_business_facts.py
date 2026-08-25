"""沿 Enrollment 来源链重读回复闭环业务事实。"""

from __future__ import annotations

from domains.demand.service import DemandService
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.service import OpportunityService
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    DeliveryCorrelationLookup,
    DeliveryFeedbackTarget,
)
from domains.outreach.service import OutreachService
from domains.prospecting.service import ProspectingService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    NeedHypothesisId,
    OpportunityId,
    SendingIdentityId,
    TenantId,
    ValidatedNeedId,
)
from workflows.reply_qualification.ports import ReplyActionContext

from ..reply_actions import ReplyBusinessFacts


def _enrollment_actor(context: ReplyActionContext) -> OutreachActor:
    return OutreachActor(
        "system:reply-business-facts",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({context.enrollment_id}),
        ),
        "system",
    )


def _identity_actor(identity_id: SendingIdentityId) -> OutreachActor:
    return OutreachActor(
        "system:reply-business-facts",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


class TenantBoundReplyBusinessFactsReader:
    """只沿 tenant/message context 指向的 Enrollment 来源链读取。"""

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        outreach: OutreachService,
        demand: DemandService,
        prospecting: ProspectingService,
        opportunities: OpportunityService,
        opportunity_actor: OpportunityActor,
    ) -> None:
        required = (
            (outreach, "get_enrollment"),
            (outreach, "resolve_delivery_feedback"),
            (demand, "get_hypothesis"),
            (demand, "get_need"),
            (prospecting, "get_account"),
            (prospecting, "get_contact_point"),
            (opportunities, "get_by_need"),
        )
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or any(not callable(getattr(value, name, None)) for value, name in required)
            or not isinstance(opportunity_actor, OpportunityActor)
        ):
            raise ValidationError("回复业务事实读取依赖无效")
        self._tenant_id = tenant_id
        self._outreach = outreach
        self._demand = demand
        self._prospecting = prospecting
        self._opportunities = opportunities
        self._opportunity_actor = opportunity_actor

    async def load(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
    ) -> ReplyBusinessFacts | None:
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("回复业务事实跨租户读取")
        if not isinstance(context, ReplyActionContext):
            raise ValidationError("回复业务事实上下文无效")
        enrollment = await self._outreach.get_enrollment(
            tenant_id,
            context.enrollment_id,
            actor=_enrollment_actor(context),
        )
        if (
            enrollment.tenant_id != tenant_id
            or enrollment.enrollment_id != context.enrollment_id
            or enrollment.account_id != context.account_id
            or enrollment.contact_point_id != context.contact_point_id
        ):
            raise ValidationError("回复 Enrollment 关联不匹配")
        source_hypothesis_id = enrollment.source_hypothesis_id
        if source_hypothesis_id is None:
            raise ValidationError("回复 Enrollment 缺少来源假设")
        identity_id = SendingIdentityId(str(enrollment.sending_identity_id))
        delivery = await self._outreach.resolve_delivery_feedback(
            tenant_id,
            DeliveryCorrelationLookup(
                deterministic_message_id=str(context.outbound_message_id)
            ),
            actor=_identity_actor(identity_id),
        )
        if (
            not isinstance(delivery, DeliveryFeedbackTarget)
            or delivery.tenant_id != tenant_id
            or delivery.enrollment_id != context.enrollment_id
            or delivery.account_id != context.account_id
            or delivery.contact_point_id != context.contact_point_id
            or delivery.sending_identity_id != identity_id
        ):
            raise ValidationError("回复出站消息关联不匹配")

        account = await self._prospecting.get_account(tenant_id, context.account_id)
        contact = await self._prospecting.get_contact_point(
            tenant_id, context.contact_point_id
        )
        if (
            account.tenant_id != tenant_id
            or account.account_id != context.account_id
            or contact.tenant_id != tenant_id
            or contact.contact_point_id != context.contact_point_id
            or contact.account_id != context.account_id
            or getattr(contact.verification, "value", None) != "verified"
            or contact.verified_at is None
        ):
            raise ValidationError("回复客户关联事实无效")

        hypothesis = await self._demand.get_hypothesis(
            tenant_id, NeedHypothesisId(str(source_hypothesis_id))
        )
        if (
            hypothesis.hypothesis_id != str(source_hypothesis_id)
            or hypothesis.account_id != str(context.account_id)
        ):
            raise ValidationError("回复来源假设关联不匹配")

        need_id: ValidatedNeedId | None = None
        opportunity_id: OpportunityId | None = None
        need = None
        if hypothesis.validated_need_id is not None:
            need_id = ValidatedNeedId(hypothesis.validated_need_id)
            need = await self._demand.get_need(tenant_id, need_id)
            if (
                need.need_id != str(need_id)
                or need.account_id != str(context.account_id)
            ):
                raise ValidationError("回复已验证需求关联不匹配")
            opportunity = await self._opportunities.get_by_need(
                tenant_id,
                need_id,
                actor=self._opportunity_actor,
            )
            if opportunity is not None:
                if (
                    opportunity.need_id != str(need_id)
                    or opportunity.account_id != str(context.account_id)
                    or opportunity.product_category != need.product_category
                ):
                    raise ValidationError("回复机会关联不匹配")
                opportunity_id = OpportunityId(str(opportunity.opportunity_id))
        elif hypothesis.status == "validated":
            raise ValidationError("回复来源假设缺少已验证需求链接")

        category = need.product_category if need is not None else hypothesis.category
        return ReplyBusinessFacts(
            need_id=need_id,
            hypothesis_id=NeedHypothesisId(str(source_hypothesis_id)),
            opportunity_id=opportunity_id,
            account_name=account.name,
            country=account.country,
            why_valuable=f"客户回复正在验证 {category} 需求",
            how_we_found_them="需求信号驱动的账户发现",
            validated_need_summary=(
                f"{need.product_category}；完整度 {need.completeness}/5"
                if need is not None
                else None
            ),
            missing_information=(
                tuple(need.missing_for_sourcing) if need is not None else ()
            ),
        )


__all__ = ("TenantBoundReplyBusinessFactsReader",)
