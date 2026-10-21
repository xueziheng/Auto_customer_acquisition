"""平台管理员的一次短模型连接测试，凭证与正文不进入账本。"""
from collections.abc import Awaitable, Callable, Mapping

from connectors.deepseek.client import DeepSeekFailure
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from shared.schemas.model_invocation import ModelRequest
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import ModelProvider, PreparedModelProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, SafeScalar, ToolCallContext

MODEL_CONNECTION_MANIFEST = ToolManifest(
    tool_id="model.connection.test", version="v1", description="管理员显式短模型连接测试",
    risk_level=RiskLevel.LOW, cost_class=CostClass.LOW, requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED, required_permissions=("platform:configure",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={"type": "object", "properties": {"configuration_version": {"type": "string"}}, "required": ("configuration_version",), "additionalProperties": False},
    output_schema={"type": "object", "properties": {"provider_ref": {"type": "string"}}, "required": ("provider_ref",), "additionalProperties": False},
)


class ModelConnectionProbe:
    def __init__(self, tenant: TenantId, actor: str, version: str, key: str,
        authorize: Callable[[], Awaitable[None]], factory: Callable[[], ModelProvider],
        model: str, fingerprints: HmacFingerprintProvider) -> None:
        self._tenant, self._actor, self._version, self._key = tenant, actor, version, key
        self._authorize, self._factory, self._model, self._fingerprints = authorize, factory, model, fingerprints
        self._prepared: PreparedToolCall | None = None
        self._started = False
        self.result: tuple[str, str | None] | None = None

    async def prepare(self, ctx: ToolCallContext, preflight: object | None) -> PreparedToolCall:
        if self._prepared is not None or preflight is not None or ctx.tenant_id != self._tenant or str(ctx.user_id) != self._actor or ctx.idempotency_key != self._key or ctx.tool_id != MODEL_CONNECTION_MANIFEST.tool_id or dict(ctx.params) != {"configuration_version": self._version}:
            raise ValidationError("模型测试绑定无效")
        await self._authorize()
        digest, key_version = self._fingerprints.fingerprint((str(self._tenant).encode(), self._version.encode(), self._key.encode()))
        self._prepared = PreparedToolCall(digest, key_version, {"model_configuration": self._version}, None)
        return self._prepared

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> Mapping[str, SafeScalar]:
        if tenant_id != self._tenant or prepared is not self._prepared or self._started:
            raise ValidationError("模型测试执行无效")
        self._started = True
        await self._authorize()
        provider = self._factory()
        try:
            if not isinstance(provider, PreparedModelProvider):
                raise ValidationError("模型连接测试不支持准备接口")
            await provider.prepare()
            response = await provider.generate(ModelRequest(model=self._model,
                system_prompt='Return only this JSON object: {"ok":true}.',
                payload={"test": "connection"}, max_output_tokens=128))
            import json
            value = json.loads(response.text)
            passed = isinstance(value, dict) and set(value) == {"ok"} and value["ok"] is True
            result = ("passed", None) if passed else ("failed", "invalid_response")
        except DeepSeekFailure as exc:
            result = ("failed", exc.code)
        except Exception:  # noqa: BLE001 -- 只返回固定安全错误，禁止凭证和 SDK 原文泄漏
            result = ("failed", "unknown")
        finally:
            await provider.aclose()
        await self._authorize()
        self.result = result
        return {"provider_ref": self._key}
