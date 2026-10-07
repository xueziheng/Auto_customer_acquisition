"""需求域的事件契约声明。

这个文件不定义事件（定义在 ``shared/events/catalog.py``），只声明
**本域发布什么、订阅什么**。价值在于：改动本域前能一眼看到影响面，
不用全库 grep。

每个域都有同名文件，格式一致。
"""

from __future__ import annotations

from shared.events.catalog import (
    DemandSignalCaptured,
    NeedBecameSourcingReady,
    NeedCatalogFactsChanged,
    NeedClusterFormed,
    NeedClusterMembershipChanged,
    NeedHypothesisCreated,
    NeedHypothesisRejected,
    NeedValidated,
    ReplyReceived,
)

PUBLISHES = (
    DemandSignalCaptured,
    NeedHypothesisCreated,
    NeedHypothesisRejected,
    NeedValidated,
    NeedBecameSourcingReady,
    NeedClusterFormed,
    NeedClusterMembershipChanged,
    NeedCatalogFactsChanged,
)
"""本域发布的事件。

``NeedValidated`` 是系统里最重要的事件——它是商业价值的计数依据，
下游（机会评估、需求聚类）都靠它触发。改它的字段要评估所有订阅方。
"""

SUBSCRIBES = (ReplyReceived,)
"""本域订阅的事件。

``ReplyReceived`` —— 客户回复可能包含需求信息。处理逻辑：
1. 找到该会话关联的假设
2. 交给 ``qualification_agent`` 判断回复里有没有可提取的需求字段
3. 若达到验证门槛则晋升，否则补全字段或记录为无需求

处理器必须幂等：同一条回复可能被重复投递（重试、Worker 重启）。
"""
