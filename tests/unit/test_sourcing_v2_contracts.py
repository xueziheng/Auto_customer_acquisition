"""Sourcing V2 强类型合同与域不变量。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.sourcing import models as sourcing_models
from domains.sourcing import schemas as sourcing_schemas
from shared.errors import InvalidStateTransition
from shared.errors import ValidationError as DomainValidationError
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
    TenantId,
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

EXPECTED_STOP_CODES = {
    "approval_required",
    "quota_status_unknown",
    "paid_usage_enabled",
    "quota_exhausted",
    "provider_timeout",
    "provider_rate_limited",
    "page_access_forbidden",
    "login_or_captcha",
    "unsafe_redirect",
    "no_search_results",
    "no_verifiable_supplier",
    "no_qualified_candidate",
    "reconciliation_required",
    "opportunity_required",
    "need_incomplete",
    "plan_confirmation_required",
    "free_quota_unavailable",
    "budget_exhausted",
    "no_qualified_supply",
    "manual_stop",
}


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


def _candidate_submission_payload() -> dict[str, object]:
    return {
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


def test_stop_codes_use_lifecycle_and_binding_public_search_vocabulary() -> None:
    stop_code_type = _type(sourcing_models, "SourcingStopCode")

    assert {item.value for item in stop_code_type} == EXPECTED_STOP_CODES


@pytest.mark.parametrize("stop_code", sorted(EXPECTED_STOP_CODES))
def test_stop_code_accepts_and_roundtrips_every_contract_value(stop_code: str) -> None:
    stop_code_type = _type(sourcing_models, "SourcingStopCode")

    assert stop_code_type(stop_code).value == stop_code


@pytest.mark.parametrize(
    "shorthand", ("usage_unknown", "paid_enabled", "request_uncertain", "no_results")
)
def test_stop_code_rejects_every_draft_shorthand(shorthand: str) -> None:
    stop_code_type = _type(sourcing_models, "SourcingStopCode")

    with pytest.raises(ValueError):
        stop_code_type(shorthand)


def test_stop_detail_is_a_safe_structured_value_and_roundtrips() -> None:
    detail_type = _type(sourcing_models, "SourcingStopDetail")
    stage_type = _type(sourcing_models, "SourcingStopStage")
    stop_code_type = _type(sourcing_models, "SourcingStopCode")
    case_type = _type(sourcing_models, "SourcingCase")
    detail = detail_type(
        stage=stage_type.PROVIDER,
        query_index=2,
        provider_http_status=429,
        observed_count=3,
        configured_limit=4,
    )

    restored = detail_type(**asdict(detail))
    case = case_type(
        SourcingCaseId("src-case-stop"),
        TenantId("tenant-a"),
        ValidatedNeedId("need-stop"),
        NOW,
        stop_code=stop_code_type.PROVIDER_RATE_LIMITED,
        stop_detail=restored,
    )

    assert restored == detail
    assert case.stop_detail == detail
    with pytest.raises(DomainValidationError, match="stop_detail"):
        case_type(
            SourcingCaseId("src-case-unsafe"),
            TenantId("tenant-a"),
            ValidatedNeedId("need-stop"),
            NOW,
            stop_code=stop_code_type.PROVIDER_TIMEOUT,
            stop_detail="raw provider exception with request payload",
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("query_index", True),
        ("query_index", -1),
        ("query_index", 0.5),
        ("provider_http_status", 99),
        ("provider_http_status", 429.5),
        ("observed_count", -1),
        ("configured_limit", "4"),
    ),
)
def test_stop_detail_rejects_invalid_structured_values(
    field_name: str, invalid_value: object
) -> None:
    detail_type = _type(sourcing_models, "SourcingStopDetail")
    stage_type = _type(sourcing_models, "SourcingStopStage")

    with pytest.raises(DomainValidationError, match=f"stop_detail.{field_name}"):
        detail_type(stage=stage_type.PROVIDER, **{field_name: invalid_value})


def test_public_plan_factory_binds_trusted_tenant_to_entity() -> None:
    plan_type = _type(sourcing_models, "PublicSourcingPlan")

    plan = plan_type.create(
        TenantId("tenant-a"),
        _plan_command(),
        created_at=NOW,
    )

    assert plan.tenant_id == TenantId("tenant-a")


def test_public_plan_factory_rejects_blank_trusted_tenant() -> None:
    plan_type = _type(sourcing_models, "PublicSourcingPlan")

    with pytest.raises(DomainValidationError, match="tenant_id"):
        plan_type.create(TenantId("   "), _plan_command(), created_at=NOW)


def test_public_plan_confirmation_is_bound_to_exact_hash() -> None:
    plan_type = _type(sourcing_models, "PublicSourcingPlan")
    status_type = _type(sourcing_models, "PublicPlanStatus")
    stale_error = _type(__import__("domains.sourcing.errors", fromlist=["SourcingPlanStaleError"]), "SourcingPlanStaleError")

    plan = plan_type.create(TenantId("tenant-a"), _plan_command(), created_at=NOW)
    confirmed = plan.confirm(EmployeeId("boss-a"), confirmed_at=NOW)

    assert confirmed.status is status_type.AUTHORIZED
    assert confirmed.authorized_plan_hash == confirmed.plan_hash
    with pytest.raises(stale_error):
        confirmed.replace_scope(max_pages_read=confirmed.max_pages_read + 1)


def test_unconfirmed_plan_scope_change_creates_new_version_and_hash() -> None:
    plan_type = _type(sourcing_models, "PublicSourcingPlan")
    status_type = _type(sourcing_models, "PublicPlanStatus")
    plan = plan_type.create(TenantId("tenant-a"), _plan_command(), created_at=NOW)

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
    ("field_name", "bad_value"),
    [("source_id", ""), ("source_id", " source "), ("extracted_by", "   ")],
)
def test_need_fact_rejects_unusable_provenance_labels(
    field_name: str, bad_value: str
) -> None:
    fact_type = _type(sourcing_schemas, "NeedFact")
    provenance = PROVENANCE.model_copy(update={field_name: bad_value})

    with pytest.raises(PydanticValidationError, match=field_name):
        fact_type(value="hinge", provenance=provenance)


@pytest.mark.parametrize(
    "wrapper_name", ["SourcingObservedFact", "SourcingSupplierClaim"]
)
def test_candidate_fact_wrappers_reject_unusable_evidence_refs(
    wrapper_name: str,
) -> None:
    wrapper_type = _type(sourcing_schemas, wrapper_name)

    with pytest.raises(PydanticValidationError, match="evidence_ref"):
        wrapper_type(
            value="304 stainless steel",
            provenance=PROVENANCE,
            evidence_ref=ArtifactId("   "),
        )


def test_match_inference_rejects_unusable_evidence_refs() -> None:
    inference_type = _type(sourcing_schemas, "SourcingMatchInference")

    with pytest.raises(PydanticValidationError, match="based_on"):
        inference_type(
            value="材质与型号可匹配",
            based_on=(ArtifactId("   "),),
            inferred_by="human",
            inferred_at=NOW,
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


def test_cost_price_option_wire_decimal_preserves_long_string_and_rejects_json_number() -> None:
    option_type = _type(sourcing_schemas, "SourcingCostPriceOption")
    exact = "12345678901234567890.123456789012345678901234567890"
    string_payload = (
        '{"minimum_quantity":1,"unit_amount":"'
        + exact
        + '","currency":"USD","unit":"piece",'
        '"evidence_ref":"art-a","source_kind":"existing_product"}'
    )
    numeric_payload = string_payload.replace(f'"{exact}"', exact)

    option = option_type.model_validate_json(string_payload)

    assert option.unit_amount == Decimal(exact)
    with pytest.raises(PydanticValidationError, match="十进制字符串"):
        option_type.model_validate_json(numeric_payload)


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
    valid = _candidate_submission_payload()
    submission = submission_type.model_validate(valid)
    assert 1000 in submission.indicative_price_tiers
    with pytest.raises(PydanticValidationError):
        submission_type.model_validate(
            {**valid, "quoted_prices": valid["indicative_price_tiers"]}
        )


def test_candidate_submission_rejects_caller_supplied_verified_by() -> None:
    submission_type = _type(sourcing_schemas, "CandidateSubmission")

    with pytest.raises(PydanticValidationError):
        submission_type.model_validate(
            {**_candidate_submission_payload(), "verified_by": "employee-forged"}
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
