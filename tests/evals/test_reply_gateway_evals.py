"""冻结语料只用于评分；模型输入来自生产投影后的 canonical 入站映射。"""

import hashlib
import importlib
import json
from dataclasses import replace
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from agent_runtime.qualification_agent.agent import QualificationAgent
from agent_runtime.qualification_agent.openai_port import StructuredReplyModelPort
from apps.scheduler_worker.adapters.message_content_reader import project_reply_content
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id
from tests.evals.reply_evals_runner import REPLIES_DIR


def adapter_type():
    return importlib.import_module(
        "tests.evals.reply_gateway_evals"
    ).GatewayReplyEvalClassifier


class JsonProvider:
    def __init__(self, output=None):
        self.payloads = []
        self.output = output or '{"category":"auto_reply","candidate_fields":[]}'

    async def complete_json(self, *, model, system_prompt, payload, max_output_tokens):
        self.payloads.append(dict(payload))
        return self.output


def classifier(provider):
    return QualificationAgent(
        "controlled-eval-port",
        StructuredReplyModelPort(provider, "controlled-eval-port"),
        None,
        None,
    )


def projected(body, subject="expected-label-must-not-be-sent"):
    raw = EmailMessage()
    raw["Subject"] = subject
    raw.set_content(body)
    view = project_reply_content(
        raw.as_bytes(policy=SMTP), max_subject_chars=4096, max_body_chars=65536
    )
    input_type = importlib.import_module(
        "tests.evals.reply_gateway_evals"
    ).GatewayReplyEvalInput
    return input_type(str(new_id("msg")), view)


def corpus_hashes():
    return {
        str(p.relative_to(REPLIES_DIR)): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in ("*/*/input.json", "*/*/expected.json")
        for p in REPLIES_DIR.glob(pattern)
    }


async def test_eval_projection_does_not_leak_expected_labels():
    before = corpus_hashes()
    inputs = list(REPLIES_DIR.glob("*/*/input.json"))
    assert len(inputs) == 160
    source = json.loads(
        (REPLIES_DIR / "provides_specification/case_01/input.json").read_text()
    )
    provider = JsonProvider()
    message = projected(source["body"], source["subject"])
    adapter = adapter_type()(classifier(provider), {source["message_id"]: message})
    assert adapter.model == "controlled-eval-port"
    # runner 的主题/正文不作为提示；只能按语料 ID 定位经过生产护栏的入站原文。
    result = await adapter.classify(
        message={
            "message_id": source["message_id"],
            "subject": "provides_specification",
            "body": "expected: provides_specification",
        }
    )
    assert result.category.value == "auto_reply"
    assert provider.payloads == [
        {"subject": "(current reply)", "body": message.content.body}
    ]
    assert set(provider.payloads[0]) == {"subject", "body"}
    assert corpus_hashes() == before


async def test_eval_uses_current_expression_and_keeps_validated_mapping_snapshot():
    provider = JsonProvider()
    message = projected(
        "Thanks.\n\nOn Tuesday Supplier wrote:\n> We need 9000 units.\n"
    )
    mapping = {"fixture-opaque-id": message}
    adapter = adapter_type()(classifier(provider), mapping)
    expected = message.content.body
    assert "9000" not in expected
    mapping["fixture-opaque-id"] = replace(
        message, content=replace(message.content, body="password: synthetic")
    )
    mapping.clear()
    await adapter.classify(
        message={"message_id": "fixture-opaque-id", "subject": "label", "body": "label"}
    )
    assert provider.payloads == [{"subject": "(current reply)", "body": expected}]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_canonical",
        "missing_id",
        "extra_expected",
        "label_subject",
        "credential",
    ],
)
def test_eval_rejects_ambiguous_or_unprojected_mapping(case):
    provider = JsonProvider()
    first = projected("Thanks.")
    second = projected("Thank you.")
    if case == "duplicate_canonical":
        second = replace(second, message_id=first.message_id)
    elif case == "missing_id":
        first = replace(first, message_id="")
    elif case == "extra_expected":
        first = {"message_id": first.message_id, "expected": "unsubscribe"}
    elif case == "label_subject":
        first = replace(
            first, content=replace(first.content, subject="unsubscribe case 01")
        )
    else:
        first = replace(
            first, content=replace(first.content, body="password: synthetic")
        )
    with pytest.raises(ValidationError):
        adapter_type()(classifier(provider), {"a": first, "b": second})
    assert provider.payloads == []


@pytest.mark.parametrize(
    "message",
    [
        {"subject": "x", "body": "x"},
        {"message_id": "unknown", "subject": "x", "body": "x"},
        {"message_id": "a", "subject": "x", "body": "x", "expected": "unsubscribe"},
    ],
)
async def test_eval_missing_or_untrusted_case_fails_without_model(message):
    provider = JsonProvider()
    adapter = adapter_type()(classifier(provider), {"a": projected("Thanks.")})
    with pytest.raises(ValidationError):
        await adapter.classify(message=message)
    assert provider.payloads == []


@pytest.mark.parametrize(
    "body,quote",
    [
        ("Thanks.", "5000 units"),
        ("Thanks.", "(current reply)"),
        ("Contact buyer@example.test.", "[private reference omitted]"),
        ("Contact buyer@example.test.", "private reference"),
        ("We need\n> a previous note\n100 units.", "expression boundary"),
        ("We need\n> a previous note\n100 units.", "[current expression boundary]"),
    ],
)
async def test_eval_rejects_evidence_that_production_workflow_would_reject(body, quote):
    provider = JsonProvider(
        json.dumps(
            {
                "category": "provides_specification",
                "candidate_fields": [
                    {"field": "quantity", "value": "5000", "quote": quote}
                ],
            }
        )
    )
    message = projected(body)
    if quote.startswith("["):
        assert quote in message.content.body
    adapter = adapter_type()(classifier(provider), {"a": message})
    with pytest.raises(ValidationError):
        await adapter.classify(
            message={"message_id": "a", "subject": "ignored", "body": "ignored"}
        )
    assert len(provider.payloads) == 1


async def test_eval_rejects_quote_spanning_distinct_current_evidence_segments():
    body = "We need 100 units."
    provider = JsonProvider(
        json.dumps(
            {
                "category": "provides_specification",
                "candidate_fields": [
                    {"field": "quantity", "value": "100", "quote": body}
                ],
            }
        )
    )
    message = projected(body)
    message = replace(
        message,
        content=replace(message.content, evidence_segments=("We need", "100 units.")),
    )
    adapter = adapter_type()(classifier(provider), {"a": message})
    with pytest.raises(ValidationError):
        await adapter.classify(
            message={"message_id": "a", "subject": "ignored", "body": "ignored"}
        )


async def test_eval_preserves_verbatim_unicode_and_keeps_original_out_of_model():
    body = "我们需要 ５０００ 件铰链。"
    quote = "５０００ 件铰链"
    provider = JsonProvider(
        json.dumps(
            {
                "category": "provides_specification",
                "candidate_fields": [
                    {"field": "quantity", "value": "5000", "quote": quote}
                ],
            },
            ensure_ascii=False,
        )
    )
    message = projected(body)
    adapter = adapter_type()(classifier(provider), {"a": message})
    result = await adapter.classify(
        message={"message_id": "a", "subject": "ignored", "body": "ignored"}
    )
    assert result.candidate_fields[0].quote == quote
    assert provider.payloads == [
        {"subject": "(current reply)", "body": message.content.body}
    ]
