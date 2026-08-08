"""员工域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import DirectiveActivated

PUBLISHES = ()

SUBSCRIBES = (DirectiveActivated,)
"""市场分配段 → Territory Matrix 更新（``apply_territory_from_directive``）。
只影响未来分配，不回溯改已有归属锁。"""
