"""首次回复晋升后的机会创建与员工归属应用层组合。"""

from __future__ import annotations

from domains.demand.service import DemandService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.service import EmployeeService
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    ValidatedNeedEvidence,
)
from domains.opportunities.service import OpportunityService, OpportunityState
from domains.organization.permissions import OrganizationActor
from domains.organization.service import OrganizationService
from domains.prospecting.service import ProspectingService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    EmployeeId,
    NeedHypothesisId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import Provenance, SourceType
from workflows.reply_qualification.ports import ReplyActionContext

from ..reply_actions import ReplyEvidenceSnapshot


class DurableReplyOpportunityIntake:
    """从 durable reply/Playbook/contact 事实创建并分配唯一机会。"""

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        demand: DemandService,
        prospecting: ProspectingService,
        organization: OrganizationService,
        opportunities: OpportunityService,
        employees: EmployeeService,
        organization_actor: OrganizationActor,
        opportunity_actor: OpportunityActor,
        employee_actor: EmployeeActor,
    ) -> None:
        required = (
            (demand, "get_hypothesis"),
            (demand, "get_need"),
            (prospecting, "get_account"),
            (prospecting, "get_contact_point"),
            (organization, "get_playbook"),
            (opportunities, "create_from_need"),
            (opportunities, "assign"),
            (opportunities, "get_by_need"),
            (opportunities, "transition"),
            (employees, "resolve_owner"),
        )
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or any(not callable(getattr(value, name, None)) for value, name in required)
            or not isinstance(organization_actor, OrganizationActor)
            or not isinstance(opportunity_actor, OpportunityActor)
            or not isinstance(employee_actor, EmployeeActor)
            or opportunity_actor.role != "boss"
            or employee_actor.role != "boss"
            or organization_actor.role != "boss"
            or opportunity_actor.actor_id != employee_actor.actor_id
            or opportunity_actor.actor_id != organization_actor.actor_id
        ):
            raise ValidationError("回复机会创建依赖无效")
        self._tenant_id = tenant_id
        self._demand = demand
        self._prospecting = prospecting
        self._organization = organization
        self._opportunities = opportunities
        self._employees = employees
        self._organization_actor = organization_actor
        self._opportunity_actor = opportunity_actor
        self._employee_actor = employee_actor

    @staticmethod
    def _reply_provenance(
        field: object,
        context: ReplyActionContext,
        evidence: ReplyEvidenceSnapshot,
    ) -> Provenance:
        source_ref = getattr(field, "source_ref", None)
        source_quote = getattr(field, "source_quote", None)
        if (
            source_ref != str(context.message_id)
            or not isinstance(source_quote, str)
            or not source_quote.strip()
        ):
            raise ValidationError("回复需求字段来源不匹配")
        if not any(
            candidate.quote == source_quote
            for candidate in evidence.candidate_fields
            if candidate.field == getattr(field, "name", None)
        ):
            raise ValidationError("回复需求字段逐字证据不匹配")
        return Provenance(
            source_type=SourceType.CONVERSATION,
            source_id=str(context.message_id),
            extracted_by=evidence.classified_by,
            extracted_at=evidence.classified_at,
            source_quote=source_quote,
        )

    async def create_and_assign(
        self,
        tenant_id: TenantId,
        context: ReplyActionContext,
        hypothesis_id: NeedHypothesisId,
        need_id: ValidatedNeedId,
        evidence: ReplyEvidenceSnapshot,
    ) -> OpportunityId | None:
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("回复机会创建跨租户执行")
        hypothesis = await self._demand.get_hypothesis(tenant_id, hypothesis_id)
        need = await self._demand.get_need(tenant_id, need_id)
        account = await self._prospecting.get_account(tenant_id, context.account_id)
        contact = await self._prospecting.get_contact_point(
            tenant_id, context.contact_point_id
        )
        if (
            hypothesis.hypothesis_id != str(hypothesis_id)
            or hypothesis.account_id != str(context.account_id)
            or hypothesis.validated_need_id != str(need_id)
            or need.need_id != str(need_id)
            or need.account_id != str(context.account_id)
            or account.tenant_id != tenant_id
            or account.account_id != context.account_id
            or contact.tenant_id != tenant_id
            or contact.contact_point_id != context.contact_point_id
            or contact.account_id != context.account_id
            or getattr(contact.verification, "value", None) != "verified"
            or contact.verified_at is None
        ):
            raise ValidationError("回复机会来源链不匹配")

        existing = await self._opportunities.get_by_need(
            tenant_id, need_id, actor=self._opportunity_actor
        )

        playbook = await self._organization.get_playbook(
            tenant_id, actor=self._organization_actor
        )
        if playbook.tenant_id != tenant_id:
            raise TenantIsolationViolation("回复机会 Playbook 跨租户")

        fields = {item.name: item for item in need.fields}
        product_field = fields.get("product_category")
        if product_field is None:
            raise ValidationError("回复需求缺少产品类别证据")
        reply_provenance = {
            name: self._reply_provenance(field, context, evidence)
            for name, field in fields.items()
        }
        account_provenance = getattr(account, "field_provenance", None)
        if (
            not isinstance(account_provenance, dict)
            or set(account_provenance) != {"name", "country"}
            or any(
                not isinstance(value, Provenance)
                or value.source_type is SourceType.AGENT_INFERENCE
                for value in account_provenance.values()
            )
        ):
            raise ValidationError("企业关键字段来源不完整或无效")
        target_price = need.target_price
        estimated_order_value = (
            target_price.multiply(need.quantity)
            if target_price is not None and need.quantity is not None
            else None
        )
        category_key = need.product_category.casefold()
        country_key = account.country.casefold()
        category_allowed = category_key not in set(
            playbook.excluded_categories
        ) and country_key not in set(playbook.excluded_countries)
        field_provenance = {
            "account_name": account_provenance["name"],
            "country": account_provenance["country"],
        }
        for request_name, need_name in (
            ("quantity", "quantity"),
            ("application", "application"),
            ("destination", "destination"),
            ("required_by", "required_by"),
            ("target_price", "target_price"),
            ("current_supply_problem", "current_supply_issue"),
        ):
            if need_name in reply_provenance:
                field_provenance[request_name] = reply_provenance[need_name]

        level = (
            EvidenceLevel.CUSTOMER_SPECIFICATION
            if evidence.category == "provides_specification"
            else EvidenceLevel.CUSTOMER_INTEREST_REPLY
        )
        if existing is None:
            opportunity_id = await self._opportunities.create_from_need(
                tenant_id,
                OpportunityCreateRequest(
                    need_id=str(need_id),
                    account_id=str(context.account_id),
                    account_name=account.name,
                    country=account.country,
                    product_category=need.product_category,
                    evidence_tier=level.value,
                    has_verified_contact=True,
                    category_allowed=category_allowed,
                    minimum_order_value=playbook.minimum_deal_value,
                    supply_available=None,
                    field_provenance=field_provenance,
                    quantity=need.quantity,
                    application=(
                        fields["application"].value if "application" in fields else None
                    ),
                    destination=need.destination,
                    required_by=need.required_by,
                    target_price=target_price,
                    current_supply_problem=(
                        fields["current_supply_issue"].value
                        if "current_supply_issue" in fields
                        else None
                    ),
                    estimated_order_value=estimated_order_value,
                ),
                ValidatedNeedEvidence(
                    level=level,
                    provenance=reply_provenance["product_category"],
                ),
                actor=self._opportunity_actor,
            )
            if opportunity_id is None:
                return None
        else:
            opportunity_id = OpportunityId(str(existing.opportunity_id))
        ownership = await self._employees.resolve_owner(
            tenant_id,
            ProspectAccountId(str(context.account_id)),
            actor=self._employee_actor,
            country=account.country,
            need_category=need.product_category,
        )
        current = await self._opportunities.get_by_need(
            tenant_id, need_id, actor=self._opportunity_actor
        )
        if current is None:
            raise ValidationError("回复机会创建结果不可读")
        if current.owner is None:
            await self._opportunities.assign(
                tenant_id,
                opportunity_id,
                ownership.owner,
                EmployeeId(self._opportunity_actor.actor_id),
                actor=self._opportunity_actor,
            )
            current = await self._opportunities.get_by_need(
                tenant_id, need_id, actor=self._opportunity_actor
            )
            if current is None:
                raise ValidationError("回复机会分配结果不可读")
        if str(current.owner) != str(ownership.owner):
            raise ValidationError("回复机会负责人关联冲突")
        state = getattr(current.state, "value", current.state)
        if state == OpportunityState.QUALIFIED.value:
            await self._opportunities.transition(
                tenant_id,
                opportunity_id,
                OpportunityState.ASSIGNED,
                actor=self._opportunity_actor,
            )
            current = await self._opportunities.get_by_need(
                tenant_id, need_id, actor=self._opportunity_actor
            )
            if current is None:
                raise ValidationError("回复机会状态结果不可读")
            state = getattr(current.state, "value", current.state)
        if state != OpportunityState.ASSIGNED.value:
            raise ValidationError("回复机会未进入 assigned 状态")
        return OpportunityId(str(opportunity_id))


__all__ = ("DurableReplyOpportunityIntake",)
