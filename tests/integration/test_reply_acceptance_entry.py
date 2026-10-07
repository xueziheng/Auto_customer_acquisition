"""用受控网络响应验证真实验收入口的接线；结果不计为模型质量通过。"""

import importlib

import pytest

from tests.integration.test_standalone_reply_chain import (
    BODY,
    ReplyProvider,
    reply_storage,  # noqa: F401
)
from tests.integration.test_standalone_reply_chain import reply_chain as _reply_chain

reply_chain = _reply_chain


class AcceptanceProvider(ReplyProvider):
    async def generate(self, request):
        if set(request.payload) == {"subject", "body"}:
            body = request.payload["body"]
            if body == BODY:
                from tests.integration.test_standalone_reply_chain import FIELDS

                self.output = {
                    "category": "provides_specification",
                    "candidate_fields": FIELDS,
                }
            elif body == "Please unsubscribe me.":
                self.output = {
                    "category": "unsubscribe",
                    "candidate_fields": [],
                    "suppress_scope": "contact",
                }
            else:
                self.output = {"category": "auto_reply", "candidate_fields": []}
        return await super().generate(request)


@pytest.mark.parametrize(
    "reply_chain", [{"tenant_calls": 163, "employee_calls": 163}], indirect=True
)
async def test_acceptance_runs_frozen_corpus_and_business_through_same_gateway(
    reply_chain, tmp_path
):
    execute = importlib.import_module(
        "tests.evals.reply_live_acceptance"
    ).run_acceptance
    chain = reply_chain
    provider = AcceptanceProvider()
    chain["model_provider"] = provider
    report = await execute(chain, tmp_path / "controlled-report.json")
    assert report["real_model"] is False
    assert report["corpus"]["total"] == 160
    assert report["corpus"]["overall_accuracy"] < 1
    assert report["business"] == {"positive": "passed", "unsubscribe": "passed"}
    assert report["corpus_unchanged"] is True
    assert report["canonical_runs"] == 160
    assert report["ledger"]["calls"] == 163
    assert len(provider.replies) == 162
    assert provider.probes == 1
    assert report["complete"] is False
    assert all(
        set(request.payload) == {"subject", "body"} for request in provider.replies
    )
