"""模型入口的受信身份、敏感输入与严格输出契约。"""

import json

import pytest

from agent_runtime.business_inquiry import BusinessInquiryPlanner
from shared.errors import ValidationError
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelResponse,
    ModelUsage,
)
from tests.unit.test_business_inquiry import PLAN, TEXT
from tests.unit.test_business_reply import scope


def identity():
    return InvocationIdentity(
        **scope().actor.model_dump(),
        run_id="run-fixture",
        capability="business_mail_followup",
        configuration_version="fixture",
        sequence=0,
    )


class Generator:
    def __init__(self, text=None, model="fixture"):
        self.calls = []
        self.text = text or json.dumps(PLAN)
        self.model = model

    async def generate(self, ident, request):
        self.calls.append((ident, request))
        return ModelResponse(
            text=self.text,
            model=self.model,
            usage=ModelUsage(
                input_tokens=None, output_tokens=None, cached_input_tokens=None
            ),
        )


async def test_planner_uses_measured_model_port_and_only_current_customer_text():
    gen = Generator()
    planner = BusinessInquiryPlanner(gen, model="fixture", max_output_tokens=1600)
    result = await planner.analyze(
        identity(), {"body": TEXT + "\n> Quantity: 999 units"}, followup=False
    )
    assert result.intent == "buyer_inquiry" and len(gen.calls) == 1
    assert gen.calls[0][1].payload["untrusted_mail"]["body"] == TEXT
    assert gen.calls[0][0].capability == "business_mail_followup"


async def test_classifier_identity_cannot_be_reused_to_follow_up():
    gen = Generator()
    planner = BusinessInquiryPlanner(gen, model="fixture", max_output_tokens=1600)
    with pytest.raises(ModelGenerationError):
        await planner.analyze(
            identity().model_copy(update={"capability": "business_mail_triage"}),
            {"body": TEXT},
            followup=False,
        )
    assert not gen.calls


async def test_sensitive_marker_blocks_before_model():
    gen = Generator()
    planner = BusinessInquiryPlanner(gen, model="fixture", max_output_tokens=1600)
    with pytest.raises(ValidationError):
        await planner.analyze(
            identity(),
            {"body": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"},
            followup=False,
        )
    assert not gen.calls


@pytest.mark.parametrize(
    "text,model",
    [
        ("not json", "fixture"),
        (json.dumps(PLAN | {"send": True}), "fixture"),
        (json.dumps(PLAN), "wrong"),
    ],
)
async def test_invalid_or_changed_model_output_cannot_become_reply(text, model):
    gen = Generator(text, model)
    planner = BusinessInquiryPlanner(gen, model="fixture", max_output_tokens=1600)
    with pytest.raises(ModelGenerationError):
        await planner.analyze(identity(), {"body": TEXT}, followup=False)
