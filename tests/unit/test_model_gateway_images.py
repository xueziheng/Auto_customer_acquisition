"""图像透传、容量和摘要指纹不扩大账本正文暴露面。"""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelInputImage,
    ModelLimits,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import (
    GatewayModelGenerator,
    ModelGenerateHandler,
    ModelResponseSlot,
)


def actor():
    return InvocationIdentity(tenant_id="tn_test", user_id="usr_test", employee_id="emp_test", run_id="run_test", capability="knowledge_ingest", configuration_version="v1", sequence=0)


def request(images=()):
    return ModelRequest(model="test-model", system_prompt="JSON", payload={"text": "private"}, max_output_tokens=8, images=images)


def image(marker=b"PRIVATE_IMAGE"):
    data = b"\x89PNG\r\n\x1a\n" + marker
    return ModelInputImage(mime_type="image/png", data=data, sha256=hashlib.sha256(data).hexdigest(), byte_length=len(data))


async def test_fingerprint_is_ordered_digest_only_and_text_path_unchanged():
    captured = []

    class Fingerprints:
        def fingerprint(self, parts):
            captured.append(parts)
            return "0" * 64, "v1"

    async def prepare(req):
        handler = ModelGenerateHandler(actor(), req, ModelResponseSlot(), Fingerprints(), None, None, None, None)
        return await handler.prepare(None, None)

    await prepare(request())
    identity_bytes, plain = captured[-1]
    expected = json.dumps({"model": "test-model", "instructions": "JSON", "payload": {"text": "private"}, "max_output_tokens": 8}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    assert plain == expected
    assert identity_bytes == actor().model_dump_json().encode()
    first, second = image(), image(b"SECOND_PRIVATE_IMAGE")
    prepared = await prepare(request((first, second)))
    forward = captured[-1][1]
    await prepare(request((second, first)))
    assert captured[-1][1] != forward
    assert b"PRIVATE_IMAGE" not in forward
    assert json.loads(forward)["images"] == [
        {"mime_type": item.mime_type, "sha256": item.sha256, "byte_length": item.byte_length}
        for item in (first, second)
    ]
    assert "PRIVATE_IMAGE" not in repr(prepared)
    assert dict(prepared.audit_projection) == {"capability": "knowledge_ingest", "image_count": 2, "image_bytes": first.byte_length + second.byte_length}


async def test_gateway_freezes_payload_without_losing_attachments(monkeypatch):
    from tool_gateway import handlers
    from tool_gateway.errors import ToolCallStatus

    captured = []
    original = ModelGenerateHandler

    def handler(*args):
        value = original(*args)
        captured.append(value)
        return value

    async def invoke(self, ctx):
        value = captured[-1]
        assert value._request.images == (attachment,)
        assert value._request.payload is not source.payload
        response = ModelResponse(text='{"ok":true}', model="test-model", usage=ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1))
        return SimpleNamespace(status=ToolCallStatus.SUCCEEDED, output={"provider_ref": value._slot.put(response)})

    monkeypatch.setattr(handlers.model_generate, "ModelGenerateHandler", handler)
    monkeypatch.setattr(handlers.model_generate.ToolGateway, "invoke", invoke)
    # 文本限额继续只管既有文本，图像容量由共享契约固定约束。
    limits = ModelLimits(window_seconds=60, tenant_calls=2, employee_calls=2, tenant_concurrency=1, employee_concurrency=1, max_input_bytes=100, max_output_tokens=8, timeout_seconds=5)
    generator = GatewayModelGenerator(authority=None, usage=None, provider_factory=None, limits=limits, model="test-model", configuration_version="v1", ledger_factory=None, fingerprints=HmacFingerprintProvider("v1", b"k" * 32), lease_owner="unit", lease_duration=timedelta(seconds=30))
    attachment = image(b"PRIVATE_IMAGE" * 100)
    source = request((attachment,))
    assert (await generator.generate(actor(), source)).text == '{"ok":true}'


async def test_output_limit_settles_invalid_and_never_creates_success_result():
    from connectors.deepseek.client import DeepSeekFailure
    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

    states = []
    provider_calls = []

    class Authority:
        async def check(self, identity):
            pass

    class Usage:
        async def mark_dispatched(self, tenant, invocation):
            states.append("dispatched")

        async def finish(self, tenant, invocation, usage, state):
            states.append(state)

    class Provider:
        async def generate(self, model_request):
            provider_calls.append(model_request)
            raise DeepSeekFailure("output_limit")

        async def aclose(self):
            pass

    slot = ModelResponseSlot()
    handler = ModelGenerateHandler(
        actor(), request(), slot, None,
        SimpleNamespace(reservation=SimpleNamespace(invocation_id="minv_fixture")),
        Usage(), Authority(), Provider,
    )
    with pytest.raises(ToolGatewayError) as error:
        await handler.execute(actor().tenant_id, None)
    assert error.value.category is ToolErrorCategory.PROVIDER_PERMANENT
    assert states == ["dispatched", "invalid"]
    assert len(provider_calls) == 1
    assert handler.failure.code == "output_limit"
    with pytest.raises(ValueError):
        slot.take("missing")
