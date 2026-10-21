"""普通来信prompt受控候选契约评估；无Provider网络，不能证明真实模型分类准确率。"""

import json
from pathlib import Path

import pytest

from agent_runtime.business_mail import BusinessMailClassifier
from shared.errors import ValidationError
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)

CASES = tuple(sorted(Path(__file__).parent.glob("*/input.json")))
MODEL = "synthetic-business-mail-eval-v1"
IDENTITY = InvocationIdentity(
    tenant_id="tn_business_eval",
    employee_id="emp_business_eval",
    user_id="usr_business_eval",
    run_id="run_business_eval",
    capability="business_mail_triage",
    configuration_version="synthetic-eval-v1",
    sequence=0,
)


class CandidateModel:
    def __init__(self, candidate: object) -> None:
        self.response = ModelResponse(
            text=json.dumps(candidate, ensure_ascii=False),
            model=MODEL,
            usage=ModelUsage(
                input_tokens=None, cached_input_tokens=None, output_tokens=None
            ),
        )
        self.calls: list[tuple[InvocationIdentity, ModelRequest]] = []

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        self.calls.append((identity, request))
        return self.response


@pytest.mark.parametrize("input_path", CASES, ids=lambda p: p.parent.name)
async def test_business_mail_controlled_contract(input_path: Path) -> None:
    case = json.loads(input_path.read_text())
    expected = json.loads(input_path.with_name("expected.json").read_text())
    model = CandidateModel(case["candidate_response"])
    classifier = BusinessMailClassifier(model, model=MODEL, max_output_tokens=512)

    async def classify():
        return await classifier.classify(
            IDENTITY, subject=case["subject"], body=case["body"]
        )

    if expected["outcome"] == "input_rejected":
        with pytest.raises(ValidationError):
            await classify()
    elif expected["outcome"] == "invalid_response":
        with pytest.raises(ModelGenerationError) as caught:
            await classify()
        assert caught.value.code == "invalid_response"
    else:
        result = await classify()
        assert result.category == expected["category"]
        assert result.evidence_quote in case["subject"] + "\n" + case["body"]
        assert set(result.model_dump()) == {"category", "reason", "evidence_quote"}
    assert len(model.calls) == expected["model_calls"]
    if model.calls:
        identity, request = model.calls[0]
        assert identity == IDENTITY
        assert request.payload["untrusted_mail"] == {
            "subject": case["subject"],
            "body": case["body"],
        }
        assert set(request.payload) == {"untrusted_mail", "response_schema"}
        assert request.payload["response_schema"]["additionalProperties"] is False
        assert set(request.payload["response_schema"]["properties"]) == {
            "category",
            "reason",
            "evidence_quote",
        }
        assert request.images == () and request.max_output_tokens == 512
        assert "tenant_id" not in request.payload and "action" not in request.payload
        assert (
            "不调用工具" in request.system_prompt
            and "不浏览网络" in request.system_prompt
        )
