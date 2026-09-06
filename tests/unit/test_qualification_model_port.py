"""回复分类生产模型端口的输入收窄与输出上限。"""

from __future__ import annotations

import pytest

from shared.errors import ValidationError


class _StructuredClient:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    async def complete_json(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.output


async def test_reply_model_port_forwards_only_guarded_content() -> None:
    from agent_runtime.qualification_agent.openai_port import (
        StructuredReplyModelPort,
    )

    client = _StructuredClient('{"category":"auto_reply"}')
    port = StructuredReplyModelPort(client, "gpt-5-mini", max_output_tokens=512)

    result = await port.classify_reply(
        system_prompt="Return strict JSON.",
        message={"subject": "Out of office", "body": "I am away until Friday."},
    )

    assert result == '{"category":"auto_reply"}'
    assert client.calls == [
        {
            "model": "gpt-5-mini",
            "system_prompt": "Return strict JSON.",
            "payload": {
                "subject": "Out of office",
                "body": "I am away until Friday.",
            },
            "max_output_tokens": 512,
        }
    ]


async def test_reply_model_port_fails_closed_on_extra_input_or_large_output() -> None:
    from agent_runtime.qualification_agent.openai_port import (
        StructuredReplyModelPort,
    )

    client = _StructuredClient("x" * 1_025)
    port = StructuredReplyModelPort(client, "gpt-5-mini", max_output_bytes=1_024)

    with pytest.raises(ValidationError, match="回复分类模型输入无效"):
        await port.classify_reply(
            system_prompt="Return strict JSON.",
            message={
                "subject": "Hello",
                "body": "Interested.",
                "message_id": "msg_must_not_reach_model",
            },
        )
    assert client.calls == []

    with pytest.raises(ValidationError, match="回复分类模型输出过大"):
        await port.classify_reply(
            system_prompt="Return strict JSON.",
            message={"subject": "Hello", "body": "Interested."},
        )


async def test_original_qualification_agent_prompt_reaches_structured_port():
    from agent_runtime.qualification_agent.agent import QualificationAgent
    from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort

    client = _StructuredClient('{"category":"auto_reply"}')
    agent = QualificationAgent(
        "controlled-reply-v1",
        StructuredReplyModelPort(client, "controlled-reply-v1"),
        None,
        None,
    )
    result = await agent.classify(
        message={
            "message_id": "message",
            "subject": "Out of office",
            "body": "I am away.",
        }
    )
    assert result.category.value == "auto_reply"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "candidate",
    [
        {"field": "quantity", "value": "100", "quote": "missing quote"},
        {"field": "unrecognized", "value": "100", "quote": "We need hinges."},
        {"field": "quantity", "value": "", "quote": "We need hinges."},
        {"field": "quantity", "value": "100", "quote": ""},
    ],
)
async def test_agent_reports_deterministic_rejected_candidates_without_original(
    candidate,
):
    import json

    from agent_runtime.qualification_agent.agent import QualificationAgent
    from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort

    client = _StructuredClient(
        json.dumps(
            {"category": "provides_specification", "candidate_fields": [candidate]}
        )
    )
    agent = QualificationAgent(
        "controlled-reply-v1",
        StructuredReplyModelPort(client, "controlled-reply-v1"),
        None,
        None,
    )
    result = await agent.classify(
        message={
            "message_id": "sample",
            "subject": "Current reply",
            "body": "We need hinges.",
        }
    )
    assert result.rejected_candidates is True and result.candidate_fields == ()
