"""conversations 域错误。"""

from __future__ import annotations

from shared.errors import ValidationError


class ReingestConflictError(ValidationError):
    """同 (tenant, external_message_id) 但语义不一致：拒绝重复入库。

    固定安全摘要，不回显正文/凭证/引用内容；调用方按不可重试处理。
    """
