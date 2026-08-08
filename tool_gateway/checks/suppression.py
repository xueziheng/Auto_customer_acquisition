"""Stage: 抑制名单。

联系人级和企业级都查（经 domains/outreach 的 service）。

**服务不可用时拒绝发送，不是放行**——宁可晚发一封，
不能发给退订过的人。remediation 恒为 None。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class SuppressionCheck:
    name = "suppression"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
