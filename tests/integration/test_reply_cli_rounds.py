"""真实固定 CLI/沙箱 + 合成 SDK；验证回信上下文在原额度内完成两轮，无外网。"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from agent_runtime.qualification_agent.agent import _SYSTEM_PROMPT
from apps.composition_support.model import configured_codex_provider
from apps.scheduler_worker.adapters.message_content_reader import project_reply_content
from connectors.codex_agent.client import AgentTransport
from connectors.codex_agent.runner import REPLY_INSTRUCTIONS
from shared.schemas.model_invocation import ModelGenerationError, ModelRequest
from tests.evals.reply_evals_runner import REPLIES_DIR
from tests.integration.test_qq_trial_model import FakeSDK
from tests.unit.test_reply_cli_profile import profile_identity, profile_settings

BINARY = Path("/usr/local/lib/node_modules/@openai/codex/node_modules/"
              "@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex")


class SerializedBudget:
    def __init__(self):
        self.sizes = []

    async def run(self, body, dispatch, *, output_key="max_output_tokens"):
        size = len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode())
        if sum(self.sizes) + size > 32768:
            raise ModelGenerationError("permission")
        self.sizes.append(size)
        return await dispatch(body)


def largest_eval_body():
    # 只读 input，不把 expected 标签送给模型；包含生产中文分类说明的真实体积。
    from email.message import EmailMessage
    from email.policy import SMTP

    bodies = []
    for path in REPLIES_DIR.glob("*/*/input.json"):
        message = EmailMessage()
        message["Subject"] = "Synthetic offline acceptance"
        message.set_content(json.loads(path.read_text())["body"])
        view = project_reply_content(message.as_bytes(policy=SMTP),
                                     max_subject_chars=4096, max_body_chars=65536)
        bodies.append(view.body)
    assert len(bodies) >= 160
    return max(bodies, key=lambda body: len(json.dumps(body, ensure_ascii=True)))


@pytest.mark.skipif(not BINARY.is_file() or not shutil.which("bwrap"),
                    reason="需要服务器固定 CLI 和 Linux bwrap")
@pytest.mark.parametrize("case", ["whatsapp", "largest_eval"])
async def test_real_cli_reads_full_reply_context_within_original_budget(monkeypatch, case):
    budget, sdk = SerializedBudget(), FakeSDK()

    class OfflineTransport(AgentTransport):
        def __init__(self, *args, round_budget, **kwargs):
            self._round_budget = round_budget
            self._protocol, self._deepseek = "responses", True

        async def prepare(self):
            pass

        async def aclose(self):
            pass

        async def _sdk(self):
            return sdk

    monkeypatch.setattr("connectors.codex_agent.client.AgentTransport", OfflineTransport)
    body = ("我想加 WhatsApp，方便继续沟通，请告诉我如何联系。"
            if case == "whatsapp" else largest_eval_body())
    with tempfile.TemporaryDirectory(prefix="tr-cli-") as directory:
        root = Path(directory)
        settings = profile_settings(root).model_copy(update={"codex_binary": BINARY})
        provider = configured_codex_provider(settings, profile_identity(), round_budget=budget)
        request = ModelRequest(model=settings.model, system_prompt=_SYSTEM_PROMPT,
                               payload={"subject": "(current reply)", "body": body},
                               max_output_tokens=512)
        try:
            response = await provider.generate(request)
        finally:
            await provider.aclose()
        assert json.loads(response.text) == {"category": "clear_interest", "candidate_fields": []}
        assert len(sdk.requests) == len(budget.sizes) == 2
        assert sum(budget.sizes) <= 32768
        assert not list(root.iterdir())
        contexts = [item for item in sdk.requests[1]["input"]
                    if item.get("type") == "function_call_output"]
        context = json.loads(contexts[0]["output"].split("\nOutput:\n", 1)[-1])
        assert REPLY_INSTRUCTIONS.strip() == sdk.requests[0]["instructions"]
        assert context == {"instructions": _SYSTEM_PROMPT, "input": dict(request.payload)}
        # 结果来自固定 SDK，仅证明传输/预算/原件边界，不能当真实模型分类准确率。
        print(json.dumps({"case": case, "round_bytes": budget.sizes,
                          "external_model_calls": 0, "jobs_cleaned": True}))
