"""Stage: 国家政策包。

按目标国家查合规要求（冷邮件是否允许、退订要求、处理依据要求）。
政策包是数据不是代码（docs/architecture/08-compliance.md）——
规则会变，新市场会加。

政策包里没有的国家默认拒绝，不默认放行。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class CountryPolicyCheck:
    name = "country_policy"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
