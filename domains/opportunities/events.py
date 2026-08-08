"""贸易机会域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import (
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
    NeedValidated,
    OpportunityLost,
    OpportunityQualified,
    QuoteApproved,
    SourcingCaseCompleted,
)

PUBLISHES = (
    OpportunityQualified,
    OpportunityLost,
    HandoffRequested,
    HandoffAccepted,
    HandoffQueueBacklogged,
)
"""本域发布的事件。

``OpportunityLost`` 携带 ``loss_reason`` 与 ``died_at_state``，
是反馈闭环的数据源。

``HandoffRequested`` 触发通知；SLA 计时从这个事件开始，到
``HandoffAccepted`` 结束。
"""

SUBSCRIBES = (NeedValidated, SourcingCaseCompleted, QuoteApproved)
"""本域订阅的事件。

``NeedValidated`` —— 评估硬门槛，通过则创建机会。未通过是正常结果，
记录被拦原因即可，不要抛异常。

``SourcingCaseCompleted`` —— 更新 ``can_source`` 与 ``estimated_cost``，
从 sourcing 推进。寻源失败时按 ``NO_SUPPLY_FOUND`` 终结。

``QuoteApproved`` —— 转入 quoted 状态。

处理器必须幂等。
"""
