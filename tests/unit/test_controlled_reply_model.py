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


@pytest.mark.parametrize(
    "operation", ["init", "set_response", "complete_json", "call_count"]
)
@pytest.mark.parametrize("fail", [False, True])
async def test_every_controlled_connection_closes_with_transaction(
    tmp_path, monkeypatch, operation, fail
):
    from infra.controlled.reply_model import ControlledReplyModelClient

    class CountedConnection:
        def __init__(self):
            self.closes = 0
            self.commits = 0
            self.rollbacks = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.rollbacks += int(exc_type is not None)
            self.commits += int(exc_type is None)

        def execute(self, statement, parameters=()):
            if fail:
                raise RuntimeError("controlled_connection_failure")
            return self

        def fetchone(self):
            return (1,)

        def close(self):
            self.closes += 1

    model = ControlledReplyModelClient(tmp_path / "owned.sqlite", tenant_id="tn_test")
    connection = CountedConnection()
    monkeypatch.setattr(ControlledReplyModelClient, "_connect", lambda self: connection)

    async def run():
        if operation == "init":
            ControlledReplyModelClient(tmp_path / "owned.sqlite", tenant_id="tn_test")
        elif operation == "set_response":
            model.set_response({"subject": "Reply", "body": "Thanks"}, "configured")
        elif operation == "complete_json":
            await model.complete_json(
                model="controlled",
                system_prompt="rules",
                payload={"subject": "Reply", "body": "Thanks"},
                max_output_tokens=100,
            )
        else:
            assert model.call_count() == 1

    if fail:
        with pytest.raises(RuntimeError, match="controlled_connection_failure"):
            await run()
    else:
        await run()
    assert connection.closes == 1
    assert connection.commits == int(not fail)
    assert connection.rollbacks == int(fail)
