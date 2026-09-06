"""从精确消息/投递/Enrollment/Need读取缺项，只有原领域选择器决定下一问。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domains.conversations.schemas import ReplyNextQuestionsView
from domains.conversations.service import (
    ConversationService,
    require_reply_internal_access,
)
from domains.demand.service import DemandService
from domains.employees.schemas import EmployeeView
from domains.employees.service import Actor as EmployeeActor
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import DeliveryCorrelationLookup
from domains.outreach.service import OutreachService
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ConversationId,
    EmployeeId,
    MessageId,
    NeedHypothesisId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.quote_facts import QuoteEmployeeFact


class ReplyEmployeeReader(Protocol):
    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: EmployeeActor
    ) -> EmployeeView: ...


@dataclass(frozen=True)
class ReplySuggestionApplication:
    """入口只接可信actor ID和精确选择，授权必须先于任何会话/需求IO。"""

    tenant_id: TenantId
    employees: ReplyEmployeeReader
    lookup_actor: EmployeeActor
    conversations: ConversationService
    outreach: OutreachService
    demand: DemandService

    async def read(
        self,
        tenant: TenantId,
        actor_id: EmployeeId,
        conversation_id: ConversationId,
        message_id: MessageId,
    ) -> ReplyNextQuestionsView:
        if tenant != self.tenant_id:
            raise PermissionDenied("回复建议权限拒绝")
        employee = await self.employees.get_employee(
            tenant, actor_id, actor=self.lookup_actor
        )
        if employee.employee_id != actor_id:
            raise PermissionDenied("回复建议权限拒绝")
        fact = QuoteEmployeeFact.model_validate(
            {
                "tenant_id": employee.tenant_id,
                "employee_id": employee.employee_id,
                "role": employee.role,
                "is_active": employee.is_active,
                "manager_id": employee.manager_id,
                "team_id": employee.team_id,
            }
        )
        require_reply_internal_access(tenant, fact, action="next_questions")
        detail = await self.conversations.get_inbox_detail(tenant, conversation_id)
        message = next((m for m in detail.messages if m.message_id == message_id), None)
        if message is None or message.direction != "inbound":
            raise ValidationError("回复建议消息关联不存在")
        base = {
            "conversation_id": str(conversation_id),
            "source_message_id": str(message_id),
            "need_id": None,
            "state": "need_unavailable",
            "completeness": None,
            "topics": (),
            "suggestions": (),
        }
        if message.outbound_message_id is None or message.original_category is None:
            return ReplyNextQuestionsView.model_validate(base)
        actor = Actor(
            str(actor_id),
            OutreachScope(
                level=ScopeLevel.TENANT,
                allowed_account_ids=frozenset({detail.account_id}),
            ),
            employee.role,
        )
        delivery = await self.outreach.resolve_reply_source(
            tenant,
            DeliveryCorrelationLookup(
                deterministic_message_id=str(message.outbound_message_id)
            ),
            actor=actor,
        )
        if (
            delivery is None
            or delivery.tenant_id != tenant
            or delivery.account_id != detail.account_id
        ):
            return ReplyNextQuestionsView.model_validate(base)
        enrollment = await self.outreach.get_enrollment(
            tenant, delivery.enrollment_id, actor=actor
        )
        if (
            enrollment.tenant_id != tenant
            or enrollment.enrollment_id != delivery.enrollment_id
            or enrollment.account_id != detail.account_id
            or enrollment.contact_point_id != delivery.contact_point_id
        ):
            raise ValidationError("回复建议投递关联无效")
        if enrollment.source_hypothesis_id is None:
            return ReplyNextQuestionsView.model_validate(base)
        hypothesis = await self.demand.get_hypothesis(
            tenant, NeedHypothesisId(str(enrollment.source_hypothesis_id))
        )
        if hypothesis.account_id != str(detail.account_id):
            raise ValidationError("回复建议需求关联无效")
        if hypothesis.validated_need_id is None:
            return ReplyNextQuestionsView.model_validate(base)
        need = await self.demand.get_need(
            tenant, ValidatedNeedId(hypothesis.validated_need_id)
        )
        if need.account_id != str(detail.account_id):
            raise ValidationError("回复建议需求关联无效")
        if not any(field.source_ref == str(message_id) for field in need.fields):
            return ReplyNextQuestionsView.model_validate(base)
        selected = await self.conversations.suggest_next_questions(
            tenant, conversation_id, need.missing_for_sourcing, need.completeness
        )
        from agent_runtime.qualification_agent.questions import render_questions

        return ReplyNextQuestionsView.model_validate(
            base
            | {
                "need_id": need.need_id,
                "state": "suggested" if selected.topics else "no_missing_fields",
                "completeness": need.completeness,
                "topics": tuple(selected.topics),
                "suggestions": render_questions(tuple(selected.topics)),
            }
        )
