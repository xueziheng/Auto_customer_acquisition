"""真实回复格式故障回归：提示示例与现有消费者一致，不猜金额或放宽原文证据。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from agent_runtime.qualification_agent.agent import QualificationAgent
from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort
from apps.scheduler_worker.reply_actions import (
    ReplyFieldSnapshot,
    _candidate_business_value,
)
from domains.demand.service_impl import _coerce_field_value
from shared.errors import ValidationError
from shared.schemas.money import Money


class CapturedJsonClient:
    """仅替换外部模型响应；分类器、模型端口和字段消费者均为真实实现。"""

    def __init__(self, response: dict) -> None:
        self.response = json.dumps(response)
        self.prompt = ""

    async def complete_json(self, *, model, system_prompt, payload, max_output_tokens):
        self.prompt = system_prompt
        return self.response


def classifier(client: CapturedJsonClient) -> QualificationAgent:
    return QualificationAgent(
        "controlled-prompt-contract",
        StructuredReplyModelPort(client, "controlled-prompt-contract"),
        None,
        None,
    )


async def test_prompt_example_survives_existing_need_value_consumers() -> None:
    """遗漏格式示例或给模型一个消费者拒绝的示例，必须失败；不模拟模型遵循能力。"""
    captured = CapturedJsonClient({"category": "auto_reply", "candidate_fields": []})
    await classifier(captured).classify(
        message={
            "message_id": "msg_prompt_contract",
            "subject": "Reply",
            "body": "Thanks.",
        }
    )
    examples = []
    for line in captured.prompt.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            example = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(example, dict):
            examples.append(example)
    inputs = [item for item in examples if set(item) == {"subject", "body"}]
    outputs = [
        item for item in examples if item.get("category") == "provides_specification"
    ]
    assert len(inputs) == len(outputs) == 1, (
        "生产提示缺少能被实际字段消费者验证的完整输入/输出格式示例"
    )
    result = await classifier(CapturedJsonClient(outputs[0])).classify(
        message={"message_id": "msg_example_contract", **inputs[0]}
    )
    assert not result.rejected_candidates
    candidates = {field.field: field for field in result.candidate_fields}
    assert set(candidates) == {
        "product_category",
        "application",
        "quantity",
        "target_price",
    }
    assert candidates["product_category"].value == "valves"
    assert candidates["application"].value == "irrigation"
    for field in result.candidate_fields:
        assert field.quote in inputs[0]["body"]
        value = _candidate_business_value(
            ReplyFieldSnapshot(field.field, field.value, field.quote)
        )
        converted = _coerce_field_value(field.field, value)
        if field.field == "quantity":
            assert field.value == "120"
            assert type(converted) is int and converted == 120
        elif field.field == "target_price":
            assert isinstance(converted, Money)
            assert converted.amount == Decimal("4.25")
            assert converted.currency == "EUR"


@pytest.mark.parametrize(
    ("field", "value", "quote"),
    [
        ("quantity", "5000 units", "5000 units"),
        ("target_price", "USD 2 per unit", "USD 2 per unit"),
    ],
)
async def test_cloud_capture_does_not_gain_guessing_or_silent_field_drop(
    field: str, value: str, quote: str
) -> None:
    """smoke03 实际值仍被原消费者拒绝；仅补提示不能宣称已改变旧输出的业务结果。"""
    result = await classifier(
        CapturedJsonClient(
            {
                "category": "provides_specification",
                "candidate_fields": [{"field": field, "value": value, "quote": quote}],
            }
        )
    ).classify(
        message={
            "message_id": "msg_smoke03_format",
            "subject": "Reply",
            "body": "We need hinges for cabinet doors. We need 5000 units at USD 2 per unit.",
        }
    )
    assert len(result.candidate_fields) == 1
    candidate = result.candidate_fields[0]
    assert candidate.value == value and candidate.quote == quote
    with pytest.raises(ValidationError):
        _coerce_field_value(
            field,
            _candidate_business_value(
                ReplyFieldSnapshot(field, candidate.value, candidate.quote)
            ),
        )
