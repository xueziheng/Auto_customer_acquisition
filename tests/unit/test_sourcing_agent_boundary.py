"""SourcingAgent 的逐项匹配、参考价边界与人工拒绝建议合同。"""

from __future__ import annotations

import json
import sys
import unicodedata
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
    "summary",
    [
        "Observed price is CHF 2.50.",
        "Observed price is usd 2.50.",
        "Model X. AED 250 is listed.",
    ],
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
        "Grade 304 costs AED250.",
        "Model AED 250 extra.",
        "The price-sensitive model is AED-250.",
        "Series CHF 250 matches the requested type.",
        "Cost impact 304。Price 250.",
        "cost implication 304；pricing 250.",
        "Pricing guidance applies to model 304.",
        "The item was priced for a 304-piece batch.",
        "Costs changed for grade 304 steel.",
        "Price details are pending for model 304.",
        "cost impact 304\u2028pricing 250",
        "Observed €；250.",
        "Currency symbol € unavailable.",
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_rejects_non_strict_iso_identity_and_sentence_mix(
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


_UNICODE_CURRENCY_SYMBOLS = tuple(
    chr(codepoint)
    for codepoint in range(sys.maxunicode + 1)
    if unicodedata.category(chr(codepoint)) == "Sc"
)


@pytest.mark.parametrize("symbol", _UNICODE_CURRENCY_SYMBOLS)
@pytest.mark.asyncio
async def test_existing_agent_rejects_every_unicode_currency_symbol_with_number(
    symbol: str,
) -> None:
    assert len(_UNICODE_CURRENCY_SYMBOLS) == 63
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    response = _page_review_response()
    response["summary"] = f"Observed {symbol} 250 today."

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
        "The cost is approximately 2.50.",
        "The price starts at 2.50.",
        "cost is about 2.50",
        "Unit price 2.50 per piece.",
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_rejects_tightly_connected_qualified_money_forms(
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
    ("keyword", "connector", "qualifier", "separator"),
    [
        ("cost", "is", "approx.", ""),
        ("price", "was", "roughly", " "),
        ("unit price", "at", "about", " "),
        ("price", "from", "around", " "),
        ("cost", "starts at", "approximately", " "),
        ("price", "starts from", "roughly", " "),
        ("cost", "begins at", "about", " "),
        ("unit-price", "begins from", "around", " "),
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_rejects_composed_money_predicate_variants(
    keyword: str, connector: str, qualifier: str, separator: str
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
    response["summary"] = (
        f"Observed {keyword} {connector} {qualifier}{separator}2.50 today."
    )

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析不得生成价格"


@pytest.mark.parametrize(
    "summary",
    [
        "The tolerance is 2.50 mm.",
        "The value changed to 3.75.",
        "The tolerance is ٢.٥٠ mm.",
        "2.50 was the cost.",
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_rejects_untrusted_decimals_and_reverse_price_predicate(
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
        "The cost impact for grade 304 steel is unknown.",
        "The price-sensitive model X250 uses grade 304 steel.",
        "Grade 304 steel has an unknown cost impact.",
        "The amount of 304 stainless steel is sufficient.",
        "The amount is about 304 pieces.",
        "cost impact for grade 304",
        "cost implication for grade 304",
        "Cost\u2028304 steel matches.",
        "Model AED 250",
        "Model AED-250 uses grade 304 steel.",
        "Series CHF-250",
        "Grade 304",
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


@pytest.mark.parametrize(
    ("trusted_literal", "summary"),
    [
        (
            "Tolerance 2.50 mm",
            "Tolerance 2.50 mm matches the required specification.",
        ),
        (
            "Tolerance 2.50 mm",
            "The first statement is complete. Tolerance 2.50 mm matches exactly.",
        ),
        ("2.50", "The first statement is complete. 2.50 matches exactly."),
    ],
)
@pytest.mark.asyncio
async def test_existing_agent_allows_decimal_only_inside_exact_trusted_spec_literal(
    trusted_literal: str, summary: str
) -> None:
    module = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    )
    review = module.SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    required_specs = review["required_specs"]
    offered_specs = review["offered_specs"]
    assert isinstance(required_specs, tuple) and isinstance(offered_specs, tuple)
    required_specs[1]["required"] = trusted_literal
    offered_specs[1]["offered"] = trusted_literal
    response = _page_review_response()
    comparisons = response["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[1]["offered"] = trusted_literal
    response["summary"] = summary

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert [change["operation"] for change in result.changes] == [
        "record_match_explanation"
    ]


@pytest.mark.parametrize("summary", ["Cost 2.50 mm", "Cost: 2.50", "USD: 2.50"])
@pytest.mark.asyncio
async def test_trusted_decimal_cannot_exempt_explicit_cost_sentence(
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
    required_specs = review["required_specs"]
    offered_specs = review["offered_specs"]
    assert isinstance(required_specs, tuple) and isinstance(offered_specs, tuple)
    required_specs[1]["required"] = "2.50"
    offered_specs[1]["offered"] = "2.50"
    response = _page_review_response()
    comparisons = response["comparisons"]
    assert isinstance(comparisons, list)
    comparisons[1]["offered"] = "2.50"
    response["summary"] = summary

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析不得生成价格"


@pytest.mark.parametrize(
    "summary",
    [
        "AED250",
        "AED—250",
        "250/AED",
        "The tolerance is .50 mm.",
        "The tolerance is １２．５０ mm.",
        "The tolerance is ٢٫٥٠ mm.",
        "cost will be 250",
    ],
)
@pytest.mark.asyncio
async def test_agent_uses_unicode_money_parser_for_reviewer_bypass_corpus(
    summary: str,
) -> None:
    review = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    ).SourcingAgent.build_page_candidate_review(
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


@pytest.mark.parametrize("field", ["summary", "substitution_impact", "explanation"])
@pytest.mark.asyncio
async def test_agent_applies_money_parser_to_every_model_prose_field(
    field: str,
) -> None:
    review = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    ).SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    response = _page_review_response()
    if field == "summary":
        response["summary"] = "AED250"
    elif field == "substitution_impact":
        comparisons = response["comparisons"]
        assert isinstance(comparisons, list)
        comparisons[0].update(
            {
                "level": "different",
                "substitutable": True,
                "substitution_impact": "AED250",
                "needs_customer_confirmation": True,
            }
        )
    else:
        review = __import__(
            "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
        ).SourcingAgent.build_page_candidate_review(
            draft=_page_draft_with_incomplete_tier("quantity_tier_missing"),
            case_id=SourcingCaseId("src_case"),
            candidate_id=SupplierCandidateId("sc_candidate"),
        )
        response["price_rejection_suggestions"] = [
            {"reason": "quantity_tier_missing", "explanation": "AED250"}
        ]

    result = await _agent(_ReviewPort(json.dumps(response))).run(
        _page_review_task(review), None
    )

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析不得生成价格"


@pytest.mark.asyncio
async def test_agent_fails_closed_on_raw_surrogate_without_leaking_exception() -> None:
    review = __import__(
        "agent_runtime.sourcing_agent.agent", fromlist=["SourcingAgent"]
    ).SourcingAgent.build_page_candidate_review(
        draft=_page_draft(),
        case_id=SourcingCaseId("src_case"),
        candidate_id=SupplierCandidateId("sc_candidate"),
    )
    response = _page_review_response()
    response["summary"] = "unsafe\ud800value"
    raw = json.dumps(response, ensure_ascii=False)

    result = await _agent(_ReviewPort(raw)).run(_page_review_task(review), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：寻源分析模型输出无效"
    assert result.summary.encode("utf-8")
