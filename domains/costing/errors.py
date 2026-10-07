"""成本域特有错误。

注意 ``IndicativePriceInQuoteError`` 定义在 ``shared.errors``——
它同时被本域、quotations 域和 guardrails 使用。
"""

from __future__ import annotations

from typing import Literal

from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TradeOSError,
    ValidationError,
)


class CostSheetNotFoundError(ValidationError):
    """旧get_sheet缺表保持原文本，供新HTTP按类型区分404。"""

    code = "record_not_found"

    def __init__(self) -> None:
        super().__init__("成本表不存在")


class CostingQuoteNotFoundError(ValidationError):
    """新增安全读取的固定缺对象类型，不改变旧确认/冻结错误契约。"""

    code = "record_not_found"

    def __init__(self) -> None:
        super().__init__("成本报价记录不存在")


CostFreezeErrorCode = Literal[
    "invalid_input",
    "context_changed",
    "facts_missing",
    "facts_corrupt",
    "unit_missing",
    "unit_stale",
    "fact_unconfirmed",
    "specification_mismatch",
    "quantity_mismatch",
    "destination_mismatch",
    "coverage_stale",
    "scope_stale",
    "evidence_invalid",
    "evidence_expired",
    "policy_missing",
    "fx_missing",
    "idempotency_conflict",
    "operation_pending",
    "revision_conflict",
    "record_not_found",
]

_FREEZE_MESSAGES = {
    "invalid_input": "成本冻结输入无效",
    "context_changed": "报价业务上下文已变化",
    "facts_missing": "报价所需事实不完整",
    "facts_corrupt": "成本冻结事实绑定损坏",
    "unit_missing": "客户数量单位缺失",
    "unit_stale": "客户数量单位绑定已失效",
    "fact_unconfirmed": "报价事实尚未人工确认",
    "specification_mismatch": "报价规格与需求不一致",
    "quantity_mismatch": "报价数量与需求不一致",
    "destination_mismatch": "报价目的地与需求不一致",
    "coverage_stale": "成本完整性清单已失效",
    "scope_stale": "成本适用性确认已失效",
    "evidence_invalid": "报价依据无效",
    "evidence_expired": "报价依据已过期",
    "policy_missing": "缺少已确认报价政策",
    "fx_missing": "缺少适用报价汇率",
    "idempotency_conflict": "幂等键已绑定其他意图",
    "operation_pending": "成本表有待恢复创建操作",
    "revision_conflict": "报价修订版本冲突",
    "record_not_found": "成本冻结记录不存在",
}


class CostFreezeError(ValidationError):
    """固定错误码，不附SQL或原始商业内容。"""

    def __init__(self, code: CostFreezeErrorCode) -> None:
        self.code = code
        super().__init__(_FREEZE_MESSAGES[code])


class CostFreezePermissionError(PermissionDenied):
    """当前成本权限不足。"""

    def __init__(self, code: Literal["permission_denied"]) -> None:
        self.code = code
        super().__init__("当前员工没有成本冻结权限")


class CostFreezeUnavailableError(TradeOSError):
    """未知存储结果只允许原键恢复，不自动解锁或换键。"""

    def __init__(
        self, code: Literal["dependency_unavailable", "lock_timeout", "storage_unknown"]
    ) -> None:
        self.code = code
        super().__init__(
            {
                "dependency_unavailable": "成本冻结依赖不可用",
                "lock_timeout": "成本冻结锁等待超时",
                "storage_unknown": "成本冻结存储状态未知",
            }[code]
        )


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


class InvalidPricingEvidenceError(ValidationError):
    """可信来源、逐字段确认或适用范围不完整，必须人工补证。"""


class CostCoverageConflict(ValidationError):
    """确认清单对应的成本内容已改变，不允许把旧结论贴到新成本上。"""
