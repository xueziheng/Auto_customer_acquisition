"""报价域特有错误。

``ForbiddenCommitmentError`` 与 ``IndicativePriceInQuoteError``
定义在 ``shared.errors``（多个模块共用）。
"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


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
