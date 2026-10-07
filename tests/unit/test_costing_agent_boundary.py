"""CostingAgent 只解释确定性结果，不产生可参与计算的金额。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_runtime.base import AgentTask
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id


class _CostReviewPort:
    def __init__(self, response: object) -> None:
        self._response = response

    async def review_costing(
        self, *, system_prompt: str, review: dict[str, object]
    ) -> str:
        del system_prompt, review
        return self._response  # type: ignore[return-value]


class _MustNotRunPort:
    async def review_costing(
        self, *, system_prompt: str, review: dict[str, object]
    ) -> str:
        del system_prompt, review
        raise AssertionError("含凭证输入不得进入模型")


def _task(*, opportunity_context: str = "Outdoor furniture sourcing case") -> AgentTask:
    return AgentTask(
        tenant_id=TenantId(new_id("tn")),
        run_id=RunId(new_id("run")),
        acting_user=UserId(new_id("usr")),
        objective="review_cost_sheet",
        inputs={
            "cost_review": {
                "cost_sheet_id": "cost-sheet-one",
                "opportunity_id": new_id("opp"),
                "present_item_types": (
                    "product_purchase",
                    "international_freight",
                ),
                "expected_item_types": (
                    "product_purchase",
                    "international_freight",
                    "insurance",
                ),
                "margin_status": "below_target",
                "has_indicative_items": True,
                "target_language": "en-US",
                "opportunity_context": opportunity_context,
            }
        },
    )


def _agent(port: Any) -> Any:
    module = __import__(
        "agent_runtime.costing_agent.agent",
        fromlist=["CostingAgent"],
    )
    return module.CostingAgent(
        model="test-model-v1",
        model_client=port,
        gateway=None,
        guardrails=CredentialMarkerGuard(),
    )


def _safe_response() -> str:
    return json.dumps(
        {
            "missing_item_suggestions": [
                {
                    "item_type": "insurance",
                    "reason": "Insurance has not yet been confirmed for this shipment.",
                }
            ],
            "risk_note": (
                "The sheet still uses indicative inputs and remains below the target "
                "margin category."
            ),
            "customer_explanation": (
                "We are reviewing the complete logistics and risk structure before "
                "preparing any customer-facing quotation."
            ),
            "content_language": "en",
        }
    )


@pytest.mark.asyncio
async def test_cost_review_outputs_pending_text_only_suggestions() -> None:
    result = await _agent(_CostReviewPort(_safe_response())).run(_task(), None)

    assert result.guardrail_violations == []
    assert [item["operation"] for item in result.changes] == [
        "suggest_cost_item",
        "risk_note",
        "customer_explanation",
    ]
    suggestion = result.changes[0]
    assert suggestion["payload"] == {
        "cost_sheet_id": "cost-sheet-one",
        "item_type": "insurance",
        "reason": "Insurance has not yet been confirmed for this shipment.",
        "entered_by": None,
        "is_pending_confirmation": True,
        "generated_by": "test-model-v1",
    }
    assert "amount" not in json.dumps(result.changes)
    assert "currency" not in json.dumps(result.changes)


@pytest.mark.asyncio
async def test_model_must_cover_only_deterministically_missing_item_types() -> None:
    payload = json.loads(_safe_response())
    payload["missing_item_suggestions"][0]["item_type"] = "packaging"

    result = await _agent(_CostReviewPort(json.dumps(payload))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：成本遗漏建议与确定性检查不一致"


@pytest.mark.asyncio
async def test_model_cannot_add_amounts_or_currency_to_cost_review() -> None:
    payload = json.loads(_safe_response())
    payload["missing_item_suggestions"][0]["amount"] = "12.50"

    result = await _agent(_CostReviewPort(json.dumps(payload))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：成本解释模型输出含未授权字段"


@pytest.mark.asyncio
async def test_model_generated_currency_number_is_rejected_even_inside_text() -> None:
    payload = json.loads(_safe_response())
    payload["risk_note"] = "The missing insurance may cost USD 12.50."

    result = await _agent(_CostReviewPort(json.dumps(payload))).run(_task(), None)

    assert result.changes == []
    assert result.summary == "模型输出被护栏拦截：成本解释不得生成金额"


@pytest.mark.asyncio
async def test_cost_review_credentials_are_rejected_before_model_call() -> None:
    result = await _agent(_MustNotRunPort()).run(
        _task(opportunity_context="Forwarded note contains bearer secret-value"),
        None,
    )

    assert result.changes == []
    assert result.summary == "成本解释输入被安全边界拒绝"
