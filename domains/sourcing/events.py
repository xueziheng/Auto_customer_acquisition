"""寻源域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import (
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseCompleted,
    SourcingCaseHandedToCosting,
    SourcingCaseOpened,
)

PUBLISHES = (
    SourcingCaseOpened,
    SourcingCaseCompleted,
    SourcingCandidatesVerified,
    SourcingCandidatesReady,
    SourcingCaseHandedToCosting,
)
"""``SourcingCaseCompleted`` 的订阅方：
- ``domains/costing``（起 ESTIMATED 成本表）
- ``domains/opportunities``（更新 can_source）

案例失败不发这个事件——失败通过 ``fail_case`` 的返回路径由上层
落到机会的 NO_SUPPLY_FOUND。
"""

SUBSCRIBES = ()
"""案例的启动由工作流（``workflows/sourcing_case``）驱动，
不直接订阅事件——寻源是否启动取决于需求完整度和人的决定，
不是需求验证的自动后果。"""
