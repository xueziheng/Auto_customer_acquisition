"""成本域特有错误。

注意 ``IndicativePriceInQuoteError`` 定义在 ``shared.errors``——
它同时被本域、quotations 域和 guardrails 使用。
"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


class LockedCostSheetError(PolicyViolation):
    """试图修改已锁定的成本表。

    锁定的成本表是历史报价的依据。供应商改价就开新版本，
    改旧版本等于篡改「我们当时是怎么算的」。
    """


class EmptyCostSheetError(ValidationError):
    """成本项为空时试图计算或锁定。

    零成本的报价是事故，不是边界情况。
    """


class MissingFxSnapshotError(ValidationError):
    """QUOTED 版本没有绑定汇率快照。

    没有快照意味着历史报价的金额会随今天的汇率漂移。
    """


class MarginBelowFloorError(PolicyViolation):
    """拟议价格低于利润底线。

    不是禁止——低利润单可能有战略价值——而是必须走审批，
    且审批人不能是机会负责人。错误消息带上实际利润率和底线。
    """


class UnconfirmedModelValueError(PolicyViolation):
    """模型建议的成本值未经人工确认就试图参与计算。

    模型可以提建议（「你可能漏了报关费」），但进入计算的每个数字
    都要有人确认过（硬边界 2 的延伸）。
    """
