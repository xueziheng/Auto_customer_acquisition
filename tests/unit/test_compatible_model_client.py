"""真实 SDK 与受控 HTTP 验证兼容协议，避免仅保存设置的假接入。"""
import json

import httpx
import pytest
from openai import AsyncOpenAI

from connectors.deepseek.client import DeepSeekFailure
from connectors.model_api.client import CompatibleModelClient
from tests.unit.test_deepseek_client import Resolver, body, request


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
async def test_selected_protocol_sends_exact_model_and_returns_final_json(protocol):
    seen = []
    def transport(req):
        sent = json.loads(req.content)
        seen.append((req.url.path, sent["model"]))
        assert req.headers["authorization"] == "Bearer test-only-key-never-a-real-credential"
        if protocol == "responses":
            assert sent["store"] is False and "reasoning" not in sent
            result = body(model="resolved-model-version")
        else:
            assert sent["response_format"] == {"type": "json_object"}
            result = {"id": "test", "object": "chat.completion", "created": 1, "model": "resolved-version", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok":true}'}}], "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11}}
        return httpx.Response(200, json=result)
    client = CompatibleModelClient("MODEL_KEY", Resolver(), base_url="https://api.moonshot.cn/v1", protocol=protocol, timeout_seconds=5,
        client_factory=lambda key, timeout: AsyncOpenAI(api_key=key, base_url="https://api.moonshot.cn/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=False)))
    try:
        response = await client.generate(request())
        assert response.text == '{"ok":true}' and response.model == "test-model"
        assert seen == [("/v1/responses" if protocol == "responses" else "/v1/chat/completions", "test-model")]
        assert response.usage.output_tokens == (3 if protocol == "responses" else 4)
    finally:
        await client.aclose()


@pytest.mark.parametrize("status", [302, 401, 429])
async def test_failure_never_redirects_or_retries_and_hides_key(status):
    calls = []
    def transport(req):
        calls.append(req.url)
        return httpx.Response(status, headers={"location": "https://other.invalid/"}, json={"error": "synthetic-sensitive-error"})
    client = CompatibleModelClient("MODEL_KEY", Resolver(), base_url="https://api.moonshot.cn/v1", protocol="chat_completions", timeout_seconds=5,
        client_factory=lambda key, timeout: AsyncOpenAI(api_key=key, base_url="https://api.moonshot.cn/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=False)))
    try:
        with pytest.raises(DeepSeekFailure) as exc:
            await client.generate(request())
        assert "synthetic-sensitive" not in str(exc.value)
        assert len(calls) == 1
    finally:
        await client.aclose()
