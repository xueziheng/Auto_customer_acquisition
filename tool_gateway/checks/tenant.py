"""Stage: 租户一致性（硬边界 8）。

检查调用上下文的 tenant_id 与参数中引用的所有实体归属一致。
不一致不是普通拒绝——抛 TenantIsolationViolation 级审计告警：
它意味着上游某处漏了租户过滤，同类漏洞可能还有。
"""

from __future__ import annotations

import re

from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState

_ATTEMPT_RE = re.compile(r"mat_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_CAMPAIGN_RE = re.compile(r"cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}")


class TenantCheck:
    name = "tenant"

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        attempt_id = ctx.params.get("attempt_id")
        if (
            not isinstance(attempt_id, str)
            or _ATTEMPT_RE.fullmatch(attempt_id) is None
            or not isinstance(ctx.campaign_ref, str)
            or _CAMPAIGN_RE.fullmatch(ctx.campaign_ref) is None
        ):
            return CheckRejection(
                self.name,
                "tenant:resource_binding",
                "工具资源租户绑定无效",
            )
        return None
