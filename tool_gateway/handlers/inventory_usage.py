"""免费搜索冷启动用量观察；不执行搜索、不预留或刷新已有额度。"""

from collections.abc import Awaitable, Callable
from dataclasses import replace

from connectors.search_contracts import SearchUsage
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.free_search import MANIFEST, FreeSearchReaderFactory
from tool_gateway.pipeline import PreparedToolCall, ToolCallContext

USAGE_MANIFEST = replace(
    MANIFEST,
    tool_id="inventory.search_usage",
    version="v1",
    description="观察免费搜索用量；未知或付费状态不会授权搜索",
    checks=("tenant", "permission", "rate_limit"),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    redact_fields=(),
)


class InventoryUsageHandler:
    def __init__(
        self,
        factory: FreeSearchReaderFactory,
        fingerprints: HmacFingerprintProvider,
        observe: Callable[[SearchUsage], Awaitable[None]],
    ) -> None:
        self.factory, self.fingerprints, self.observe = factory, fingerprints, observe

    async def prepare(
        self, ctx: ToolCallContext, preflight: object
    ) -> PreparedToolCall:
        if ctx.params or ctx.tenant_id != self.factory.tenant_id or ctx.run_id is None:
            raise ValueError("免费用量任务绑定无效")
        fingerprint, version = self.fingerprints.fingerprint(
            (str(ctx.tenant_id).encode(), str(ctx.run_id).encode())
        )
        return PreparedToolCall(fingerprint, version, {}, ctx.tenant_id)

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall):
        if prepared.payload != tenant_id or tenant_id != self.factory.tenant_id:
            raise ValueError("免费用量租户无效")
        connector = self.factory.connector_factory()
        await connector.configure(self.factory.secret_resolver)
        usage = await connector.usage()
        await self.observe(usage)
        return {"provider_ref": "inventory-usage-observed"}
