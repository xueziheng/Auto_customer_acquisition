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

QuoteFileErrorCode = Literal[
    "invalid_input", "not_found", "approval_missing", "file_conflict",
    "metadata_mismatch", "workflow_binding_invalid", "template_unsupported",
]


class QuoteFileError(ValidationError):
    """文件固定业务拒绝；仅接受定义过的code，不接受自由错误原文。"""

    def __init__(self, code: QuoteFileErrorCode) -> None:
        if code not in {"invalid_input", "not_found", "approval_missing", "file_conflict",
            "metadata_mismatch", "workflow_binding_invalid", "template_unsupported"}:
            raise ValueError("无效文件错误码")
        self.code = code
        super().__init__(f"报价文件未通过：{code}")


class QuoteFilePermissionError(PermissionDenied):
    """文件用途当前权限拒绝，不包含员工或机会详情。"""

    def __init__(self, code: Literal["permission_denied"]) -> None:
        if code != "permission_denied":
            raise ValueError("无效文件错误码")
        self.code = code
        super().__init__("当前员工无报价文件用途权限")


class QuoteFileUnavailableError(TradeOSError):
    """存储/依赖固定故障；未知提交只能按原artifact恢复。"""

    def __init__(self, code: Literal[
        "dependency_unavailable", "lock_timeout", "storage_unknown", "storage_inconsistent",
    ]) -> None:
        if code not in {"dependency_unavailable", "lock_timeout", "storage_unknown", "storage_inconsistent"}:
            raise ValueError("无效文件错误码")
        self.code = code
        super().__init__(f"报价文件暂不可用：{code}")

QuoteApprovalErrorCode = Literal[
    "invalid_input",
    "quote_contract_invalid",
    "idempotency_conflict",
    "approval_binding_conflict",
    "approval_round_closed",
    "approval_fact_invalid",
    "approval_expired",
    "context_changed",
    "policy_stale",
    "evidence_invalid",
    "evidence_expired",
    "decider_invalid",
    "quote_inactive",
    "workflow_binding_invalid",
]


class QuoteApprovalError(ValidationError):
    """单轮报价审批固定错误，禁止携带业务原文。"""

    def __init__(self, code: QuoteApprovalErrorCode) -> None:
        self.code = code
        super().__init__(f"报价审批未通过：{code}")


class QuoteApprovalPermissionError(PermissionDenied):
    """报价审批当前授权拒绝，不泄露报价或员工详情。"""

    def __init__(self, code: Literal["permission_denied"]) -> None:
        self.code = code
        super().__init__("当前员工无报价审批用途权限")


class QuoteApprovalUnavailableError(TradeOSError):
    """报价审批依赖不可用，未知状态不可转为成功或永久失败。"""

    def __init__(
        self,
        code: Literal[
            "dependency_unavailable",
            "lock_timeout",
            "storage_unknown",
            "storage_inconsistent",
        ],
    ) -> None:
        self.code = code
        super().__init__(f"报价审批暂不可用：{code}")


QuotationErrorCode = Literal[
    "invalid_input",
    "unsupported_term",
    "quote_not_found",
    "issuer_not_found",
    "idempotency_conflict",
    "revision_conflict",
    "active_quote_exists",
    "context_changed",
    "basis_mismatch",
    "scope_stale",
    "evidence_invalid",
    "evidence_expired",
    "quote_expired",
    "approval_missing",
    "receipt_invalid",
    "invalid_state",
]


class QuotationError(ValidationError):
    """报价固定业务错误，不携带来源原文。"""

    def __init__(self, code: QuotationErrorCode) -> None:
        self.code = code
        super().__init__(
            {
                "invalid_input": "报价输入无效",
                "unsupported_term": "报价条款类型不支持",
                "quote_not_found": "报价不存在",
                "issuer_not_found": "缺少已确认抬头",
                "idempotency_conflict": "幂等键绑定冲突",
                "revision_conflict": "报价修订版本冲突",
                "active_quote_exists": "机会已有活跃报价",
                "context_changed": "报价上下文已变化",
                "basis_mismatch": "报价与冻结依据不一致",
                "scope_stale": "人工适用性确认已失效",
                "evidence_invalid": "报价依据无效",
                "evidence_expired": "报价依据已过期",
                "quote_expired": "报价有效期已到",
                "approval_missing": "缺少适用报价批准",
                "receipt_invalid": "发送回执无效",
                "invalid_state": "报价状态不允许此操作",
            }[code]
        )


class QuotationPermissionError(PermissionDenied):
    """当前员工无本次内部用途权限。"""

    def __init__(self, code: Literal["permission_denied"]) -> None:
        self.code = code
        super().__init__("当前员工无报价用途权限")


class QuotationUnavailableError(TradeOSError):
    """持久化失败固定脱敏；未知提交只能原键恢复。"""

    def __init__(
        self,
        code: Literal[
            "dependency_unavailable",
            "lock_timeout",
            "storage_unknown",
            "storage_inconsistent",
        ],
    ) -> None:
        self.code = code
        super().__init__(
            {
                "dependency_unavailable": "报价依赖不可用",
                "lock_timeout": "报价锁等待超时",
                "storage_unknown": "报价存储状态未知",
                "storage_inconsistent": "报价持久记录不一致",
            }[code]
        )


QuoteContextErrorCode = Literal[
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

_CONTEXT_MESSAGES: dict[str, str] = {
    "invalid_input": "报价准备输入无效",
    "context_changed": "报价业务上下文已变化",
    "facts_missing": "报价所需事实不完整",
    "facts_corrupt": "报价事实绑定损坏",
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
    "idempotency_conflict": "幂等键已绑定其他创建意图",
    "operation_pending": "成本表有待恢复创建操作",
    "revision_conflict": "报价修订版本冲突",
    "record_not_found": "报价准备记录不存在",
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

    def __init__(
        self, code: Literal["dependency_unavailable", "lock_timeout", "storage_unknown"]
    ) -> None:
        self.code = code
        super().__init__(
            {
                "dependency_unavailable": "报价准备依赖不可用",
                "lock_timeout": "报价上下文锁等待超时",
                "storage_unknown": "报价上下文存储状态未知",
            }[code]
        )


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
