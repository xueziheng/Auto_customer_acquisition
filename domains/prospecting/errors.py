"""潜客域特有错误。

``UnverifiedContactError`` 定义在 ``shared.errors``（outreach 与
tool_gateway 也用）。
"""

from __future__ import annotations

from shared.errors import ValidationError


class MissingLegalBasisError(ValidationError):
    """联系方式缺少处理依据。

    入库即处理，处理必须有依据。GDPR 问询时「我们以为可以」
    不是答案——现在留痕的成本几乎为零，事后追溯每个联系人的
    来源成本极高。
    """


class MissingAssessmentRefError(ValidationError):
    """依据是 legitimate_interest 但没有评估引用。

    依赖正当利益的前提是能拿出评估记录，不是假设自己有。
    """


class ProspectingConflictError(ValidationError):
    """同一唯一身份对应不同业务语义，拒绝静默覆盖。"""


class ProspectAccountNotFoundError(ValidationError):
    """潜在企业在当前租户不可见。"""


class ProspectContactNotFoundError(ValidationError):
    """潜在联系人在当前租户不可见。"""


class ContactPointNotFoundError(ValidationError):
    """联系方式在当前租户不可见。"""


class ErasedContactPointError(ValidationError):
    """该联系方式已响应删除/反对处理请求，不得再次采集。"""
