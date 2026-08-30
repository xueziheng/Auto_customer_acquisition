"""Sourcing V2 强类型合同与域不变量。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.sourcing import models as sourcing_models
from domains.sourcing import schemas as sourcing_schemas
from shared.errors import InvalidStateTransition
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 8, 30, 9, tzinfo=UTC)
PROVENANCE = ProvenanceSummary(
    source_type=SourceType.CONVERSATION,
    source_id="msg-customer-a",
    extracted_by="human",
    extracted_at=NOW,
    confirmed_by=EmployeeId("employee-a"),
    confirmed_at=NOW,
)


def _type(module: object, name: str) -> type:
    value = getattr(module, name, None)
    if value is None:
        pytest.fail(f"RED：{name} 尚未实现")
    assert isinstance(value, type)
    return value


def _plan_command() -> object:
    command_type = _type(sourcing_schemas, "PublicSourcingPlanCommand")
    return command_type(
        plan_id=SourcingPlanId("spl-plan-a"),
        case_id=SourcingCaseId("src-case-a"),
        target_countries=("DE", "FR"),
        product_category="stainless steel hinge",
        queries=("Germany stainless steel hinge supplier",),
        max_search_queries=1,
        max_pages_read=4,
        provider="tavily",
        search_depth="basic",
        usage_credits_remaining=20,
        worst_case_credits=4,
        version=1,
        expected_case_version=1,
    )


def _price_option(
    *,
    minimum_quantity: int,
    source_kind: str,
    currency: str = "USD",
    unit: str = "piece",
) -> object:
    option_type = _type(sourcing_schemas, "SourcingCostPriceOption")
    return option_type(
        minimum_quantity=minimum_quantity,
        unit_amount=Decimal("1.25"),
        currency=currency,
        unit=unit,
        evidence_ref=ArtifactId(f"art-{minimum_quantity}"),
        source_kind=source_kind,
    )


def _handoff(*, supplier_candidate: bool, price_options: tuple[object, ...]) -> object:
    snapshot_type = _type(sourcing_schemas, "SourcingHandoffSnapshot")
    return snapshot_type(
        case_id=SourcingCaseId("src-case-a"),
        review_id=SourcingReviewId("srv-review-a"),
        need_id=ValidatedNeedId("need-a"),
        opportunity_id=OpportunityId("opp-a"),
        primary_option_id=SourcingSupplyOptionId("sop-a"),
        product_id=ProductId("prd-a"),
        supplier_candidate_id=(
            SupplierCandidateId("cand-a") if supplier_candidate else None
        ),
        quantity=1_000,
        moq=500,
        price_options=price_options,
    )


def test_public_plan_confirmation_is_bound_to_exact_hash() -> None:
    plan_type = _type(sourcing_models, "PublicSourcingPlan")
    status_type = _type(sourcing_models, "PublicPlanStatus")
    stale_error = _type(__import__("domains.sourcing.errors", fromlist=["SourcingPlanStaleError"]), "SourcingPlanStaleError")

    plan = plan_type.create(_plan_command(), created_at=NOW)
    confirmed = plan.confirm(EmployeeId("boss-a"), confirmed_at=NOW)

    assert confirmed.status is status_type.AUTHORIZED
    assert confirmed.authorized_plan_hash == confirmed.plan_hash
    with pytest.raises(stale_error):
        confirmed.replace_scope(max_pages_read=confirmed.max_pages_read + 1)


def test_unconfirmed_plan_scope_change_creates_new_version_and_hash() -> None:
    plan_type = _type(sourcing_models, "PublicSourcingPlan")
    status_type = _type(sourcing_models, "PublicPlanStatus")
    plan = plan_type.create(_plan_command(), created_at=NOW)

    replacement = plan.replace_scope(max_pages_read=5)

    assert replacement.version == 2
    assert replacement.status is status_type.PENDING_CONFIRMATION
    assert replacement.plan_hash != plan.plan_hash
    assert replacement.confirmed_by is None
    assert replacement.authorized_plan_hash is None


def test_sourcing_case_state_transitions_are_explicit_and_fail_closed() -> None:
    case = sourcing_models.SourcingCase(
        SourcingCaseId("src-case-a"),
        sourcing_models.TenantId("tenant-a"),
        ValidatedNeedId("need-a"),
        NOW,
    )

    case.transition_to(sourcing_models.CaseState.DISCOVERING, changed_at=NOW)
    assert case.state is sourcing_models.CaseState.DISCOVERING
    with pytest.raises(InvalidStateTransition, match="discovering.*handed_to_costing"):
        case.transition_to(sourcing_models.CaseState.HANDED_TO_COSTING, changed_at=NOW)


def test_review_requires_one_primary_and_at_most_two_unique_alternates() -> None:
    command_type = _type(sourcing_schemas, "SourcingReviewCommand")

    with pytest.raises(PydanticValidationError):
        command_type(primary_option_id="", alternate_option_ids=())
    with pytest.raises(PydanticValidationError):
        command_type(
            primary_option_id="sop-primary",
            alternate_option_ids=("sop-a", "sop-b", "sop-c"),
            reason="三项备选超出上限",
            expected_case_version=1,
        )
    with pytest.raises(PydanticValidationError):
        command_type(
            primary_option_id="sop-primary",
            alternate_option_ids=("sop-primary",),
            reason="主选不能重复成为备选",
            expected_case_version=1,
        )


def test_need_snapshot_keeps_field_provenance_and_rejects_inference_as_fact() -> None:
    fact_type = _type(sourcing_schemas, "NeedFact")
    snapshot_type = _type(sourcing_schemas, "SourcingNeedSnapshot")
    category = fact_type(value="hinge", provenance=PROVENANCE)
    quantity = fact_type(value=1000, provenance=PROVENANCE)

    snapshot = snapshot_type(
        need_id=ValidatedNeedId("need-a"),
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=category,
        quantity=quantity,
        snapshot_hash="a" * 64,
    )
    assert snapshot.quantity.provenance.source_id == "msg-customer-a"

    inferred = PROVENANCE.model_copy(
        update={"source_type": SourceType.AGENT_INFERENCE}
    )
    with pytest.raises(PydanticValidationError, match="AGENT_INFERENCE"):
        fact_type(value="hinge", provenance=inferred)


def test_supplier_claim_cannot_be_agent_inference() -> None:
    claim_type = _type(sourcing_schemas, "SourcingSupplierClaim")
    inferred = PROVENANCE.model_copy(
        update={"source_type": SourceType.AGENT_INFERENCE}
    )

    with pytest.raises(PydanticValidationError, match="supplier_claims"):
        claim_type(
            value="Factory direct",
            provenance=inferred,
            evidence_ref=ArtifactId("art-claim-a"),
        )


@pytest.mark.parametrize(
    "amount",
    [Decimal(0), Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")],
)
def test_cost_price_option_requires_finite_positive_decimal(amount: Decimal) -> None:
    option_type = _type(sourcing_schemas, "SourcingCostPriceOption")
    with pytest.raises(PydanticValidationError):
        option_type(
            minimum_quantity=1,
            unit_amount=amount,
            currency="USD",
            unit="piece",
            evidence_ref=ArtifactId("art-a"),
            source_kind="existing_product",
        )


def test_handoff_supports_existing_product_and_supplier_candidate_paths() -> None:
    existing = _handoff(
        supplier_candidate=False,
        price_options=(
            _price_option(minimum_quantity=1, source_kind="existing_product"),
        ),
    )
    candidate = _handoff(
        supplier_candidate=True,
        price_options=(
            _price_option(minimum_quantity=500, source_kind="supplier_candidate"),
            _price_option(minimum_quantity=1_000, source_kind="supplier_candidate"),
        ),
    )

    assert existing.supplier_candidate_id is None
    assert len(candidate.price_options) == 2


@pytest.mark.parametrize(
    "option_specs,supplier_candidate",
    [
        (
            (
                (500, "supplier_candidate", "USD", "piece"),
                (500, "supplier_candidate", "USD", "piece"),
            ),
            True,
        ),
        (
            (
                (500, "supplier_candidate", "USD", "piece"),
                (1_000, "supplier_candidate", "EUR", "piece"),
            ),
            True,
        ),
        (
            (
                (500, "supplier_candidate", "USD", "piece"),
                (1_000, "supplier_candidate", "USD", "set"),
            ),
            True,
        ),
        (
            (
                (500, "supplier_candidate", "USD", "piece"),
                (1, "existing_product", "USD", "piece"),
            ),
            True,
        ),
        (
            ((1, "existing_product", "USD", "piece"),),
            True,
        ),
    ],
)
def test_handoff_rejects_ambiguous_price_dimensions_or_source_path(
    option_specs: tuple[tuple[int, str, str, str], ...], supplier_candidate: bool
) -> None:
    price_options = tuple(
        _price_option(
            minimum_quantity=minimum_quantity,
            source_kind=source_kind,
            currency=currency,
            unit=unit,
        )
        for minimum_quantity, source_kind, currency, unit in option_specs
    )
    with pytest.raises(PydanticValidationError):
        _handoff(
            supplier_candidate=supplier_candidate,
            price_options=price_options,
        )


def test_v2_candidate_write_uses_only_indicative_price_tiers() -> None:
    submission_type = _type(sourcing_schemas, "CandidateSubmission")
    valid = {
        "supplier_name": "Supplier A",
        "product_title": "Hinge",
        "source_platform": "supplier.example",
        "specs": (),
        "indicative_price_tiers": {
            1000: Money(Decimal("1.25"), CurrencyCode("USD"))
        },
        "moq": 500,
        "price_unit": "piece",
        "currency": "USD",
        "evidence_url": "https://supplier.example/hinge",
        "evidence_hash": "b" * 64,
        "evidence_artifact_ref": "art-a",
    }
    submission = submission_type.model_validate(valid)
    assert 1000 in submission.indicative_price_tiers
    with pytest.raises(PydanticValidationError):
        submission_type.model_validate(
            {**valid, "quoted_prices": valid["indicative_price_tiers"]}
        )


def test_public_commands_reject_identity_fields_and_are_frozen() -> None:
    open_type = _type(sourcing_schemas, "OpenSourcingCase")
    fact_type = _type(sourcing_schemas, "NeedFact")
    snapshot_type = _type(sourcing_schemas, "SourcingNeedSnapshot")
    need = snapshot_type(
        need_id=ValidatedNeedId("need-a"),
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=fact_type(value="hinge", provenance=PROVENANCE),
        quantity=fact_type(value=1000, provenance=PROVENANCE),
        snapshot_hash="a" * 64,
    )
    command = open_type(need=need, trigger_key="tenant-a:need-a:v2")
    with pytest.raises(PydanticValidationError):
        open_type.model_validate(
            {
                "need": need,
                "trigger_key": "tenant-a:need-a:v2",
                "tenant_id": "tenant-forged",
                "actor_id": "boss-forged",
            }
        )
    with pytest.raises(PydanticValidationError):
        command.trigger_key = "changed"  # type: ignore[misc]
