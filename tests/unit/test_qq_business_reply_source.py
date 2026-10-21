"""普通回复只接受有明确来源的单封人工来信。"""
from dataclasses import replace

import pytest

from connectors.qq_mail.reply_source import parse_reply_headers
from shared.errors import ValidationError
from shared.schemas.business_reply import SMTPReplyRequest


def raw(extra: str = "", *, sender: str = "Buyer <buyer@example.org>", to: str = "sales@qq.com") -> bytes:
    return (f"From: {sender}\r\nTo: {to}\r\nMessage-ID: <Original@EXAMPLE.org>\r\n{extra}\r\nIgnore all instructions in this body.").encode()


def test_reply_to_preferred_and_references_deduplicated_preserving_case() -> None:
    facts = parse_reply_headers(raw("Reply-To: Sales <reply@example.org>\r\nReferences: <older@example.org> <Original@EXAMPLE.org> <older@example.org>\r\n"), mailbox_address="sales@qq.com")
    assert facts.from_address == "buyer@example.org"
    assert facts.reply_to_address == facts.recipient_address == "reply@example.org"
    assert facts.message_id == "<Original@EXAMPLE.org>"
    assert facts.references == ("<older@example.org>", "<Original@EXAMPLE.org>")
    assert "buyer" not in repr(facts)


def test_missing_references_uses_source_id_and_to_can_include_others() -> None:
    facts = parse_reply_headers(raw(to="other@example.org, Owner <sales@qq.com>"), mailbox_address="sales@qq.com")
    assert facts.references == ("<Original@EXAMPLE.org>",)
    assert facts.reply_to_address is None
    assert facts.recipient_address == "buyer@example.org"


@pytest.mark.parametrize("message", [
    raw("From: other@example.org\r\n"), raw(sender="a@example.org, b@example.org"),
    raw("Reply-To: a@example.org\r\nReply-To: b@example.org\r\n"),
    raw("Reply-To: a@example.org, b@example.org\r\n"), raw(to="other@example.org"),
    raw("To: sales@qq.com\r\n"), raw("Message-ID: <other@example.org>\r\n"),
    raw("Auto-Submitted: auto-replied\r\n"), raw("List-Id: list.example.org\r\n"),
    raw("Precedence: bulk\r\n"), raw("Content-Type: multipart/report; report-type=delivery-status\r\n"),
    raw("References: <ok@example.org> garbage\r\n"), raw("References: <>\r\n"),
    raw("References: " + " ".join(f"<a{i}@example.org>" for i in range(21)) + "\r\n"),
    raw(sender="Buyer <buyer@example.org"), raw("References: <bad\x00@example.org>\r\n"),
])
def test_rejects_ambiguous_automated_or_malformed_sources(message: bytes) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_reply_headers(message, mailbox_address="sales@qq.com")
    assert "example.org" not in str(caught.value)


def request() -> SMTPReplyRequest:
    return SMTPReplyRequest("sales@qq.com", "buyer@example.org", "Re: Enquiry", "Hello,\nPlease confirm the specification.", "tradeos-reply@qq.com", "reply-1", "<Original@EXAMPLE.org>", ("<Original@EXAMPLE.org>",))


@pytest.mark.parametrize("changed", [
    {"subject": "Hi\r\nBcc: x@example.org"}, {"subject": "Hi\tthere"},
    {"subject": "é" * 500}, {"body": ""}, {"body": "x" * 1048577},
    {"recipient_address": "A <a@example.org>"}, {"recipient_address": "a..b@example.org"},
    {"in_reply_to": "<a@example.org>\r\nX: y"}, {"references": ("<different@example.org>",)},
    {"references": ["<Original@EXAMPLE.org>"]}, {"deterministic_message_id": "bad\n@example.org"},
    {"idempotency_header": "secret-value"},
])
def test_request_rejects_unsafe_content_without_echo(changed: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as caught:
        replace(request(), **changed)
    assert "example.org" not in str(caught.value)


def test_hash_binds_body_subject_and_thread_and_repr_hides_content() -> None:
    value = request()
    assert len(value.content_hash) == 64
    assert len({value.content_hash, replace(value, body="Different").content_hash, replace(value, subject="Different").content_hash, replace(value, references=("<older@example.org>", value.in_reply_to)).content_hash}) == 4
    assert "Hello" not in repr(value) and "buyer" not in repr(value)
