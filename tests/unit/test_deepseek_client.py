"""真实 SDK + 受控 HTTP，验证模型协议、无重试与脱敏。"""

import asyncio
import json
import logging

import httpx
import pytest
from openai import AsyncOpenAI

from shared.schemas.model_invocation import ModelRequest

SENTINEL = "PRIVATE_MODEL_BODY"
KEY = "test-only-key-never-a-real-credential"


def body(**patch):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_test",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "PRIVATE_THINKING"}],
            },
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": '{"ok":true}', "annotations": []}
                ],
            },
        ],
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens": 3,
            "total_tokens": 15,
        },
        **patch,
    }


def request():
    return ModelRequest(
        model="test-model",
        system_prompt="只能返回 JSON",
        payload={"message": SENTINEL},
        max_output_tokens=64,
    )


class Resolver:
    def resolve(self, secret_ref):
        assert secret_ref == "MODEL_KEY"
        return KEY


def client(transport):
    from connectors.deepseek.client import DeepSeekClient

    return DeepSeekClient(
        "MODEL_KEY",
        Resolver(),
        timeout_seconds=5,
        client_factory=lambda key, timeout: AsyncOpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            max_retries=0,
            timeout=timeout,
            http_client=httpx.AsyncClient(transport=transport, follow_redirects=False),
        ),
    )


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_noncompleted_response_cannot_be_used(status):
    from connectors.deepseek.client import DeepSeekFailure, decode_response

    with pytest.raises(DeepSeekFailure) as error:
        decode_response(body(status=status), "test-model")
    assert error.value.code == "invalid_response"


def test_missing_usage_stays_unknown_and_reasoning_is_not_returned():
    from connectors.deepseek.client import decode_response

    result = decode_response(body(usage=None), "test-model")
    assert result.text == '{"ok":true}'
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert "PRIVATE_THINKING" not in repr(result)


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "other-model"},
        {"output": []},
        {
            "output": [
                {"type": "function_call", "name": "send_email", "arguments": "{}"}
            ]
        },
        {"usage": {"input_tokens": True, "output_tokens": 3}},
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "[]"}],
                }
            ]
        },
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": "{}"}],
                }
            ]
        },
    ],
)
def test_invalid_payload_is_not_a_success(patch):
    from connectors.deepseek.client import DeepSeekFailure, decode_response

    with pytest.raises(DeepSeekFailure):
        decode_response(body(**patch), "test-model")


async def test_sdk_request_and_usage_are_explicit(caplog):
    caplog.set_level(logging.DEBUG)
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(200, json=body())

    connector = client(httpx.MockTransport(respond))
    try:
        result = await connector.generate(request())
    finally:
        await connector.aclose()
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == "https://api.deepseek.com/responses"
    payload = json.loads(req.content)
    assert payload["reasoning"] == {"effort": "none"}
    assert payload["text"] == {"format": {"type": "json_object"}}
    assert payload["max_output_tokens"] == 64
    assert payload["store"] is False
    assert "tools" not in payload
    assert result.usage.model_dump() == {
        "input_tokens": 12,
        "cached_input_tokens": 4,
        "output_tokens": 3,
    }
    for sensitive in (KEY, SENTINEL, "PRIVATE_THINKING"):
        assert sensitive not in caplog.text


async def test_private_sdk_logging_does_not_hide_other_tasks(caplog):
    caplog.set_level(logging.DEBUG)
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(req):
        entered.set()
        await release.wait()
        logging.getLogger("httpcore.http11").debug("PRIVATE_PROVIDER_RESPONSE")
        return httpx.Response(200, json=body())

    connector = client(httpx.MockTransport(respond))
    task = asyncio.create_task(connector.generate(request()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        logging.getLogger("openai._base_client").info("unrelated-task-diagnostic")
        release.set()
        await task
        logging.getLogger("httpcore.http11").info("after-call-diagnostic")
        assert "unrelated-task-diagnostic" in caplog.text
        assert "after-call-diagnostic" in caplog.text
        assert SENTINEL not in caplog.text
        assert "PRIVATE_PROVIDER_RESPONSE" not in caplog.text
    finally:
        release.set()
        await task
        await connector.aclose()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication"),
        (402, "insufficient_balance"),
        (400, "invalid_request"),
        (429, "rate_limit"),
        (500, "provider_error"),
        (307, "invalid_response"),
    ],
)
async def test_provider_error_never_retries_or_exposes_body(status, code, caplog):
    from connectors.deepseek.client import DeepSeekFailure

    caplog.set_level(logging.DEBUG)
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(
            status,
            json={"error": {"message": SENTINEL}},
            headers={"Retry-After": "7", "Location": "https://untrusted.invalid"},
        )

    connector = client(httpx.MockTransport(respond))
    try:
        with pytest.raises(DeepSeekFailure) as error:
            await connector.generate(request())
        assert error.value.code == code
        assert len(calls) == 1
        assert error.value.dispatched
        assert SENTINEL not in str(error.value)
        assert SENTINEL not in caplog.text
        assert KEY not in caplog.text
    finally:
        await connector.aclose()


async def test_timeout_is_unknown_and_not_retried():
    from connectors.deepseek.client import DeepSeekFailure

    calls = []

    def respond(req):
        calls.append(req)
        raise httpx.ReadTimeout(SENTINEL, request=req)

    connector = client(httpx.MockTransport(respond))
    try:
        with pytest.raises(DeepSeekFailure) as error:
            await connector.generate(request())
        assert error.value.code == "unknown"
        assert error.value.dispatched
        assert len(calls) == 1
        assert SENTINEL not in str(error.value)
    finally:
        await connector.aclose()


async def test_close_prevents_lazy_secret_access():
    from connectors.deepseek.client import DeepSeekClient, DeepSeekFailure

    class RefuseResolver:
        def resolve(self, secret_ref):
            pytest.fail("关闭后不应读取凭证")

    connector = DeepSeekClient("MODEL_KEY", RefuseResolver(), timeout_seconds=5)
    await connector.aclose()
    await connector.aclose()
    with pytest.raises(DeepSeekFailure) as error:
        await connector.generate(request())
    assert not error.value.dispatched
