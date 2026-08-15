"""Gmail RFC 3464 DSN 的保守解析器；原始 MIME 只在调用栈内短暂存在。"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from email.utils import parsedate_to_datetime

from shared.errors import ValidationError
from shared.schemas.email_feedback import (
    EmailFeedbackCorrelation,
    EmailFeedbackItem,
    EmailFeedbackKind,
    EmailFeedbackParseIssue,
)

_MAX_MIME_BYTES = 4 * 1024 * 1024

MAX_FEEDBACK_MIME_BYTES = _MAX_MIME_BYTES
"""反馈 MIME 上限的公开别名：client 分派在把 raw 交给本解析器前先按此定界，
避免超大非 feedback-report 输入在这里被输入校验抛 ValidationError。"""
_PROVIDER_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_STATUS_RE = re.compile(r"([245])\.\d{1,3}\.\d{1,3}")
_MESSAGE_ID_RE = re.compile(
    r"(?:[a-z0-9-]{1,32}\.)?[0-9a-f]{64}@messages\.tradeos\.invalid"
)
_HEADER_RE = re.compile(r"(?:[a-z0-9-]{1,32}\.)?[0-9a-f]{64}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def provider_ref_digest(provider_message_ref: str) -> str:
    """provider ref 的安全 lower-hex 摘要；后续持久化只用摘要。"""
    if (
        not isinstance(provider_message_ref, str)
        or _PROVIDER_REF_RE.fullmatch(provider_message_ref) is None
    ):
        raise ValidationError("Gmail provider ref 无效")
    return _digest(provider_message_ref)


def message_occurred_at(raw_message: bytes) -> datetime:
    """解析顶层 Date 为 UTC；缺失/不可解析返回 EPOCH（与 DSN 解析一致）。"""
    if not isinstance(raw_message, bytes):
        raise ValidationError("Gmail feedback 输入无效")
    message = BytesParser(policy=default).parsebytes(raw_message)
    return _occurred_at(message)


def _occurred_at(message: Message) -> datetime:
    value = message.get("Date")
    if not isinstance(value, str):
        return _EPOCH
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return _EPOCH
    if parsed.tzinfo is None:
        return _EPOCH
    return parsed.astimezone(UTC)


def _unparseable(
    provider_ref: str,
    ref_digest: str,
    occurred_at: datetime,
    issue: EmailFeedbackParseIssue,
    *,
    ordinal: int = 0,
) -> EmailFeedbackItem:
    return EmailFeedbackItem(
        provider_event_id=_digest(f"{provider_ref}:{ordinal}"),
        provider_ref_digest=ref_digest,
        ordinal=ordinal,
        kind=EmailFeedbackKind.UNPARSEABLE,
        occurred_at=occurred_at,
        correlation=None,
        parse_issue=issue,
    )


def original_headers(message: Message) -> EmailFeedbackCorrelation | None:
    """从唯一的 message/rfc822 部分提取 TradeOS 自有关联键；缺失/歧义返回 None。"""
    originals = [
        part for part in message.walk() if part.get_content_type() == "message/rfc822"
    ]
    if len(originals) != 1:
        return None
    payload = originals[0].get_payload()
    if not isinstance(payload, list) or len(payload) != 1:
        return None
    original = payload[0]
    if not isinstance(original, Message):
        return None
    message_ids = original.get_all("Message-ID", [])
    headers = original.get_all("X-TradeOS-Idempotency-V1", [])
    if len(message_ids) > 1 or len(headers) > 1 or (not message_ids and not headers):
        return None
    message_id = str(message_ids[0]).strip() if message_ids else None
    if isinstance(message_id, str) and message_id.startswith("<") and message_id.endswith(">"):
        message_id = message_id[1:-1]
    header = str(headers[0]).strip() if headers else None
    if (
        message_id is not None and _MESSAGE_ID_RE.fullmatch(message_id) is None
    ) or (header is not None and _HEADER_RE.fullmatch(header) is None):
        return None
    routes: set[str | None] = set()
    if message_id is not None:
        message_local = message_id.removesuffix("@messages.tradeos.invalid")
        routes.add(message_local.split(".", 1)[0] if "." in message_local else None)
    if header is not None:
        routes.add(header.split(".", 1)[0] if "." in header else None)
    if len(routes) != 1:
        return None
    return EmailFeedbackCorrelation(next(iter(routes)), message_id, header)


def _delivery_blocks(message: Message) -> list[Message]:
    parts = [
        part
        for part in message.walk()
        if part.get_content_type() == "message/delivery-status"
    ]
    if len(parts) != 1:
        return []
    payload = parts[0].get_payload()
    if not isinstance(payload, list):
        return []
    return [
        block
        for block in payload
        if isinstance(block, Message)
        and (
            block.get_all("Status")
            or block.get_all("Final-Recipient")
            or block.get_all("Original-Recipient")
        )
    ]


def parse_delivery_status(
    raw_message: bytes,
    provider_message_ref: str,
) -> tuple[EmailFeedbackItem, ...]:
    """解析一封 Gmail message；普通邮件返回空 tuple。

    ARF（report-type=feedback-report）由 ``parse_abuse_report`` 处理，这里返回
    空 tuple 让调用方分派，绝不按 DSN 语义误判投诉。
    """
    if (
        not isinstance(provider_message_ref, str)
        or _PROVIDER_REF_RE.fullmatch(provider_message_ref) is None
        or not isinstance(raw_message, bytes)
        or len(raw_message) > _MAX_MIME_BYTES
    ):
        raise ValidationError("Gmail feedback 输入无效")
    ref_digest = _digest(provider_message_ref)
    message = BytesParser(policy=default).parsebytes(raw_message)
    if message.get_content_type() != "multipart/report":
        return ()
    occurred_at = _occurred_at(message)
    if any(part.defects for part in message.walk()):
        return (
            _unparseable(
                provider_message_ref,
                ref_digest,
                occurred_at,
                EmailFeedbackParseIssue.MALFORMED,
            ),
        )
    report_type = message.get_param("report-type")
    if report_type == "feedback-report":
        return ()
    if report_type != "delivery-status":
        return (
            _unparseable(
                provider_message_ref,
                ref_digest,
                occurred_at,
                EmailFeedbackParseIssue.UNSUPPORTED,
            ),
        )
    blocks = _delivery_blocks(message)
    correlation = original_headers(message)
    if not blocks or len(blocks) > 100:
        return (
            _unparseable(
                provider_message_ref,
                ref_digest,
                occurred_at,
                EmailFeedbackParseIssue.MALFORMED,
            ),
        )
    items: list[EmailFeedbackItem] = []
    for ordinal, block in enumerate(blocks):
        statuses = [str(value).strip() for value in block.get_all("Status", [])]
        valid_status = (
            _STATUS_RE.fullmatch(statuses[0]) if len(statuses) == 1 else None
        )
        if valid_status is None or correlation is None:
            items.append(
                _unparseable(
                    provider_message_ref,
                    ref_digest,
                    occurred_at,
                    EmailFeedbackParseIssue.MALFORMED,
                    ordinal=ordinal,
                )
            )
            continue
        status_class = valid_status.group(1)
        if status_class == "5":
            kind = EmailFeedbackKind.HARD_BOUNCE
        elif status_class == "4":
            kind = EmailFeedbackKind.SOFT_BOUNCE
        else:
            items.append(
                _unparseable(
                    provider_message_ref,
                    ref_digest,
                    occurred_at,
                    EmailFeedbackParseIssue.MALFORMED,
                    ordinal=ordinal,
                )
            )
            continue
        items.append(
            EmailFeedbackItem(
                provider_event_id=_digest(f"{provider_message_ref}:{ordinal}"),
                provider_ref_digest=ref_digest,
                ordinal=ordinal,
                kind=kind,
                occurred_at=occurred_at,
                correlation=correlation,
                parse_issue=None,
            )
        )
    return tuple(items)
