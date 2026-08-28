"""报价域特有错误。

``ForbiddenCommitmentError`` 与 ``IndicativePriceInQuoteError``
定义在 ``shared.errors``（多个模块共用）。
"""

from __future__ import annotations

from typing import Literal

from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TradeOSError,
    ValidationError,
)

QuoteContextErrorCode = Literal[
    "invalid_input", "context_changed", "facts_missing", "facts_corrupt", "unit_missing",
    "unit_stale", "fact_unconfirmed", "specification_mismatch", "quantity_mismatch",
    "destination_mismatch", "coverage_stale", "scope_stale", "evidence_invalid",
    "evidence_expired", "policy_missing", "fx_missing", "idempotency_conflict",
    "operation_pending", "revision_conflict", "record_not_found",
]

_CONTEXT_MESSAGES: dict[str, str] = {
    "invalid_input": "报价准备输入无效", "context_changed": "报价业务上下文已变化",
    "facts_missing": "报价所需事实不完整", "facts_corrupt": "报价事实绑定损坏",
    "unit_missing": "客户数量单位缺失", "unit_stale": "客户数量单位绑定已失效",
    "fact_unconfirmed": "报价事实尚未人工确认", "specification_mismatch": "报价规格与需求不一致",
    "quantity_mismatch": "报价数量与需求不一致", "destination_mismatch": "报价目的地与需求不一致",
    "coverage_stale": "成本完整性清单已失效", "scope_stale": "成本适用性确认已失效",
    "evidence_invalid": "报价依据无效", "evidence_expired": "报价依据已过期",
    "policy_missing": "缺少已确认报价政策", "fx_missing": "缺少适用报价汇率",
    "idempotency_conflict": "幂等键已绑定其他创建意图", "operation_pending": "成本表有待恢复创建操作",
    "revision_conflict": "报价修订版本冲突", "record_not_found": "报价准备记录不存在",
}


class QuoteContextError(ValidationError):
    """固定错误码，不携带敏感原文或数据库信息。"""

    def __init__(self, code: QuoteContextErrorCode) -> None:
        self.code = code
        super().__init__(_CONTEXT_MESSAGES[code])


class QuoteContextPermissionError(PermissionDenied):
    """报价准备用途拒绝，不授予通用CRM或原件权限。"""

    def __init__(self, code: Literal["permission_denied"]) -> None:
        self.code = code
        super().__init__("当前员工没有报价准备用途权限")


class QuoteContextUnavailableError(TradeOSError):
    """依赖和存储失败固定脱敏。"""

    def __init__(self, code: Literal["dependency_unavailable", "lock_timeout", "storage_unknown"]) -> None:
        self.code = code
        super().__init__({"dependency_unavailable": "报价准备依赖不可用",
            "lock_timeout": "报价上下文锁等待超时", "storage_unknown": "报价上下文存储状态未知"}[code])


class ApprovalSkipError(PolicyViolation):
    """试图跳过审批发送报价。

    状态机里 DRAFT 到 SENT 没有路径，这个错误是那条规则的运行时形式。
    """


class MissingValidityWindowError(ValidationError):
    """报价缺少有效期。

    没有有效期的报价是开放式承诺：三个月后客户拿旧价下单时，
    汇率和供应商价格早变了。
    """


class SentQuoteImmutableError(PolicyViolation):
    """试图修改已发送的报价。

    客户手里的版本不能在我们这边被改掉——改了审计链就断了。
    修改 = 创建新版本。
    """


class UnlockedCostSheetError(PolicyViolation):
    """成本表未通过 ``lock_for_quote`` 就试图创建报价。

    锁定检查包含 INDICATIVE 门禁和汇率快照校验，跳过它等于
    跳过硬边界 7。
    """


class ConcurrentActiveQuoteError(PolicyViolation):
    """同一机会已存在活跃报价。

    客户手里有两份不同的有效报价是谈判事故。要么等旧的出结果，
    要么显式让新版本替代旧版本。
    """
