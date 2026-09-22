"""调用身份由受信客户端绑定；模型输出槽不能被其他任务领取。"""

import asyncio

import pytest

from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelResponse,
    ModelUsage,
)


async def test_wrapper_keeps_trusted_identity():
    from agent_runtime.gateway_model import GatewayJsonModelClient

    seen = []

    class Generator:
        async def generate(self, identity, request):
            seen.append(identity)
            return ModelResponse(
                text='{"ok":true}',
                model=request.model,
                usage=ModelUsage(
                    input_tokens=1, cached_input_tokens=0, output_tokens=1
                ),
            )

    identity = InvocationIdentity(
        tenant_id="tn_test",
        user_id="usr_test",
        employee_id="emp_test",
        run_id="run_test",
        capability="product_help",
        configuration_version="v1",
        sequence=0,
    )
    client = GatewayJsonModelClient(identity, Generator())
    assert (
        await client.complete_json(
            model="test-model",
            system_prompt="规则",
            payload={"tenant_id": "attacker"},
            max_output_tokens=64,
        )
        == '{"ok":true}'
    )
    assert seen == [identity]


async def test_response_slot_is_single_use_and_task_owned():
    from tool_gateway.handlers.model_generate import ModelResponseSlot

    slot = ModelResponseSlot()
    response = ModelResponse(
        text='{"ok":true}',
        model="test-model",
        usage=ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
    )
    handle = slot.put(response)

    async def steal():
        with pytest.raises(ValueError):
            slot.take(handle)

    await asyncio.create_task(steal())
    assert slot.take(handle).text == '{"ok":true}'
    with pytest.raises(ValueError):
        slot.take(handle)
