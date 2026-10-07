"""承诺域特有错误。"""

from __future__ import annotations

from shared.errors import ValidationError


class RelativeDueTimeError(ValidationError):
    """承诺的到期时间仍是相对表述（"tomorrow"、"Friday"）。

    必须在提取时用消息时间戳 + 客户时区解析为绝对时间——
    存 "tomorrow" 一周后毫无意义。解析没把握就置
    due_at_uncertain 让员工确认，不要猜。
    """
