"""回复首次晋升后的机会创建/归属组合。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

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
from domains.opportunities.service import OpportunityState
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from shared.errors import ValidationError
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
from shared.schemas.provenance import Provenance, SourceType
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
HISTORICAL_MESSAGE = MessageId(new_id("msg"))
QUOTE = "We need 5000 hinges at USD 2 each."
HISTORICAL_QUOTE = "We source industrial hinges for our assembly line."
PAGE_HASH = "a" * 64
ACCOUNT_NAME_PROVENANCE = Provenance(
    source_type=SourceType.WEB_PAGE,
    source_id="b" * 64,
    extracted_by="account-name-extractor-v2",
    extracted_at=NOW,
    source_url="https://acme.example/about",
    page_hash="b" * 64,
    source_quote="Acme Components manufactures industrial hardware.",
)
COUNTRY_PROVENANCE = Provenance(
    source_type=SourceType.WEB_PAGE,
    source_id="c" * 64,
    extracted_by="country-extractor-v7",
    extracted_at=NOW,
    source_url="https://acme.example/contact",
    page_hash="c" * 64,
    source_quote="Headquarters: Cleveland, United States.",
)
CONTEXT = ReplyActionContext(
    message_id=MESSAGE,
    outbound_message_id=OutboundMessageId("<reply.test@messages.tradeos.invalid>"),
    enrollment_id=EnrollmentId(new_id("enr")),
    account_id=ACCOUNT,
    contact_point_id=CONTACT,
)
EVIDENCE = ReplyEvidenceSnapshot(
    message_id=MESSAGE,
    account_id=ACCOUNT,
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

HISTORICAL_EVIDENCE = ReplyEvidenceSnapshot(
    message_id=HISTORICAL_MESSAGE,
    account_id=ACCOUNT,
    category="provides_specification",
    classified_by="controlled-model-v0",
    classified_at=NOW,
    raw_artifact_ref="art_reply_intake_historical",
    outbound_message_id=CONTEXT.outbound_message_id,
    candidate_fields=(
        ReplyFieldSnapshot(
            "product_category", "hinges", HISTORICAL_QUOTE
        ),
    ),
)


class _DurableEvidence:
    def __init__(self, *, include_historical: bool = True) -> None:
        self._snapshots = {MESSAGE: EVIDENCE}
        if include_historical:
            self._snapshots[HISTORICAL_MESSAGE] = HISTORICAL_EVIDENCE
        self.calls: list[MessageId] = []

    async def load(self, tenant_id, message_id):
        assert tenant_id == TENANT
        self.calls.append(message_id)
        return self._snapshots.get(message_id)


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
            field_provenance={
                "name": ACCOUNT_NAME_PROVENANCE,
                "country": COUNTRY_PROVENANCE,
            },
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
        self.transitions = []
        self.owner = None
        self.state = "qualified"
        self.exists = False

    async def get_by_need(self, tenant_id, need_id, *, actor):
        del tenant_id, need_id, actor
        if not self.exists:
            return None
        return SimpleNamespace(
            opportunity_id=OPPORTUNITY,
            owner=self.owner,
            state=self.state,
        )

    async def create_from_need(self, tenant_id, request, evidence, *, actor):
        self.created.append((tenant_id, request, evidence, actor))
        self.exists = True
        return OPPORTUNITY

    async def assign(
        self, tenant_id, opportunity_id, owner, assigned_by, *, actor
    ):
        self.assigned.append(
            (tenant_id, opportunity_id, owner, assigned_by, actor)
        )
        self.owner = owner

    async def transition(self, tenant_id, opportunity_id, target, *, actor):
        self.transitions.append((tenant_id, opportunity_id, target, actor))
        assert self.owner == OWNER
        assert self.state == "qualified"
        assert target is OpportunityState.ASSIGNED
        self.state = "assigned"


class _Employees:
    async def resolve_owner(
        self, tenant_id, account_id, *, actor, country, need_category
    ):
        del tenant_id, account_id, actor, country, need_category
        return SimpleNamespace(owner=OWNER)


class _ExistingUnassignedOpportunities(_Opportunities):
    def __init__(self) -> None:
        super().__init__()
        self.exists = True

    async def get_by_need(self, tenant_id, need_id, *, actor):
        return await super().get_by_need(tenant_id, need_id, actor=actor)


class _ExistingOwnerBeforeTransitionOpportunities(_ExistingUnassignedOpportunities):
    def __init__(self) -> None:
        super().__init__()
        self.owner = OWNER


def _intake(opportunities: _Opportunities) -> DurableReplyOpportunityIntake:
    return DurableReplyOpportunityIntake(
        tenant_id=TENANT,
        demand=_Demand(),
        prospecting=_Prospecting(),
        organization=_Organization(),
        opportunities=opportunities,
        employees=_Employees(),
        evidence_reader=_DurableEvidence(),
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


def _intake_with_evidence(
    opportunities: _Opportunities,
    *,
    demand: object,
    evidence: object,
) -> DurableReplyOpportunityIntake:
    return DurableReplyOpportunityIntake(
        tenant_id=TENANT,
        demand=demand,
        prospecting=_Prospecting(),
        organization=_Organization(),
        opportunities=opportunities,
        employees=_Employees(),
        evidence_reader=evidence,
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
    assert request.field_provenance["account_name"] == ACCOUNT_NAME_PROVENANCE
    assert request.field_provenance["country"] == COUNTRY_PROVENANCE
    assert request.field_provenance["account_name"].extracted_by == (
        "account-name-extractor-v2"
    )
    assert request.field_provenance["quantity"].source_id == MESSAGE
    assert opportunities.assigned[0][2:4] == (OWNER, BOSS)
    assert opportunities.state == "assigned"


async def test_intake_replay_assigns_an_existing_unassigned_opportunity() -> None:
    opportunities = _ExistingUnassignedOpportunities()

    result = await _intake(opportunities).create_and_assign(
        TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
    )

    assert result == OPPORTUNITY
    assert opportunities.assigned[0][1:4] == (OPPORTUNITY, OWNER, BOSS)
    assert opportunities.state == "assigned"


async def test_intake_recovers_when_owner_commit_precedes_assigned_transition() -> None:
    opportunities = _ExistingOwnerBeforeTransitionOpportunities()

    result = await _intake(opportunities).create_and_assign(
        TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
    )

    assert result == OPPORTUNITY
    assert opportunities.assigned == []
    assert opportunities.state == "assigned"
    assert opportunities.transitions[0][2] is OpportunityState.ASSIGNED


async def test_existing_opportunity_does_not_revalidate_historical_fields_as_current() -> None:
    """第二条回复只恢复机会归属/状态；历史 Need 字段不必来自当前消息。"""
    class HistoricalDemand(_Demand):
        async def get_need(self, tenant_id, need_id):
            need = await super().get_need(tenant_id, need_id)
            for field in need.fields:
                field.source_ref = str(HISTORICAL_MESSAGE)
                field.source_quote = HISTORICAL_QUOTE
            return need

    class EvidenceMustNotBeRead:
        async def load(self, tenant_id, message_id):
            raise AssertionError(
                f"existing Opportunity 不应重验历史字段：{tenant_id}/{message_id}"
            )

    opportunities = _ExistingOwnerBeforeTransitionOpportunities()
    intake = _intake_with_evidence(
        opportunities,
        demand=HistoricalDemand(),
        evidence=EvidenceMustNotBeRead(),
    )

    result = await intake.create_and_assign(
        TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
    )

    assert result == OPPORTUNITY
    assert opportunities.created == []
    assert opportunities.assigned == []
    assert opportunities.state == "assigned"


async def test_new_opportunity_uses_each_fields_durable_message_provenance() -> None:
    """跨多条已验证回复积累的事实保持各自 source_id/quote/classifier。"""
    class CrossMessageDemand(_Demand):
        async def get_need(self, tenant_id, need_id):
            need = await super().get_need(tenant_id, need_id)
            product = next(
                field for field in need.fields if field.name == "product_category"
            )
            product.source_ref = str(HISTORICAL_MESSAGE)
            product.source_quote = HISTORICAL_QUOTE
            return need

    opportunities = _Opportunities()
    evidence = _DurableEvidence()
    intake = _intake_with_evidence(
        opportunities,
        demand=CrossMessageDemand(),
        evidence=evidence,
    )

    result = await intake.create_and_assign(
        TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
    )

    assert result == OPPORTUNITY
    request = opportunities.created[0][1]
    validated_need_evidence = opportunities.created[0][2]
    assert validated_need_evidence.provenance.source_id == HISTORICAL_MESSAGE
    assert validated_need_evidence.provenance.source_quote == HISTORICAL_QUOTE
    assert validated_need_evidence.provenance.extracted_by == "controlled-model-v0"
    assert request.field_provenance["quantity"].source_id == MESSAGE
    assert request.field_provenance["quantity"].source_quote == QUOTE
    assert set(evidence.calls) == {HISTORICAL_MESSAGE, MESSAGE}


async def test_new_opportunity_fails_closed_when_historical_field_is_not_verified() -> None:
    class CrossMessageDemand(_Demand):
        async def get_need(self, tenant_id, need_id):
            need = await super().get_need(tenant_id, need_id)
            product = next(
                field for field in need.fields if field.name == "product_category"
            )
            product.source_ref = str(HISTORICAL_MESSAGE)
            product.source_quote = HISTORICAL_QUOTE
            return need

    intake = _intake_with_evidence(
        _Opportunities(),
        demand=CrossMessageDemand(),
        evidence=_DurableEvidence(include_historical=False),
    )

    with pytest.raises(ValidationError, match="持久分类证据不存在"):
        await intake.create_and_assign(
            TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
        )


@pytest.mark.parametrize("source_quote", (None, "fabricated historical quote"))
async def test_new_opportunity_fails_closed_on_missing_or_inexact_field_quote(
    source_quote: str | None,
) -> None:
    class InvalidQuoteDemand(_Demand):
        async def get_need(self, tenant_id, need_id):
            need = await super().get_need(tenant_id, need_id)
            product = next(
                field for field in need.fields if field.name == "product_category"
            )
            product.source_ref = str(HISTORICAL_MESSAGE)
            product.source_quote = source_quote
            return need

    intake = _intake_with_evidence(
        _Opportunities(),
        demand=InvalidQuoteDemand(),
        evidence=_DurableEvidence(),
    )

    with pytest.raises(ValidationError, match="持久来源不完整|逐字证据不匹配"):
        await intake.create_and_assign(
            TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
        )


async def test_intake_rejects_account_without_field_specific_provenance() -> None:
    class MissingCountryProvenance(_Prospecting):
        async def get_account(self, tenant_id, account_id):
            account = await super().get_account(tenant_id, account_id)
            account.field_provenance = {"name": ACCOUNT_NAME_PROVENANCE}
            return account

    intake = _intake(_Opportunities())
    intake._prospecting = MissingCountryProvenance()

    with pytest.raises(ValidationError, match="企业关键字段来源"):
        await intake.create_and_assign(
            TENANT, CONTEXT, HYPOTHESIS, NEED, EVIDENCE
        )
