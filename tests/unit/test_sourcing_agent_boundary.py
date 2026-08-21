"""SourcingAgent 的逐项匹配、参考价边界与人工拒绝建议合同。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id


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


@pytest.mark.asyncio
async def test_review_preserves_unknowns_and_marks_prices_indicative() -> None:
    result = await _agent(_ReviewPort(json.dumps(_safe_response()))).run(
        _task(), None
    )

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
    response["price_rejection_suggestions"] = response[
        "price_rejection_suggestions"
    ][1:]

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
