"""需求域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


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
    """网页来源的信号缺少 URL 或 page_hash。

    没有哈希的网页证据在页面变更后无法自证当时看到了什么，
    等于没有证据。
    """


class SourcingThresholdNotMetError(PolicyViolation):
    """完整度不足，不能进寻源。

    带上 ``missing_fields``，让调用方知道该向客户追问什么。
    信息不足就去寻源会拿到供应商无法报价的模糊询问，还消耗
    与供应商之间的信誉。
    """


class HypothesisAlreadyResolvedError(PolicyViolation):
    """假设已经是 validated 或 rejected 终态，不能再改。"""
