"""产品域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import SourcingCaseCompleted

PUBLISHES = ()

SUBSCRIBES = (SourcingCaseCompleted,)
"""合格候选 → 候选产品卡（source_only 状态）。幂等：同一案例
重复投递不重复建卡。"""
