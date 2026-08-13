"""RFC 3464 Gmail DSN 解析器的保守分类合同。"""

from __future__ import annotations

import importlib
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from shared.errors import ValidationError


def _parser() -> object:
    try:
        return importlib.import_module("connectors.gmail.feedback")
    except ModuleNotFoundError as exc:
        pytest.fail(f"缺少 Gmail feedback parser: {exc}")


def _dsn(
    *,
    blocks: tuple[str | None, ...] = ("5.1.1",),
    report_type: str = "delivery-status",
    message_id: tuple[str, ...] = ("<" + "a" * 64 + "@messages.tradeos.invalid>",),
    idempotency: tuple[str, ...] = ("b" * 64,),
) -> bytes:
    boundary = "tradeos-dsn-boundary"
    lines = [
        "From: MAILER-DAEMON@example.invalid",
        "To: ignored@example.invalid",
        "Date: Thu, 13 Aug 2026 10:00:00 +0000",
        "Subject: must never classify from this hard bounce subject",
        f'Content-Type: multipart/report; report-type="{report_type}"; boundary="{boundary}"',
        "MIME-Version: 1.0",
        "",
        f"--{boundary}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        "Customer diagnostic and body must be ignored.",
        f"--{boundary}",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; mx.example.invalid",
        "",
    ]
    for index, status in enumerate(blocks):
        lines.extend(
            [
                f"Final-Recipient: rfc822; private-{index}@example.com",
            ]
        )
        if status is not None:
            lines.append(f"Status: {status}")
        lines.extend(["Diagnostic-Code: smtp; private diagnostic marker", ""])
    lines.extend(
        [
            f"--{boundary}",
            "Content-Type: message/rfc822",
            "",
            *(f"Message-ID: {value}" for value in message_id),
            *(f"X-TradeOS-Idempotency-V1: {value}" for value in idempotency),
            "From: private-sender@example.com",
            "To: private-recipient@example.com",
            "Subject: private original subject",
            "",
            "private original body",
            f"--{boundary}--",
            "",
        ]
    )
    return "\r\n".join(lines).encode("ascii")


def _ordinary_mail() -> bytes:
    message = EmailMessage(policy=SMTP)
    message["From"] = "sender@example.com"
    message["To"] = "recipient@example.com"
    message["Date"] = "Thu, 13 Aug 2026 10:00:00 +0000"
    message["Subject"] = "Delivery failed permanently"
    message.set_content("Status: 5.1.1")
    return message.as_bytes()


def test_provider_event_id_is_digest_of_message_ref_and_block_ordinal() -> None:
    parser = _parser()
    first = parser.parse_delivery_status(
        _dsn(blocks=("5.1.1", "4.2.2")), "gmail-safe-1"
    )
    replay = parser.parse_delivery_status(
        _dsn(blocks=("5.1.1", "4.2.2")), "gmail-safe-1"
    )
    assert [item.provider_event_id for item in first] == [
        item.provider_event_id for item in replay
    ]
    assert len({item.provider_event_id for item in first}) == 2
    assert [item.ordinal for item in first] == [0, 1]
    assert [item.kind.value for item in first] == ["hard_bounce", "soft_bounce"]
    assert all(item.provider_ref_digest == first[0].provider_ref_digest for item in first)


def test_missing_status_block_is_unparseable_without_shifting_later_ordinal() -> None:
    items = _parser().parse_delivery_status(
        _dsn(blocks=(None, "5.1.1")),
        "gmail-safe-mixed",
    )
    assert [item.ordinal for item in items] == [0, 1]
    assert [item.kind.value for item in items] == ["unparseable", "hard_bounce"]


@pytest.mark.parametrize("status", ("", "2.0.0", "invalid", "5.1.1 4.2.2"))
def test_missing_invalid_success_or_contradictory_status_is_unparseable(
    status: str,
) -> None:
    item = _parser().parse_delivery_status(_dsn(blocks=(status,)), "gmail-safe-2")[0]
    assert item.kind.value == "unparseable"
    assert item.parse_issue.value == "malformed"
    assert item.correlation is None


@pytest.mark.parametrize(
    ("message_ids", "headers"),
    [
        (("<a@messages.tradeos.invalid>", "<b@messages.tradeos.invalid>"), ("b" * 64,)),
        (("<" + "a" * 64 + "@messages.tradeos.invalid>",), ("b" * 64, "c" * 64)),
        (("private@example.com",), ("b" * 64,)),
    ],
)
def test_duplicate_conflicting_or_invalid_correlation_is_unparseable(
    message_ids: tuple[str, ...], headers: tuple[str, ...]
) -> None:
    item = _parser().parse_delivery_status(
        _dsn(message_id=message_ids, idempotency=headers), "gmail-safe-3"
    )[0]
    assert item.kind.value == "unparseable"
    assert item.correlation is None


@pytest.mark.parametrize(
    ("message_ids", "headers", "missing_field"),
    [
        (("<" + "a" * 64 + "@messages.tradeos.invalid>",), (), "idempotency_header"),
        ((), ("b" * 64,), "deterministic_message_id"),
    ],
)
def test_one_approved_correlation_key_still_preserves_typed_feedback(
    message_ids: tuple[str, ...],
    headers: tuple[str, ...],
    missing_field: str,
) -> None:
    item = _parser().parse_delivery_status(
        _dsn(message_id=message_ids, idempotency=headers),
        "gmail-safe-single-key",
    )[0]
    assert item.kind.value == "hard_bounce"
    assert item.correlation is not None
    assert getattr(item.correlation, missing_field) is None


def test_non_report_mail_produces_no_item_and_subject_body_are_not_inferred() -> None:
    assert _parser().parse_delivery_status(_ordinary_mail(), "gmail-safe-4") == ()


def test_unsupported_report_type_is_one_safe_unparseable_item() -> None:
    items = _parser().parse_delivery_status(
        _dsn(report_type="disposition-notification"), "gmail-safe-5"
    )
    assert len(items) == 1
    assert items[0].kind.value == "unparseable"
    assert items[0].parse_issue.value == "unsupported"


def test_malformed_claimed_report_is_deterministic_and_missing_ref_fails() -> None:
    raw = b"Content-Type: multipart/report; report-type=delivery-status\r\n\r\nmalformed"
    first = _parser().parse_delivery_status(raw, "gmail-safe-6")
    second = _parser().parse_delivery_status(raw, "gmail-safe-6")
    assert len(first) == 1
    assert first[0].provider_event_id == second[0].provider_event_id
    assert first[0].kind.value == "unparseable"
    with pytest.raises(ValidationError):
        _parser().parse_delivery_status(raw, "")


def test_mime_defect_and_more_than_100_recipient_blocks_are_unparseable() -> None:
    parser = _parser()
    missing_boundary = _dsn().replace(b"--tradeos-dsn-boundary--\r\n", b"")
    defect = parser.parse_delivery_status(missing_boundary, "gmail-safe-defect")
    oversized = parser.parse_delivery_status(
        _dsn(blocks=tuple("5.1.1" for _ in range(101))),
        "gmail-safe-oversized",
    )
    for items in (defect, oversized):
        assert len(items) == 1
        assert items[0].kind.value == "unparseable"
        assert items[0].parse_issue.value == "malformed"


def test_parser_never_exposes_raw_mime_values_in_repr_or_errors() -> None:
    raw = _dsn()
    item = _parser().parse_delivery_status(raw, "gmail-safe-7")[0]
    rendered = repr(item)
    for forbidden in (
        "private-recipient",
        "private diagnostic",
        "private original body",
        "private original subject",
    ):
        assert forbidden not in rendered
