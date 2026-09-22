"""受控意图/来源拒绝集；不代表真实模型的准确率。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as SchemaError

from agent_runtime.assistant.decision import parse_decision, validate_decision
from agent_runtime.assistant.proposal import ResearchProposalBuilder
from domains.assistant.schemas import Clarification, ResearchDraft
from shared.errors import TradeOSError
from tests.unit.test_assistant_decision import context, turn

CASES = [
    json.loads(line)
    for line in (Path(__file__).parent / "assistant/clarification.jsonl")
    .read_text()
    .splitlines()
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
async def test_untrusted_intent_never_bypasses_trade_boundaries(case):
    ctx = context(
        turn("one").model_copy(update={"input_text": case["text"]}), role=case["role"]
    )
    try:
        decision = validate_decision(parse_decision(json.dumps(case["decision"])), ctx)
        if isinstance(decision, ResearchDraft):
            result = await ResearchProposalBuilder().build(ctx, decision)
            observed = "clarify" if isinstance(result, Clarification) else "proposal"
        else:
            observed = "accepted"
    except (TradeOSError, SchemaError):
        observed = "reject"
    assert observed == case["expected"]
