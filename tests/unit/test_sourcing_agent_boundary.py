"""SourcingAgent 的逐项匹配、参考价边界与人工拒绝建议合同。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.sourcing_agent.extraction import (
    SourcingObservedLiteral,
    SourcingObservedPriceTier,
    SourcingObservedSpec,
    SourcingPageCandidateDraft,
    SourcingPageEvidence,
)
from shared.schemas.identifiers import (
    ArtifactId,
    RunId,
    SourcingCaseId,
    SupplierCandidateId,
    TenantId,
    UserId,
    new_id,
)


class _ReviewPort:
    def __init__(self, response: object) -> None:
        self._response = response

    async def review_candidate(
        self, *, system_prompt: str, candidate: dict[str, object]
    ) -> str:
        del system_prompt, candidate
        return self._response  # type: ignore[return-value]


class _MustNotRunPort:
    async def review_candidate(
        self, *, system_prompt: str, candidate: dict[str, object]
    ) -> str:
        del system_prompt, candidate
        raise AssertionError("含凭证输入不得进入模型")


def _task(*, supplier_name: str = "Example Hardware Factory") -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId(new_id("usr")),
        objective="review_manual_supplier_candidate",
        inputs={
            "candidate_review": {
                "case_id": "sourcing-case-one",
                "candidate_id": "supplier-candidate-one",
                "supplier_name": supplier_name,
                "product_title": "Stainless outdoor hinge",
                "rung": 6,
                "required_specs": (
                    {"spec_name": "product_type", "required": "outdoor hinge"},
                    {"spec_name": "material", "required": "stainless steel 304"},
                ),
                "offered_specs": (
                    {"spec_name": "product_type", "offered": "outdoor hinge"},
                    {"spec_name": "material", "offered": None},
                ),
                "price_checks": {
                    "far_below_market_without_tier": True,
                    "has_vague_range": False,
                    "has_quantity_tier": False,
                    "unit_clear": True,
                    "currency_clear": True,
                },
                "evidence": {
                    "source_url": "https://supplier.example/item",
                    "content_hash": "a" * 64,
                    "snapshot_artifact_ref": new_id("art"),
                    "observed_at": "2026-08-21T12:00:00+00:00",
                },
            }
        },
    )


def _agent(port: Any) -> Any:
    module = __import__(
        "agent_runtime.sourcing_agent.agent",
        fromlist=["SourcingAgent"],
    )
    return module.SourcingAgent(
        model="test-model-v1",
        model_client=port,
        gateway=None,
        guardrails=CredentialMarkerGuard(),
    )


def _safe_response() -> dict[str, object]:
    return {
        "comparisons": [
            {
                "spec_name": "product_type",
                "offered": "outdoor hinge",
                "level": "exact",
                "substitutable": None,
                "substitution_impact": None,
                "needs_customer_confirmation": False,
            },
            {
                "spec_name": "material",
                "offered": None,
                "level": "unknown",
                "substitutable": None,
                "substitution_impact": None,
                "needs_customer_confirmation": True,
            },
        ],
        "summary": (
            "The product type matches, while the material remains unknown and "
            "requires customer or supplier confirmation."
        ),
        "price_rejection_suggestions": [
            {
                "reason": "bait_price",
                "explanation": "The unusually low observation has no matching quantity tier.",
            },
            {
                "reason": "quantity_tier_missing",
                "explanation": "The observation does not state the quantity tier.",
            },
        ],
    }


def _page_draft() -> SourcingPageCandidateDraft:
    artifact = ArtifactId("art_01H00000000000000000000000")

    def observed(literal: str, quote: str) -> SourcingObservedLiteral:
        return SourcingObservedLiteral(
            literal=literal,
            source_quote=quote,
            snapshot_artifact_ref=artifact,
        )

    price_quote = "Price USD 2.50 per piece for minimum quantity 100."
    return SourcingPageCandidateDraft(
        evidence=SourcingPageEvidence(
            source_url="https://supplier.example/item",
            observed_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
            content_hash="a" * 64,
            snapshot_artifact_ref=artifact,
        ),
        supplier_name=observed(
            "Example Hardware Factory", "Supplier: Example Hardware Factory."
        ),
        product_title=observed(
            "Stainless outdoor hinge", "Product: Stainless outdoor hinge."
        ),
        specs=(
            SourcingObservedSpec(
                spec_name="product_type",
                required="outdoor hinge",
                observed=observed(
                    "Stainless outdoor hinge", "Product: Stainless outdoor hinge."
                ),
            ),
            SourcingObservedSpec(
                spec_name="material",
                required="304 stainless steel",
                observed=observed(
                    "304 stainless steel", "Material 304 stainless steel."
                ),
            ),
        ),
        moq=100,
        moq_literal=observed("100", "MOQ 100 pieces."),
        price_tiers=(
            SourcingObservedPriceTier(
                minimum_quantity=100,
                amount=Decimal("2.50"),
                unit="piece",
                currency="USD",
                quantity_literal=observed("100", price_quote),
                price_literal=observed("USD 2.50", price_quote),
                unit_literal=observed("piece", price_quote),
                currency_literal=observed("USD", price_quote),
                rejection_reasons=(),
            ),
        ),
        rejection_reasons=(),
    )


def _page_review_task(review: dict[str, object]) -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId(new_id("usr")),
        objective="review_public_supplier_candidate",
        inputs={"candidate_review": review},
    )


def _page_draft_with_incomplete_tier(
    reason: str,
) -> SourcingPageCandidateDraft:
    draft = _page_draft()
    artifact = draft.evidence.snapshot_artifact_ref
    quote = "Observed tier USD 3.00 per piece for minimum quantity 200."

    def observed(literal: str) -> SourcingObservedLiteral:
        return SourcingObservedLiteral(
            literal=literal,
            source_quote=quote,
            snapshot_artifact_ref=artifact,
        )

    quantity = 200
    unit = "piece"
    currency = "USD"
    quantity_literal = observed("200")
    unit_literal = observed("piece")
    currency_literal = observed("USD")
    if reason == "quantity_tier_missing":
        quantity = None  # type: ignore[assignment]
        quantity_literal = None  # type: ignore[assignment]
    elif reason == "unit_unclear":
        unit = None  # type: ignore[assignment]
        unit_literal = None  # type: ignore[assignment]
    elif reason == "currency_unclear":
        currency = None  # type: ignore[assignment]
        currency_literal = None  # type: ignore[assignment]
    else:
        raise AssertionError("unsupported controlled reason")
    incomplete = SourcingObservedPriceTier(
        minimum_quantity=quantity,
        amount=None,
        unit=unit,
        currency=currency,
        quantity_literal=quantity_literal,
        price_literal=observed("USD 3.00"),
        unit_literal=unit_literal,
        currency_literal=currency_literal,
        rejection_reasons=(reason,),
    )
    return SourcingPageCandidateDraft(
        evidence=draft.evidence,
        supplier_name=draft.supplier_name,
        product_title=draft.product_title,
        specs=draft.specs,
        moq=draft.moq,
        moq_literal=draft.moq_literal,
        price_tiers=(*draft.price_tiers, incomplete),
        rejection_reasons=(reason,),
    )


def _page_review_response(
    *, rewrite: bool = False, money: bool = False
) -> dict[str, object]:
    return {
        "comparisons": [
            {
                "spec_name": "product_type",
                "offered": "Stainless outdoor hinge",
                "level": "different",
                "substitutable": True,
                "substitution_impact": "Confirm the exact hinge subtype.",
                "needs_customer_confirmation": True,
            },
            {
                "spec_name": "material",
                "offered": "agent rewrite" if rewrite else "304 stainless steel",
                "level": "exact",
                "substitutable": None,
                "substitution_impact": None,
                "needs_customer_confirmation": False,
            },
        ],
        "summary": "Observed price is USD 2.50."
        if money
        else "One subtype needs confirmation.",
        "price_rejection_suggestions": [],
    }


@pytest.mark.asyncio
async def test_review_preserves_unknowns_and_marks_prices_indicative() -> None:
    result = await _agent(_ReviewPort(json.dumps(_safe_response()))).run(_task(), None)

    assert result.guardrail_violations == []
    assert [change["operation"] for change in result.changes] == [
        "record_match_explanation",
        "suggest_candidate_rejection",
    ]
    match = result.changes[0]
    comparisons = match["payload"]["comparisons"]
    assert comparisons[1] == {
        "spec_name": "material",
        "required": "stainless steel 304",
        "offered": None,
        "level": "unknown",
        "substitutable": None,
        "substitution_impact": None,
        "needs_customer_confirmation": True,
    }
    assert match["payload"]["price_basis"] == "indicative"
    rejection = result.changes[1]
    assert rejection["payload"]["reasons"] == (
        "bait_price",
        "quantity_tier_missing",
    )
    assert rejection["payload"]["is_pending_human_decision"] is True
    assert "amount" not in json.dumps(result.changes)
    assert "quoted" not in json.dumps(result.changes)


@pytest.mark.asyncio
async def test_missing_offered_spec_cannot_be_promoted_to_exact() -> None:
    response = _safe_response()
    response["comparisons"][1]["level"] = "exact"  # type: ignore[index]

    result = await _agent(_ReviewPort(json.dumps(response))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：未知供应规格不得标记为完全匹配"


@pytest.mark.asyncio
async def test_price_rejection_reasons_must_match_deterministic_checks() -> None:
    response = _safe_response()
    response["price_rejection_suggestions"] = response["price_rejection_suggestions"][
        1:
    ]

    result = await _agent(_ReviewPort(json.dumps(response))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：价格拒绝建议与确定性检查不一致"


@pytest.mark.asyncio
async def test_model_cannot_emit_similarity_score_or_price_fields() -> None:
    response = _safe_response()
    response["similarity_score"] = 0.87

    result = await _agent(_ReviewPort(json.dumps(response))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析模型输出含未授权字段"


@pytest.mark.asyncio
async def test_sourcing_credentials_are_rejected_before_model_call() -> None:
    result = await _agent(_MustNotRunPort()).run(
        _task(supplier_name="Forwarded bearer token secret-value"),
        None,
    )

    assert result.changes == []
    assert result.summary == "寻源分析输入被安全边界拒绝"


@pytest.mark.asyncio
async def test_page_draft_handoff_preserves_observations_and_immutable_evidence() -> (
    None
):
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )

    assert review["offered_specs"] == (
        {"spec_name": "product_type", "offered": "Stainless outdoor hinge"},
        {"spec_name": "material", "offered": "304 stainless steel"},
    )
    assert review["price_checks"] == {
        "far_below_market_without_tier": False,
        "has_vague_range": False,
        "has_quantity_tier": True,
        "unit_clear": True,
        "currency_clear": True,
    }
    assert review["evidence"] == {
        "source_url": "https://supplier.example/item",
        "content_hash": "a" * 64,
        "snapshot_artifact_ref": "art_01H00000000000000000000000",
        "observed_at": "2026-08-30T12:00:00+00:00",
    }
    result = await _agent(_ReviewPort(json.dumps(_page_review_response()))).run(
        _page_review_task(review), None
    )
    assert [change["operation"] for change in result.changes] == [
        "record_match_explanation"
    ]
    assert result.changes[0]["payload"]["price_basis"] == "indicative"


@pytest.mark.parametrize(
    ("reason", "expected_check"),
    [
        ("quantity_tier_missing", "has_quantity_tier"),
        ("unit_unclear", "unit_clear"),
        ("currency_unclear", "currency_clear"),
    ],
)
def test_page_review_requires_every_price_tier_to_be_complete(
    reason: str, expected_check: str
) -> None:
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )

    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft_with_incomplete_tier(reason),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )

    checks = review["price_checks"]
    assert isinstance(checks, dict)
    assert checks[expected_check] is False
    assert reason in module.SourcingAgent._deterministic_price_reasons(review)


@pytest.mark.asyncio
@pytest.mark.parametrize(("rewrite", "money"), [(True, False), (False, True)])
async def test_existing_agent_cannot_rewrite_page_facts_or_generate_money(
    rewrite: bool, money: bool
) -> None:
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )

    result = await _agent(
        _ReviewPort(json.dumps(_page_review_response(rewrite=rewrite, money=money)))
    ).run(_page_review_task(review), None)

    assert result.changes == []
    if rewrite:
        assert result.summary == "模型输出被护栏拦截：模型改写了供应规格事实"
    else:
        assert result.summary == "模型输出被护栏拦截：寻源分析不得生成价格"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summary", ["Observed price is CHF 2.50.", "Observed price is usd 2.50."]
)
async def test_existing_agent_rejects_money_in_any_three_letter_currency(
    summary: str,
) -> None:
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    response = _page_review_response()
    response["summary"] = summary

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析不得生成价格"


@pytest.mark.parametrize(
    "summary",
    [
        "Observed cost is chf 2.50.",
        "Unit price 2.50 per piece.",
        "The cost is 2.50.",
        "The amount is 2.50.",
        "Available for 2.50 per unit.",
        "Supplier asks €2.50.",
        "Wholesale value 2.50 AED.",
        "BWP 2.50 is listed.",
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_rejects_currency_keyword_and_per_unit_money_forms(
    summary: str,
) -> None:
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    response = _page_review_response()
    response["summary"] = summary

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析不得生成价格"


@pytest.mark.parametrize(
    "summary",
    [
        "The plate measures 304 mm by 4 mm.",
        "The set contains 2 pieces and weighs 500 g.",
        "Model AED-250 uses grade 304 steel.",
        "The cost impact for grade 304 steel is unknown.",
        "The price-sensitive model is AED-250.",
        "Grade 304 steel has an unknown cost impact.",
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_money_guard_does_not_block_normal_specs(
    summary: str,
) -> None:
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    response = _page_review_response()
    response["summary"] = summary

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert [change["operation"] for change in result.changes] == [
        "record_match_explanation"
    ]
