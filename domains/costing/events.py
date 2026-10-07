"""成本域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import SourcingCaseCompleted

PUBLISHES = ()
"""本域不直接发布事件。

成本变化的业务意义（可以报价了、利润过低）由 quotations 和
approvals 域的事件表达。成本表本身的增改是内部状态。
"""

SUBSCRIBES = (SourcingCaseCompleted,)
"""``SourcingCaseCompleted`` —— 用寻源候选的价格创建 ESTIMATED
版本成本表，让机会负责人第一时间看到「大概能不能做」。

注意候选价格 basis 是 INDICATIVE，所以这张表天然进不了报价——
这正是流程想要的：先估算判断方向，供应商实报价后再开 QUOTED 版本。
"""
