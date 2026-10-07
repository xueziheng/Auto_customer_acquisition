"""潜客域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import (
    AccountCountryFactsChanged,
    ContactPointVerified,
    ProspectAccountQualified,
)

PUBLISHES = (
    ContactPointVerified,
    ProspectAccountQualified,
    AccountCountryFactsChanged,
)
"""``ContactPointVerified`` 是 outreach 入组的前提（硬边界 6）。"""

SUBSCRIBES = ()
