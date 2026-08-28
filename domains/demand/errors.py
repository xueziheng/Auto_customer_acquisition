"""需求域特有错误。"""

from __future__ import annotations

from domains.demand.schemas import NeedUnitErrorCode
from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TradeOSError,
    ValidationError,
)

_UNIT_MESSAGES: dict[NeedUnitErrorCode, str] = {
    "invalid_input": "单位确认输入不合法", "unit_unspecified": "客户单位口径尚未明确",
    "quantity_invalid": "数量必须为正整数", "source_mismatch": "客户来源与当前数量单位不匹配",
    "source_unsupported": "不支持此客户来源", "need_not_found": "需求不存在",
    "confirmation_not_found": "单位确认记录不存在", "need_terminal": "需求已结束，不能确认单位",
    "quantity_changed": "数量或数量来源已变化，请刷新事实", "unit_changed": "单位已被其他确认更新",
    "idempotency_conflict": "幂等键已用于不同请求", "unit_missing": "缺少客户单位确认",
    "unit_stale": "单位与当前数量来源绑定已失效", "fact_unconfirmed": "事实尚未人工确认",
    "permission_denied": "当前无权访问需求或客户来源", "source_unavailable": "客户来源暂不可用",
    "dependency_unavailable": "单位确认依赖不可用", "lock_timeout": "事实锁等待超时",
    "storage_unknown": "存储结果不确定，请用原幂等键核对", "facts_corrupt": "需求事实存储损坏",
}


class NeedUnitError(ValidationError):
    """单位输入/状态错误，固定code，不自动重试。"""

    def __init__(self, code: NeedUnitErrorCode) -> None:
        """只接受固定代码，禁止把原始异常作为消息。"""
        self.code = code
        super().__init__(_UNIT_MESSAGES[code])


class NeedUnitPermissionError(PermissionDenied):
    """需求或来源权限拒绝，不泄露原文。"""

    def __init__(self, code: NeedUnitErrorCode) -> None:
        """只接受固定代码。"""
        self.code = code
        super().__init__(_UNIT_MESSAGES[code])


class NeedUnitUnavailableError(TradeOSError):
    """来源/存储不可用或提交结果不确定，不自动重试。"""

    def __init__(self, code: NeedUnitErrorCode) -> None:
        """只接受固定代码，提交未知不声明未写入。"""
        self.code = code
        super().__init__(_UNIT_MESSAGES[code])


class InsufficientEvidenceError(PolicyViolation):
    """证据不足，不能晋升为已验证需求。

    **本域最重要的错误。** 它守着整个系统的核心门槛：只有客户本人
    确认过的需求才算已验证。

    错误消息必须说清：当前最高证据等级是什么、要求是什么、
    还需要什么样的证据。写"证据不足"没用——调用方（可能是 Agent）
    需要知道下一步该做什么。

    不要为这个错误提供绕过参数。
    """


class MissingWebEvidenceError(ValidationError):
    """网页来源的信号缺少 URL、page_hash 或不可变 snapshot artifact。

    哈希只能证明内容值；没有不可变快照引用，页面变更后仍无法取回当时内容，
    因而证据链不完整。
    """


class SourcingThresholdNotMetError(PolicyViolation):
    """完整度不足，不能进寻源。

    带上 ``missing_fields``，让调用方知道该向客户追问什么。
    信息不足就去寻源会拿到供应商无法报价的模糊询问，还消耗
    与供应商之间的信誉。
    """


class HypothesisAlreadyResolvedError(PolicyViolation):
    """假设已经是 validated 或 rejected 终态，不能再改。"""
