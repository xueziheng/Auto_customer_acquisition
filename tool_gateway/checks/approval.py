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

from domains.quotations.service import contains_forbidden_commitment
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class ApprovalCheck:
    name = "approval"

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if getattr(state.manifest, "requires_approval", True):
            return CheckRejection(
                self.name,
                "approval:required",
                "此工具调用需要逐次人工审批",
                "提交审批后重试",
            )
        subject = ctx.params.get("subject")
        body = ctx.params.get("body")
        if not isinstance(subject, str) or not isinstance(body, str):
            return CheckRejection(
                self.name,
                "approval:material_invalid",
                "客户可见内容无效",
            )
        if contains_forbidden_commitment(subject) or contains_forbidden_commitment(body):
            return CheckRejection(
                self.name,
                "approval:commercial_commitment",
                "客户内容包含必须逐次审批的承诺",
                "移除承诺或提交审批后重试",
            )
        return None
