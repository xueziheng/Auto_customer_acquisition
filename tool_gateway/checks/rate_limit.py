"""Stage: 频率与配额。

三层限额取最小：
1. 发件身份日限额（sending_identity 的 SendPermission，含预热）
2. Campaign 日限额（outreach 的 QuotaRepository）
3. 工具级全局频率（防 Agent 死循环刷调用）

放最后是因为它最"贵"（要查多个计数器），且幂等命中不该消耗限额。
remediation 带上"什么时候再试"。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class RateLimitCheck:
    name = "rate_limit"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
