"""受控模型仅返回显式外部响应，调用留痕跨实例，不推导业务事实。"""

import importlib

import pytest


async def test_controlled_response_is_explicit_durable_and_payload_narrow(tmp_path):
    module = importlib.import_module("infra.controlled.reply_model")
    model = module.ControlledReplyModelClient(
        tmp_path / "model.sqlite", tenant_id="tn_test"
    )
    payload = {"subject": "(current reply)", "body": "Thanks."}
    response = '{"category":"other","candidate_fields":[]}'
    model.set_response(payload, response)
    assert (
        await model.complete_json(
            model="controlled-reply-v1",
            system_prompt="rules",
            payload=payload,
            max_output_tokens=100,
        )
        == response
    )
    restored = module.ControlledReplyModelClient(
        tmp_path / "model.sqlite", tenant_id="tn_test"
    )
    assert restored.call_count() == 1
    with pytest.raises(Exception, match="controlled_model_input_rejected"):
        await restored.complete_json(
            model="controlled-reply-v1",
            system_prompt="rules",
            payload={**payload, "raw": "private"},
            max_output_tokens=100,
        )
    with pytest.raises(Exception, match="controlled_model_response_unconfigured"):
        await restored.complete_json(
            model="controlled-reply-v1",
            system_prompt="rules",
            payload={**payload, "body": "Unknown"},
            max_output_tokens=100,
        )
    assert restored.call_count() == 2
