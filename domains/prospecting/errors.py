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
