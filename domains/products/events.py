"""产品域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import SourcingCandidatesVerified

PUBLISHES = ()

SUBSCRIBES = (SourcingCandidatesVerified,)
"""封存的精确候选 generation → source_only 产品卡。

``SourcingCandidatesReady`` 是卡与 Option 全部完成后的最终事实，不是建卡
请求；同 Case+Candidate 的重复 Verified 投递由产品来源键幂等复用。
"""
