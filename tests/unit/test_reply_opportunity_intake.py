"""回复首次晋升后的机会创建/归属组合。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from apps.scheduler_worker.adapters.reply_opportunity_intake import (
    DurableReplyOpportunityIntake,
)
from apps.scheduler_worker.reply_actions import (
    ReplyEvidenceSnapshot,
    ReplyFieldSnapshot,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope, ScopeLevel
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from shared.schemas.identifiers import (
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    MessageId,
    NeedHypothesisId,
    OpportunityId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money
from workflows.reply_qualification.ports import ReplyActionContext

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
BOSS = EmployeeId(new_id("emp"))
OWNER = EmployeeId(new_id("emp"))
ACCOUNT = ProspectAccountId(new_id("acc"))
CONTACT = ContactPointId(new_id("cp"))
HYPOTHESIS = NeedHypothesisId(new_id("hyp"))
NEED = ValidatedNeedId(new_id("need"))
OPPORTUNITY = OpportunityId(new_id("opp"))
MESSAGE = MessageId(new_id("msg"))
QUOTE = "We need 5000 hinges at USD 2 each."
PAGE_HASH = "a" * 64
CONTEXT = ReplyActionContext(
    message_id=MESSAGE,
    outbound_message_id=OutboundMessageId("<reply.test@messages.tradeos.invalid>"),
    enrollment_id=EnrollmentId(new_id("enr")),
    account_id=ACCOUNT,
    contact_point_id=CONTACT,
)
EVIDENCE = ReplyEvidenceSnapshot(
    message_id=MESSAGE,
    category="provides_specification",
    classified_by="controlled-model-v1",
    classified_at=NOW,
    raw_artifact_ref="art_reply_intake",
    outbound_message_id=CONTEXT.outbound_message_id,
    candidate_fields=(
        ReplyFieldSnapshot("product_category", "hinges", QUOTE),
        ReplyFieldSnapshot("quantity", "5000", QUOTE),
        ReplyFieldSnapshot(
            "target_price", '{"amount":"2","currency":"USD"}', QUOTE
        ),
    ),
)


class _Demand:
    async def get_hypothesis(self, tenant_id, hypothesis_id):
        del tenant_id
        return SimpleNamespace(
            hypothesis_id=str(hypothesis_id),
            account_id=str(ACCOUNT),
            validated_need_id=str(NEED),
            evidence=[
                SimpleNamespace(
                    source_ref=PAGE_HASH,
                    source_url="https://acme.example/expansion",
                    observed_at=NOW,
                )
            ],
        )

    async def get_need(self, tenant_id, need_id):
        del tenant_id
        return SimpleNamespace(
            need_id=str(need_id),
            account_id=str(ACCOUNT),
            product_category="hinges",
            fields=[
                SimpleNamespace(
                    name="product_category",
                    value="hinges",
                    source_ref=str(MESSAGE),
                    source_quote=QUOTE,
                ),
                SimpleNamespace(
                    name="quantity",
                    value="5000",
                    source_ref=str(MESSAGE),
                    source_quote=QUOTE,
                ),
                SimpleNamespace(
                    name="target_price",
                    value="2 USD",
                    source_ref=str(MESSAGE),
                    source_quote=QUOTE,
                ),
            ],
            quantity=5000,
            destination=None,
            required_by=None,
            target_price=Money(Decimal(2), CurrencyCode("USD")),
        )


class _Prospecting:
    async def get_account(self, tenant_id, account_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            account_id=account_id,
            name="Acme Components",
            country="US",
        )

    async def get_contact_point(self, tenant_id, contact_point_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            contact_point_id=contact_point_id,
            account_id=ACCOUNT,
            verification=SimpleNamespace(value="verified"),
            verified_at=NOW,
        )


class _Organization:
    async def get_playbook(self, tenant_id, *, actor):
        del actor
        return SimpleNamespace(
            tenant_id=tenant_id,
            minimum_deal_value=Money(Decimal(1000), CurrencyCode("USD")),
            excluded_categories=(),
            excluded_countries=(),
        )


class _Opportunities:
    def __init__(self) -> None:
        self.created = []
        self.assigned = []

    async def get_by_need(self, tenant_id, need_id, *, actor):
        del tenant_id, need_id, actor

    async def create_from_need(self, tenant_id, request, evidence, *, actor):
        self.created.append((tenant_id, request, evidence, actor))
        return OPPORTUNITY

    async def assign(
        self, tenant_id, opportunity_id, owner, assigned_by, *, actor
    ):
        self.assigned.append(
            (tenant_id, opportunity_id, owner, assigned_by, actor)
        )


class _Employees:
    async def resolve_owner(
        self, tenant_id, account_id, *, actor, country, need_category
    ):
        del tenant_id, account_id, actor, country, need_category
        return SimpleNamespace(owner=OWNER)


class _ExistingUnassignedOpportunities(_Opportunities):
    async def get_by_need(self, tenant_id, need_id, *, actor):
        del tenant_id, need_id, actor
        return SimpleNamespace(opportunity_id=OPPORTUNITY, owner=None)


def _intake(opportunities: _Opportunities) -> DurableReplyOpportunityIntake:
    return DurableReplyOpportunityIntake(
        tenant_id=TENANT,
        demand=_Demand(),
        prospecting=_Prospecting(),
        organization=_Organization(),
        opportunities=opportunities,
        employees=_Employees(),
        organization_actor=OrganizationActor(
            str(BOSS),
            OrganizationScope(OrganizationScopeLevel.TENANT, TENANT),
            "boss",
        ),
        opportunity_actor=OpportunityActor(
            str(BOSS), OpportunityScope(level=ScopeLevel.TENANT), "boss"
        ),
        employee_actor=EmployeeActor(str(BOSS), EmployeeScope.TENANT, "boss"),
    )


async def test_intake_uses_reply_money_playbook_and_employee_owner() -> None:
    opportunities = _Opportunities()

    result = await _intake(opportunities).create_and_assign(
        TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
    )

    assert result == OPPORTUNITY
    assert len(opportunities.created) == 1
    request = opportunities.created[0][1]
    assert request.estimated_order_value == Money(
        Decimal(10000), CurrencyCode("USD")
    )
    assert request.minimum_order_value == Money(
        Decimal(1000), CurrencyCode("USD")
    )
    assert request.has_verified_contact is True
    assert request.category_allowed is True
    assert request.field_provenance["account_name"].page_hash == PAGE_HASH
    assert request.field_provenance["quantity"].source_id == MESSAGE
    assert opportunities.assigned[0][2:4] == (OWNER, BOSS)


async def test_intake_replay_assigns_an_existing_unassigned_opportunity() -> None:
    opportunities = _ExistingUnassignedOpportunities()

    result = await _intake(opportunities).create_and_assign(
        TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
    )

    assert result == OPPORTUNITY
    assert opportunities.assigned[0][1:4] == (OPPORTUNITY, OWNER, BOSS)
