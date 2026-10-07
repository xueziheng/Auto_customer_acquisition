"""报价域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import ApprovalDecided, QuoteApproved

PUBLISHES = (QuoteApproved,)
"""``QuoteApproved`` 是发送门禁：``tool_gateway`` 执行含报价内容的
发送前，验证对应报价有此事件。没有就拒绝。"""

SUBSCRIBES = (ApprovalDecided,)
"""``ApprovalDecided`` —— 审批结果落到报价状态
（``apply_approval_result``）。幂等：审批事件重复投递不能把
REJECTED 翻回 APPROVED。"""
