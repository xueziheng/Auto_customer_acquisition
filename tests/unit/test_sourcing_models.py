from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from domains.sourcing.schemas import (
    IndicativePriceTier,
    SourcingMatchInference,
    SourcingObservedFact,
)
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    SourcingCaseId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

_models = importlib.import_module("domains.sourcing.models")
EvidenceSnapshot = _models.EvidenceSnapshot
MatchExplanation = _models.MatchExplanation
MatchLadderRung = _models.MatchLadderRung
SourcingCase = _models.SourcingCase
SpecComparison = _models.SpecComparison
SpecMatchLevel = _models.SpecMatchLevel
SupplierCandidate = _models.SupplierCandidate

NOW = datetime(2026, 8, 21, 10, tzinfo=UTC)
PROVENANCE = ProvenanceSummary(
    source_type=SourceType.WEB_PAGE,
    source_id="page-hinge-a",
    extracted_by="human",
    extracted_at=NOW,
    confirmed_by=EmployeeId("employee-one"),
    confirmed_at=NOW,
)


def specs(*, unknown: str | None = None) -> list[SpecComparison]:
    return [
        SpecComparison(
            name,
            f"required-{name}",
            None if name == unknown else f"offered-{name}",
            SpecMatchLevel.UNKNOWN if name == unknown else SpecMatchLevel.EXACT,
        )
        for name in ("product_type", "material", "size", "model")
    ]


def candidate(
    suffix: str,
    *,
    created_at: datetime = NOW,
    verified_specs: list[SpecComparison] | None = None,
    currency: str = "USD",
    rejected: bool = False,
) -> SupplierCandidate:
    artifact = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X")
    evidence = EvidenceSnapshot(
        "https://supplier.example/catalog/hinge",
        NOW,
        "a" * 64,
        str(artifact),
    )
    observed_facts = {
        name: SourcingObservedFact(
            value=f"offered-{name}",
            provenance=PROVENANCE,
            evidence_ref=artifact,
        )
        for name in ("product_type", "material", "size", "model")
    }
    observed_facts.update(
        {
            "moq": SourcingObservedFact(
                value=500, provenance=PROVENANCE, evidence_ref=artifact
            ),
            "price_unit": SourcingObservedFact(
                value="piece", provenance=PROVENANCE, evidence_ref=artifact
            ),
            "currency": SourcingObservedFact(
                value="USD", provenance=PROVENANCE, evidence_ref=artifact
            ),
        }
    )
    return SupplierCandidate(
        candidate_id=SupplierCandidateId(f"cand-{suffix}"),
        tenant_id=TenantId("tenant-one"),
        case_id=SourcingCaseId("case-one"),
        supplier_name=f"Supplier {suffix}",
        product_title="Stainless steel hinge",
        created_at=created_at,
        observed_facts=observed_facts,
        match_inferences={
            "fit": SourcingMatchInference(
                value="四项规格已核对",
                based_on=(artifact,),
                inferred_by="human",
                inferred_at=NOW,
            )
        },
        verified_specs=specs() if verified_specs is None else verified_specs,
        indicative_price_tiers=(
            IndicativePriceTier(
                minimum_quantity=1000,
                amount=Decimal("1.25"),
                currency="USD",
                unit="piece",
                provenance=PROVENANCE,
                evidence_ref=artifact,
            ),
        ),
        moq=500,
        price_unit="piece",
        currency=currency,
        evidence=evidence,
        evidence_snapshots=(evidence,),
        match=MatchExplanation(MatchLadderRung.PUBLIC_SOURCING, specs(), "四项规格已核对"),
        rejected=rejected,
        verified_by=EmployeeId("employee-one"),
    )


def test_match_explanation_reports_unknowns_and_customer_confirmation() -> None:
    explanation = MatchExplanation(
        MatchLadderRung.CATALOG_MODIFIABLE,
        [
            SpecComparison("material", "304", None, SpecMatchLevel.UNKNOWN),
            SpecComparison(
                "finish",
                "polished",
                "brushed",
                SpecMatchLevel.DIFFERENT,
                substitutable=True,
                substitution_impact="外观不同",
                needs_customer_confirmation=True,
            ),
        ],
        "材质待确认，表面处理需要客户决定",
    )

    assert explanation.has_unknowns is True
    assert explanation.requires_customer_confirmation is True


def test_candidate_verification_collects_every_missing_item_without_short_circuit() -> None:
    item = candidate("missing", verified_specs=[])
    item.indicative_price_tiers = ()
    item.moq = None
    item.price_unit = None
    item.currency = None
    item.evidence = None

    passed, missing = item.passes_verification()

    assert passed is False
    assert missing == [
        "product_type",
        "material",
        "size",
        "quantity_tier",
        "moq",
        "price_unit",
        "currency",
        "evidence_snapshot",
    ]


def test_candidate_requires_resolved_specs_and_matching_price_currency() -> None:
    unknown = candidate("unknown", verified_specs=specs(unknown="material"))
    mismatch = candidate("mismatch", currency="EUR")

    assert unknown.passes_verification() == (False, ["material"])
    assert mismatch.passes_verification() == (False, ["currency"])
    assert candidate("valid").passes_verification() == (True, [])


def test_candidate_without_model_requirement_can_pass_verification() -> None:
    """V2 Need 未声明型号时，不得把不存在的规格当作候选缺口。"""

    item = candidate("no-model-requirement")
    item.verified_specs = [
        comparison for comparison in item.verified_specs if comparison.spec_name != "model"
    ]
    item.observed_facts.pop("model")
    item.match = MatchExplanation(
        MatchLadderRung.PUBLIC_SOURCING,
        item.verified_specs,
        "三项已声明规格已核对",
    )

    assert item.passes_verification() == (True, [])


def test_candidate_verification_rejects_normalized_duplicate_specs_without_overwrite() -> None:
    duplicate_specs = [
        SpecComparison(
            " MATERIAL ",
            "required-material",
            "unverified-substitute",
            SpecMatchLevel.DIFFERENT,
            substitutable=False,
        ),
        *specs(),
    ]
    item = candidate("duplicate-spec", verified_specs=duplicate_specs)

    passed, missing = item.passes_verification()

    assert passed is False
    assert "duplicate_spec:material" in missing
    assert "incompatible_spec:material" in missing
    assert "structured_spec:material" in missing


def test_candidate_verification_rejects_legacy_only_unstructured_evidence() -> None:
    item = candidate("legacy-only")
    item.observed_facts = {}
    item.supplier_claims = {}
    item.match_inferences = {}

    assert item.passes_verification() == (
        False,
        [
            "structured_spec:product_type",
            "structured_spec:material",
            "structured_spec:size",
            "structured_spec:model",
            "match_inference",
            "structured_moq",
            "structured_price_unit",
            "structured_currency",
        ],
    )


def test_case_returns_only_first_three_deterministically_qualified_candidates() -> None:
    items = [
        candidate("four", created_at=NOW + timedelta(minutes=4)),
        candidate("two", created_at=NOW + timedelta(minutes=2)),
        candidate("rejected", created_at=NOW, rejected=True),
        candidate("one", created_at=NOW + timedelta(minutes=1)),
        candidate("three", created_at=NOW + timedelta(minutes=3)),
    ]
    case = SourcingCase(
        SourcingCaseId("case-one"),
        TenantId("tenant-one"),
        ValidatedNeedId("need-one"),
        NOW,
        candidates=items,
    )

    assert [str(item.candidate_id) for item in case.qualified_candidates()] == [
        "cand-one",
        "cand-two",
        "cand-three",
    ]
