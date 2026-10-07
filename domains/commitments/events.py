"""承诺域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import CommitmentCreated, CommitmentOverdue

PUBLISHES = (CommitmentCreated, CommitmentOverdue)
"""``CommitmentOverdue`` 的订阅方是 ``notification_gateway``：
员工承诺逾期通知本人/经理，客户承诺到期通知负责员工。"""

SUBSCRIBES = ()
