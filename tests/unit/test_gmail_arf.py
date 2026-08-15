"""Gmail ARF（RFC 5965）投诉报告解析器的严格安全合同（纯解析，无 IO）。

覆盖：
- 只接受 ``multipart/report; report-type=feedback-report`` + 恰好一个
  bounded ``message/feedback-report`` 部分 + TradeOS 自有关联部分。
- ``Feedback-Type: abuse`` 才产生 COMPLAINT 事实；其余 typed 失败。
- 普通邮件 / DSN / 畸形 multipart / 多个 report / 超大 MIME / 超大 report
  部分 / 缺失或歧义 correlation / 非 UTC 时间 → typed unparseable 结果。
- 绝不从 Subject 或 prose 正文推断投诉。
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from shared.errors import ValidationError
from shared.schemas.email_feedback import EmailFeedbackKind, EmailFeedbackParseIssue

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
_BOUNDARY = "arf-test-boundary"
_DIGEST = "a" * 64
_MESSAGE_ID = "route-v1." + "b" * 64 + "@messages.tradeos.invalid"
_HEADER = "route-v1." + "c" * 64


def _arf_module() -> object:
    try:
        return importlib.import_module("connectors.gmail.arf")
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少 ARF 解析契约: {exc.name}")


def _arf(
    *,
    feedback_type: str | None = "abuse",
    report_parts: int = 1,
    include_rfc822: bool = True,
    message_id: str | None = _MESSAGE_ID,
    idempotency_header: str | None = _HEADER,
    extra_rfc822_parts: int = 0,
    subject: str = "abuse complaint",
    body_text: str = "This message is a complaint.",
    report_body: str = "",
) -> bytes:
    lines = [
        "Date: Sat, 15 Aug 2026 10:00:00 +0000",
        (
            'Content-Type: multipart/report; report-type="feedback-report"; '
            f'boundary="{_BOUNDARY}"'
        ),
        "MIME-Version: 1.0",
        f"Subject: {subject}",
        "",
        f"--{_BOUNDARY}",
        "Content-Type: text/plain",
        "",
        body_text,
        "",
    ]
    for _ in range(report_parts):
        lines.extend(
            [
                f"--{_BOUNDARY}",
                "Content-Type: message/feedback-report",
                "",
            ]
        )
        if feedback_type is not None:
            lines.append(f"Feedback-Type: {feedback_type}")
        lines.extend(["User-Agent: provider-test/1.0", "Version: 1", ""])
        if report_body:
            lines.append(report_body)
            lines.append("")
    for _ in range(0 if not include_rfc822 else 1 + extra_rfc822_parts):
        lines.extend(
            [
                f"--{_BOUNDARY}",
                "Content-Type: message/rfc822",
                "",
            ]
        )
        if message_id is not None:
            lines.append(f"Message-ID: <{message_id}>")
        if idempotency_header is not None:
            lines.append(f"X-TradeOS-Idempotency-V1: {idempotency_header}")
        lines.append("")
    lines.extend([f"--{_BOUNDARY}--", ""])
    return "\r\n".join(lines).encode("ascii")


def _parse(
    raw: bytes,
    *,
    digest: str = _DIGEST,
    occurred_at: datetime = NOW,
) -> tuple[object, ...]:
    module = _arf_module()
    return module.parse_abuse_report(
        raw, provider_ref_digest=digest, occurred_at=occurred_at
    )


def test_valid_abuse_report_yields_single_typed_complaint_item() -> None:
    items = _parse(_arf())
    assert len(items) == 1
    item = items[0]
    assert item.kind is EmailFeedbackKind.COMPLAINT
    assert item.ordinal == 0
    assert item.occurred_at == NOW
    assert item.parse_issue is None
    assert item.provider_ref_digest == _DIGEST
    assert item.provider_event_id == hashlib.sha256(
        f"{_DIGEST}:0".encode("ascii")
    ).hexdigest()
    assert item.correlation.route_id == "route-v1"
    assert item.correlation.deterministic_message_id == _MESSAGE_ID
    assert item.correlation.idempotency_header == _HEADER
    other = _parse(_arf(), digest="f" * 64)
    assert other[0].provider_event_id != item.provider_event_id


def test_feedback_type_abuse_is_case_insensitive_and_others_unsupported() -> None:
    accepted = _parse(_arf(feedback_type="Abuse"))
    assert accepted[0].kind is EmailFeedbackKind.COMPLAINT
    for feedback_type in ("fraud", "virus", "other", "not-spam"):
        items = _parse(_arf(feedback_type=feedback_type))
        assert len(items) == 1
        assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
        assert items[0].parse_issue is EmailFeedbackParseIssue.UNSUPPORTED
        assert items[0].correlation is None


def test_missing_feedback_type_is_malformed() -> None:
    items = _parse(_arf(feedback_type=None))
    assert len(items) == 1
    assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
    assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED


def test_ordinary_mail_and_dsn_never_produce_complaint_facts() -> None:
    ordinary = b"Subject: abuse complaint\r\n\r\nplease stop mailing us\r\n"
    assert _parse(ordinary) == ()
    dsn = (
        "Date: Sat, 15 Aug 2026 10:00:00 +0000\r\n"
        'Content-Type: multipart/report; report-type="delivery-status"; '
        'boundary="dsn"\r\n'
        "MIME-Version: 1.0\r\n"
        "Subject: Delivery Status Notification\r\n"
        "\r\n"
        "--dsn\r\n"
        "Content-Type: message/delivery-status\r\n"
        "\r\n"
        "Final-Recipient: rfc822; private@example.com\r\n"
        "Status: 5.1.1\r\n"
        "\r\n"
        "--dsn--\r\n"
    ).encode("ascii")
    assert _parse(dsn) == ()


def test_subject_and_prose_never_infer_a_complaint() -> None:
    """只有结构化 feedback-report 部分才是投诉证据。"""
    subject_only = (
        "Date: Sat, 15 Aug 2026 10:00:00 +0000\r\n"
        "Subject: complaint abuse unsubscribe\r\n"
        "\r\n"
        "You are spamming us, stop now.\r\n"
    ).encode("ascii")
    assert _parse(subject_only) == ()
    prose_inside_report = _arf(feedback_type="not-spam", subject="kind regards")
    assert prose_inside_report
    items = _parse(prose_inside_report)
    assert items[0].kind is EmailFeedbackKind.UNPARSEABLE


def test_malformed_multipart_returns_typed_malformed() -> None:
    unterminated = (
        "Date: Sat, 15 Aug 2026 10:00:00 +0000\r\n"
        'Content-Type: multipart/report; report-type="feedback-report"; '
        'boundary="x"\r\n'
        "\r\n"
        "--x\r\n"
        "Content-Type: message/feedback-report\r\n"
        "\r\n"
        "Feedback-Type: abuse\r\n"
        "\r\n"
        "unterminated"
    ).encode("ascii")
    items = _parse(unterminated)
    assert len(items) == 1
    assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
    assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED


def test_report_part_count_must_be_exactly_one() -> None:
    for count in (0, 2, 101):
        items = _parse(_arf(report_parts=count))
        assert len(items) == 1, f"report_parts={count}"
        assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
        assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED


def test_oversized_report_part_is_malformed() -> None:
    items = _parse(_arf(report_body="x" * (64 * 1024 + 1)))
    assert len(items) == 1
    assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
    assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED


def test_oversized_raw_mime_returns_typed_malformed_not_raise() -> None:
    items = _parse(b"x" * (4 * 1024 * 1024 + 1))
    assert len(items) == 1
    assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
    assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED


@pytest.mark.parametrize(
    "occurred_at",
    [
        NOW.replace(tzinfo=None),
        datetime(2026, 8, 15, 10, 0, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_non_utc_time_returns_typed_malformed(occurred_at: datetime) -> None:
    items = _parse(_arf(), occurred_at=occurred_at)
    assert len(items) == 1
    assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
    assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED


def test_missing_or_ambiguous_correlation_is_malformed() -> None:
    cases = (
        _arf(include_rfc822=False),
        _arf(message_id=None, idempotency_header=None),
        _arf(message_id="customer@example.com"),
        _arf(message_id="Bearer-private"),
        _arf(extra_rfc822_parts=1),
        _arf(message_id="other-route." + "b" * 64 + "@messages.tradeos.invalid"),
    )
    for raw in cases:
        items = _parse(raw)
        assert len(items) == 1
        assert items[0].kind is EmailFeedbackKind.UNPARSEABLE
        assert items[0].parse_issue is EmailFeedbackParseIssue.MALFORMED
        assert items[0].correlation is None


def test_unparseable_outcomes_never_carry_correlation() -> None:
    for raw in (
        _arf(feedback_type="fraud"),
        _arf(report_parts=0),
        _arf(include_rfc822=False),
    ):
        items = _parse(raw)
        assert items[0].correlation is None
        assert "private" not in repr(items[0])


def test_invalid_inputs_raise_validation_error() -> None:
    module = _arf_module()
    for digest in ("A" * 64, "short", ""):
        with pytest.raises(ValidationError):
            module.parse_abuse_report(
                _arf(), provider_ref_digest=digest, occurred_at=NOW
            )
    with pytest.raises((ValidationError, TypeError)):
        module.parse_abuse_report("not-bytes", provider_ref_digest=_DIGEST, occurred_at=NOW)
    with pytest.raises((ValidationError, TypeError)):
        module.parse_abuse_report(
            _arf(), provider_ref_digest=_DIGEST, occurred_at="2026-08-15T10:00:00Z"
        )
