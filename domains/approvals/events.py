"""审批域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import ApprovalDecided

PUBLISHES = (ApprovalDecided,)
"""``ApprovalDecided`` 的订阅方：
- ``domains/quotations``（报价状态落地）
- ``agent_runtime``（Change Set 应用或丢弃）
- ``notification_gateway``（通知提议人结果）

订阅方应用变更前必须先调 ``mark_applied`` 拿幂等确认——
事件重复投递 + 直接应用 = 双重应用。
"""

SUBSCRIBES = ()
