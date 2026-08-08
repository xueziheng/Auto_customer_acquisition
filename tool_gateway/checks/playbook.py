"""Stage: 公司 Playbook。

排除品类、排除国家、金额底线。数据源是
domains/organization 的 Playbook（经其 service 读取）。

命中排除项的 remediation 恒为 None——Playbook 是老板的边界，
Agent 不该被引导去"补救"它。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class PlaybookCheck:
    name = "playbook"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
