"""Gmail ARF（RFC 5965）投诉报告的保守解析器；原始 MIME 只在调用栈内存在。

只接受 ``multipart/report; report-type=feedback-report`` + 恰好一个 bounded
``message/feedback-report`` 部分 + TradeOS 自有关联部分；``Feedback-Type:
abuse`` 才产生 COMPLAINT 事实。绝不从 Subject、正文或收件人推断投诉。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.parser import BytesParser
from email.policy import default

from shared.errors import ValidationError
from shared.schemas.email_feedback import (
    EmailFeedbackItem,
    EmailFeedbackKind,
    EmailFeedbackParseIssue,
)

from .feedback import original_headers

_MAX_MIME_BYTES = 4 * 1024 * 1024
_MAX_REPORT_PART_BYTES = 64 * 1024
_LOWER_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _unparseable(
    ref_digest: str,
    issue: EmailFeedbackParseIssue,
    *,
    occurred_at: datetime = _EPOCH,
) -> EmailFeedbackItem:
    return EmailFeedbackItem(
        provider_event_id=_digest(f"{ref_digest}:0"),
        provider_ref_digest=ref_digest,
        ordinal=0,
        kind=EmailFeedbackKind.UNPARSEABLE,
        occurred_at=occurred_at,
        correlation=None,
        parse_issue=issue,
    )


def _report_message(report: Message) -> Message | None:
    """反馈报告字段所在的内部消息；``message/feedback-report`` 的载荷按
    ``message/*`` 语义解析为嵌套消息，字段（含 Feedback-Type）在其上。"""
    payload = report.get_payload()
    if isinstance(payload, str):
        return report
    if (
        isinstance(payload, list)
        and len(payload) == 1
        and isinstance(payload[0], Message)
    ):
        return payload[0]
    return None


def _feedback_types(report: Message) -> list[str]:
    """同时接受报告部分自身头与嵌套消息头两种写法。"""
    values = [str(value).strip() for value in report.get_all("Feedback-Type", [])]
    inner = _report_message(report)
    if inner is not None and inner is not report:
        values.extend(
            str(value).strip() for value in inner.get_all("Feedback-Type", [])
        )
    return values


def parse_abuse_report(
    raw_message: bytes,
    *,
    provider_ref_digest: str,
    occurred_at: datetime,
) -> tuple[EmailFeedbackItem, ...]:
    """解析一封 Gmail ARF 投诉；非投诉/DSN 返回空 tuple，异常输入返回 typed 项。

    边界：
    - 输入形状（bytes / 64-hex digest / datetime）错误 → ``ValidationError``。
    - 非 UTC 时间、超大 MIME、畸形 multipart、report 部分数量 != 1、
      超大 report 部分、缺失/歧义 correlation → 固定 ``UNPARSEABLE`` 项
      （由整页流程隔离），绝不从 Subject/正文推断投诉。
    - 返回后原始 MIME 不在任何 DTO 或持久化中（只保留 digest 与 typed 字段）。
    """
    if (
        not isinstance(raw_message, bytes)
        or not isinstance(provider_ref_digest, str)
        or _LOWER_HEX_64_RE.fullmatch(provider_ref_digest) is None
        or not isinstance(occurred_at, datetime)
    ):
        raise ValidationError("ARF 输入无效")
    if not _is_utc(occurred_at):
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    if occurred_at == _EPOCH:
        # EPOCH 是「缺失/不可解析 Date」的哨兵（见 message_occurred_at）。
        # 带哨兵形成 COMPLAINT 会让信誉域因 occurred_at 早于身份创建时间而
        # 整页永久回滚；必须 typed MALFORMED 供隔离。
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    if len(raw_message) > _MAX_MIME_BYTES:
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    message = BytesParser(policy=default).parsebytes(raw_message)
    if message.get_content_type() != "multipart/report":
        return ()
    if any(part.defects for part in message.walk()):
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    report_type = message.get_param("report-type")
    if report_type == "delivery-status":
        return ()
    if report_type != "feedback-report":
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.UNSUPPORTED),)
    report_parts = [
        part for part in message.walk() if part.get_content_type() == "message/feedback-report"
    ]
    if len(report_parts) != 1:
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    report = report_parts[0]
    report_message = _report_message(report)
    if report_message is None or len(report_message.as_bytes()) > _MAX_REPORT_PART_BYTES:
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    feedback_types = _feedback_types(report)
    if len(feedback_types) != 1:
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    if feedback_types[0].casefold() != "abuse":
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.UNSUPPORTED),)
    correlation = original_headers(message)
    if correlation is None:
        return (_unparseable(provider_ref_digest, EmailFeedbackParseIssue.MALFORMED),)
    return (
        EmailFeedbackItem(
            provider_event_id=_digest(f"{provider_ref_digest}:0"),
            provider_ref_digest=provider_ref_digest,
            ordinal=0,
            kind=EmailFeedbackKind.COMPLAINT,
            occurred_at=occurred_at,
            correlation=correlation,
            parse_issue=None,
        ),
    )
