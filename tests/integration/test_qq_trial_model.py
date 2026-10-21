"""真实 model.generate/Gateway/PG 额度 + 原 broker；SDK 固定夹具且零网络。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from connectors.codex_agent.client import AgentBroker, AgentProvider, AgentTransport
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelRequest,
)
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.test_model_gateway import Authority, Provider
from tests.integration.test_model_usage import limits
from tests.integration.test_qq_trial_ledger import (
    received,
    snapshot,
)
from tests.integration.test_qq_trial_ledger import (
    trial as trial,  # noqa: PLC0414 - pytest fixture re-export
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.qq_trial_model import TrialModelGenerator, TrialModelPorts


class FakeSDK:
    def __init__(self, mode="ok"):
        self.mode, self.requests = mode, []
        self.started = asyncio.Event()
        self.responses = self

    async def create(self, **body):
        self.requests.append(body)
        self.started.set()
        if self.mode == "slow":
            await asyncio.Event().wait()
        if self.mode == "unknown":
            raise OSError("synthetic unknown result")
        output = (
            [
                {
                    "type": "function_call",
                    "id": "f1",
                    "call_id": "c1",
                    "name": "mcp__tradeos__read_task_context",
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
            if len(self.requests) == 1
            else [
                {
                    "type": "message",
                    "id": "m1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"category":"clear_interest","candidate_fields":[]}',
                        }
                    ],
                }
            ]
        )
        result = {
            "id": "fixture-response",
            "status": "completed",
            "model": body["model"],
            "output": output,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 0},
            },
        }
        return SimpleNamespace(model_dump=lambda: result)


class FakeTransport(AgentTransport):
    def __init__(self, budget, sdk):
        self._round_budget, self.sdk, self._protocol, self._deepseek = (
            budget,
            sdk,
            "responses",
            False,
        )

    async def _sdk(self):
        return self.sdk


class FixedBrokerProvider(AgentProvider):
    def __init__(self, budget, sdk):
        self._delegate = FakeTransport(budget, sdk)

    async def prepare(self):
        pass

    async def aclose(self):
        pass

    async def generate(self, request):
        broker = AgentBroker(self._delegate, request, max_output_bytes=4096)
        first = {
            "model": request.model,
            "input": [{"role": "user", "content": "start"}],
            "tools": [{"type": "function", "name": "mcp__tradeos__read_task_context"}],
        }
        await broker.exchange(first)
        await broker.exchange(
            {
                **first,
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "output": '{"instructions":"Return JSON","input":{"test":true}}',
                    }
                ],
            }
        )
        return broker.response


def assemble(gate, sessions, sdk, *, provider_factory=None, **limits_changes):
    scope = gate.scope
    ports = TrialModelPorts(
        Authority(),
        SqlModelUsageRepository(sessions),
        lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        HmacFingerprintProvider("v1", b"k" * 32),
        limits(**limits_changes),
        "offline-qq-model",
        timedelta(seconds=30),
        "deepseek",
    )
    generator = TrialModelGenerator(
        gate,
        message_id="msg_offline_fixture",
        ports=ports,
        provider_factory=provider_factory
        or (lambda budget, identity: FixedBrokerProvider(budget, sdk)),
    )
    actor = InvocationIdentity(
        tenant_id=scope.route.tenant_id,
        user_id=scope.classifier_user_id,
        employee_id=scope.classifier_employee_id,
        run_id=new_id("run"),
        capability="reply_qualification",
        configuration_version=scope.model_configuration_version,
        sequence=0,
    )
    request = ModelRequest(
        model=scope.model,
        system_prompt="Return JSON",
        payload={"test": True},
        max_output_tokens=512,
    )
    return generator, actor, request


async def test_original_gateway_and_broker_account_each_round_and_stricter_global_limit(
    trial,
):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    sdk = FakeSDK()
    generator, actor, request = assemble(gate, sessions, sdk, max_output_tokens=32)
    response = await generator.generate(actor, request)
    assert response.usage.output_tokens == 10
    assert [b["max_output_tokens"] for b in sdk.requests] == [32, 27]
    row = await snapshot(gate, sessions)
    assert (row.classifications, row.provider_requests, row.output_tokens) == (1, 2, 10)
    assert 0 < row.input_bytes < 32768
    with pytest.raises(QQTrialStopped):
        await generator.generate(actor, request)
    assert len(sdk.requests) == 2


async def test_miswired_provider_is_rejected_before_generate(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    provider = Provider()
    generator, actor, request = assemble(
        gate, sessions, FakeSDK(), provider_factory=lambda b, i: provider
    )
    with pytest.raises((ModelGenerationError, QQTrialStopped)):
        await generator.generate(actor, request)
    assert provider.calls == 0
    assert (await snapshot(gate, sessions)).provider_requests == 0


# 三阶段 timeout/cancel 与未知结果见 test_qq_trial_model_boundaries.py。


async def test_stricter_global_input_limit_rejects_before_sdk(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    sdk = FakeSDK()
    generator, actor, request = assemble(gate, sessions, sdk, max_input_bytes=8)
    with pytest.raises(ModelGenerationError):
        await generator.generate(actor, request)
    assert sdk.requests == []
