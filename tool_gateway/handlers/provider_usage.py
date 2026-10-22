"""免费供应商额度查询插件；一次受信调用绑定当前平台授权与配置。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from connectors.hunter.usage import HunterUsageConnector
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from shared.schemas.provider_usage import ProviderUsageObservation
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, SafeScalar, ToolCallContext

PROVIDER_USAGE_MANIFEST = ToolManifest(
    tool_id="provider.hunter.usage", version="v1", description="读取 Hunter 账户额度，不查询联系人",
    risk_level=RiskLevel.LOW, cost_class=CostClass.FREE, requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED, required_permissions=("platform:configure",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={"type": "object", "properties": {"configuration_version": {"type": "string"}}, "required": ("configuration_version",), "additionalProperties": False},
    output_schema={"type": "object", "properties": {"provider_ref": {"type": "string"}}, "required": ("provider_ref",), "additionalProperties": False},
)


class ProviderUsageHandler:
    """额度事实只在 SUCCEEDED 后交付，Gateway ledger 仅保存安全引用。"""

    def __init__(
        self, tenant: TenantId, actor: str, version: str, key: str,
        authorize: Callable[[], Awaitable[None]], connector: HunterUsageConnector,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        self._tenant, self._actor, self._version, self._key = tenant, actor, version, key
        self._authorize, self._connector, self._fingerprints = authorize, connector, fingerprints
        self._prepared: PreparedToolCall | None = None
        self._prepare_started = False
        self._started = False
        self.observation: ProviderUsageObservation | None = None

    async def prepare(self, ctx: ToolCallContext, preflight: object | None) -> PreparedToolCall:
        """仅允许受信工厂绑定的来源、用户、配置和一分钟请求键。"""
        if (self._prepare_started or preflight is not None or ctx.tenant_id != self._tenant
                or str(ctx.user_id) != self._actor or ctx.tool_id != PROVIDER_USAGE_MANIFEST.tool_id
                or ctx.idempotency_key != self._key or dict(ctx.params) != {"configuration_version": self._version}):
            raise ValidationError("额度检查绑定无效")
        self._prepare_started = True
        await self._authorize()
        digest, version = self._fingerprints.fingerprint((str(self._tenant).encode(), self._version.encode(), self._key.encode()))
        self._prepared = PreparedToolCall(digest, version, {"provider": "hunter", "configuration_version": self._version}, None)
        return self._prepared

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> Mapping[str, SafeScalar]:
        """EXECUTING 持久提交后才惰性读取密钥；撤权或重入不能继续调用。"""
        if tenant_id != self._tenant or prepared is None or prepared is not self._prepared or self._started:
            raise ValidationError("额度检查执行无效")
        self._started = True
        await self._authorize()
        observation = await self._connector.read()
        await self._authorize()
        self.observation = observation
        return {"provider_ref": self._key}
