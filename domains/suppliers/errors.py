"""供应商域特有错误。"""

from __future__ import annotations

from shared.errors import ValidationError


class QuotedPriceWithoutEvidenceError(ValidationError):
    """quoted 价格缺少报价证据。

    没有证据的 quoted 等于把参考价洗白成可承诺价——
    硬边界 7 会在报价环节失效。
    """
