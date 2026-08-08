"""Stage: 租户一致性（硬边界 8）。

检查调用上下文的 tenant_id 与参数中引用的所有实体归属一致。
不一致不是普通拒绝——抛 TenantIsolationViolation 级审计告警：
它意味着上游某处漏了租户过滤，同类漏洞可能还有。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class TenantCheck:
    name = "tenant"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
