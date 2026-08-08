"""Stage: 审批状态。

三层判断：
1. manifest.requires_approval 为 True → ctx.approval_ref 必须指向
   一条 APPROVED 且未过期的审批
2. 工具属发送类且内容含禁止自动承诺（quotations 的
   contains_forbidden_commitment）→ 即使在 Campaign 边界内也要审批
3. requires_approval 为 False 且无承诺内容 → 通过

remediation 给出提交审批的路径——这是少数可补救的拒绝。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class ApprovalCheck:
    name = "approval"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
