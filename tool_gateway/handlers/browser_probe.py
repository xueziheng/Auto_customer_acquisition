"""UNGM 登录检查插件，不修改 Gateway 核心管线。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from shared.errors import ValidationError
from shared.schemas.browser_probe import BrowserProbeConnector, BrowserProbeObservation
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, SafeScalar, ToolCallContext

BROWSER_PROBE_MANIFEST = ToolManifest(
    tool_id="browser.ungm.login.check", version="v1", description="恢复专用 UNGM 会话并检查登录",
    risk_level=RiskLevel.LOW, cost_class=CostClass.FREE, requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED, required_permissions=("platform:configure",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={"type": "object", "properties": {"configuration_version": {"type": "string"}}, "required": ("configuration_version",), "additionalProperties": False},
    output_schema={"type": "object", "properties": {"provider_ref": {"type": "string"}}, "required": ("provider_ref",), "additionalProperties": False},
)


class BrowserProbeHandler:
    """固定租户、操作者和部署绑定，结果仅在审计成功后释放。"""
    def __init__(
        self, tenant: TenantId, actor: str, version: str, key: str,
        authorize: Callable[[], Awaitable[None]], connector: BrowserProbeConnector,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        self._tenant, self._actor, self._version, self._key = tenant, actor, version, key
        self._authorize, self._connector, self._fingerprints = authorize, connector, fingerprints
        self._prepared: PreparedToolCall | None = None
        self._prepare_started = False
        self._started = False
        self.observation: BrowserProbeObservation | None = None

    async def prepare(self, ctx: ToolCallContext, preflight: object | None) -> PreparedToolCall:
        if (self._prepare_started or preflight is not None or ctx.tenant_id != self._tenant
                or str(ctx.user_id) != self._actor or ctx.tool_id != BROWSER_PROBE_MANIFEST.tool_id
                or ctx.idempotency_key != self._key or dict(ctx.params) != {"configuration_version": self._version}):
            raise ValidationError("浏览器检查绑定无效")
        self._prepare_started = True
        await self._authorize()
        digest, version = self._fingerprints.fingerprint((str(self._tenant).encode(), self._version.encode(), self._key.encode()))
        self._prepared = PreparedToolCall(digest, version, {"provider": "ungm", "configuration_version": self._version}, None)
        return self._prepared

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> Mapping[str, SafeScalar]:
        if tenant_id != self._tenant or prepared is not self._prepared or self._started:
            raise ValidationError("浏览器检查执行无效")
        self._started = True
        await self._authorize()
        observation = await self._connector.check()
        await self._authorize()
        self.observation = observation
        return {"provider_ref": self._key}
