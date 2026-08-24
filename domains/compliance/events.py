"""合规域发布的 durable 事件声明。"""

from __future__ import annotations

from shared.events.catalog import CountryPolicyVersionProposed

PUBLISHES = (CountryPolicyVersionProposed,)
SUBSCRIBES: tuple[type[object], ...] = ()

__all__ = ("PUBLISHES", "SUBSCRIBES")
