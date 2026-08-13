"""邮件投递反馈跨层共享词表与 provider-neutral 只读 DTO。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from shared.errors import ValidationError

_LOWER_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_ROUTE_RE = re.compile(r"[a-z0-9-]{1,32}")
_MESSAGE_ID_RE = re.compile(
    r"(?:[a-z0-9-]{1,32}\.)?[0-9a-f]{64}@messages\.tradeos\.invalid"
)
_IDEMPOTENCY_HEADER_RE = re.compile(
    r"(?:[a-z0-9-]{1,32}\.)?[0-9a-f]{64}"
)
_SECRET_MARKERS = ("authorization", "bearer", "cookie", "password", "secret", "token")


def _is_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _safe_cursor(value: object, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32768
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
        or any(marker in value.casefold() for marker in _SECRET_MARKERS)
    ):
        raise ValidationError("feedback cursor 无效")
    return value


class EmailFeedbackKind(str, Enum):
    """Provider-neutral 投递反馈类别。"""

    HARD_BOUNCE = "hard_bounce"
    SOFT_BOUNCE = "soft_bounce"
    UNPARSEABLE = "unparseable"


class EmailFeedbackParseIssue(str, Enum):
    """解析失败的固定安全分类。"""

    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"


class EmailFeedbackResult(str, Enum):
    """单条反馈的持久化处理结果。"""

    APPLIED = "applied"
    RECORDED = "recorded"
    QUARANTINED = "quarantined"


class EmailFeedbackQuarantineReason(str, Enum):
    """确定性隔离原因；不得包含 provider 原文。"""

    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    MISSING_CORRELATION = "missing-correlation"
    AMBIGUOUS_CORRELATION = "ambiguous-correlation"
    CROSS_TENANT_CORRELATION = "cross-tenant-correlation"


@dataclass(frozen=True, repr=False)
class EmailFeedbackCorrelation:
    """只保留 TradeOS 自有的两个关联 header 与可选 route。"""

    route_id: str | None
    deterministic_message_id: str | None = field(repr=False)
    idempotency_header: str | None = field(repr=False)

    def __post_init__(self) -> None:
        if self.route_id is not None and (
            not isinstance(self.route_id, str)
            or _ROUTE_RE.fullmatch(self.route_id) is None
        ):
            raise ValidationError("feedback correlation 无效")
        pair = (self.deterministic_message_id, self.idempotency_header)
        if pair == (None, None):
            raise ValidationError("feedback correlation 无效")
        if pair[0] is not None and (
            not isinstance(pair[0], str)
            or _MESSAGE_ID_RE.fullmatch(pair[0]) is None
        ):
            raise ValidationError("feedback correlation 无效")
        if pair[1] is not None and (
            not isinstance(pair[1], str)
            or _IDEMPOTENCY_HEADER_RE.fullmatch(pair[1]) is None
        ):
            raise ValidationError("feedback correlation 无效")
        routes: set[str | None] = set()
        if isinstance(pair[0], str):
            message_local = pair[0].removesuffix("@messages.tradeos.invalid")
            routes.add(
                message_local.split(".", 1)[0] if "." in message_local else None
            )
        if isinstance(pair[1], str):
            routes.add(pair[1].split(".", 1)[0] if "." in pair[1] else None)
        if len(routes) != 1 or next(iter(routes)) != self.route_id:
            raise ValidationError("feedback correlation 无效")


@dataclass(frozen=True, repr=False)
class EmailFeedbackItem:
    """单个 DSN recipient block 的安全事实。"""

    provider_event_id: str
    provider_ref_digest: str
    ordinal: int
    kind: EmailFeedbackKind
    occurred_at: datetime
    correlation: EmailFeedbackCorrelation | None = field(repr=False)
    parse_issue: EmailFeedbackParseIssue | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider_event_id, str)
            or _LOWER_HEX_64_RE.fullmatch(self.provider_event_id) is None
            or not isinstance(self.provider_ref_digest, str)
            or _LOWER_HEX_64_RE.fullmatch(self.provider_ref_digest) is None
            or not isinstance(self.ordinal, int)
            or isinstance(self.ordinal, bool)
            or not 0 <= self.ordinal <= 99
            or not isinstance(self.kind, EmailFeedbackKind)
            or not _is_utc(self.occurred_at)
            or (
                self.correlation is not None
                and not isinstance(self.correlation, EmailFeedbackCorrelation)
            )
            or (
                self.parse_issue is not None
                and not isinstance(self.parse_issue, EmailFeedbackParseIssue)
            )
        ):
            raise ValidationError("feedback item 无效")
        unparseable = self.kind is EmailFeedbackKind.UNPARSEABLE
        if unparseable != (self.parse_issue is not None):
            raise ValidationError("feedback item 形态无效")
        if unparseable and self.correlation is not None:
            raise ValidationError("feedback item 形态无效")
        if not unparseable and self.correlation is None:
            raise ValidationError("feedback item 形态无效")


@dataclass(frozen=True, repr=False)
class EmailFeedbackPage:
    """有界不可变的一页 typed feedback。"""

    starting_cursor: str | None = field(repr=False)
    next_cursor: str = field(repr=False)
    items: tuple[EmailFeedbackItem, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _safe_cursor(self.starting_cursor, nullable=True)
        _safe_cursor(self.next_cursor, nullable=False)
        if (
            not isinstance(self.items, tuple)
            or len(self.items) > 100
            or not all(isinstance(item, EmailFeedbackItem) for item in self.items)
        ):
            raise ValidationError("feedback page 无效")
        event_ids = {item.provider_event_id for item in self.items}
        event_keys = {(item.provider_ref_digest, item.ordinal) for item in self.items}
        if len(event_ids) != len(self.items) or len(event_keys) != len(self.items):
            raise ValidationError("feedback page 包含重复 item")
