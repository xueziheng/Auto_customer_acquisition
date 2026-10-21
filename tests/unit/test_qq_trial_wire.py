"""原IMAP解析器与socketpair固定样本；无外网、真实邮箱或模型。"""

from dataclasses import replace
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from connectors.gmail.inbound_mime import parse_inbound_content
from connectors.qq_mail.inbound import QQCursor, QQInboundReader, encode_cursor
from connectors.qq_mail.inbound_transport import _Session
from shared.schemas.email_inbound import InboundDisposition, InboundError
from shared.schemas.qq_trial import QQTrialWatermark
from tests.unit.qq_synthetic_imap import synthetic_imap
from tests.unit.test_qq_bounded_trial import ALIAS, Gate
from tests.unit.test_qq_inbound import (
    ATTEMPT,
    EMPLOYEE,
    OUTBOUND,
    ROUTE,
    TO,
    Scope,
    mail,
)


def alternative(*, attachment=False):
    message = EmailMessage(policy=SMTP)
    message["Message-ID"] = "<reply1@qq.com>"
    message["In-Reply-To"] = ALIAS
    message["From"] = "986647123@qq.com"
    message["To"] = TO
    message["Date"] = "Thu, 15 Oct 2026 10:00:00 +0000"
    message.set_content("I want to purchase fans.")
    message.add_alternative("<p>I want to purchase fans.</p>", subtype="html")
    message.set_boundary("fixture-alternative-only")
    if attachment:
        message.add_attachment(b"DO_NOT_DECODE_ATTACHMENT", maintype="application",
            subtype="octet-stream", filename="fixture.bin")
        message.set_boundary("fixture-mixed-only")
    return message.as_bytes()


def cursor(gate):
    gate.start = QQTrialWatermark(uidvalidity=7, high_uid=0)
    return encode_cursor(QQCursor(route=ROUTE, attempt_id=ATTEMPT,
        outbound_message_header=OUTBOUND, uidvalidity=7))


async def test_authorized_alias_reaches_real_imap_parser_and_raw_is_preserved(monkeypatch):
    gate = Gate()
    raw = mail(reply=ALIAS)
    with synthetic_imap(monkeypatch, raw=raw, trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(),
            address=TO, trial_gate=gate)
        page = await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert len(page.items) == 1 and page.items[0].in_reply_to == OUTBOUND
        assert page.items[0].raw_mime == raw
        assert commands.count("UID") == 3  # SEARCH、头、完整正文各一次
        assert gate.calls == ["sessions", "searches", "headers", "bodies"]


@pytest.mark.parametrize("raw,reason", [
    (alternative(attachment=True), "mime_type_not_supported"),
    (mail(reply=ALIAS, extra="Bcc: 123456789@qq.com\r\n"), "bcc_not_allowed"),
    (mail(reply=ALIAS, extra="Content-Disposition: attachment\r\n"), "attachments_not_allowed"),
    (mail(reply=ALIAS, extra="Content-Type: text/html\r\n"), "mime_headers_invalid"),
    (mail(reply=ALIAS).replace(b"text/plain", b"application/octet-stream"), "mime_type_not_supported"),
])
async def test_wire_headers_stop_before_body_with_accurate_reason(monkeypatch, raw, reason):
    gate = Gate()
    with synthetic_imap(monkeypatch, raw=raw, trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(),
            address=TO, trial_gate=gate)
        with pytest.raises(InboundError):
            await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert gate.stopped == reason
        assert gate.calls == ["sessions", "searches", "headers"]
        assert commands.count("UID") == 2 and commands[-1] == "LOGOUT"


def test_shared_parser_supports_standard_alternative_but_does_not_certify_no_attachment():
    parsed = parse_inbound_content(alternative())
    assert parsed.disposition == InboundDisposition.CANDIDATE
    assert "I want to purchase fans." in parsed.body and parsed.evidence_available
    assert len(parsed.evidence_segments) == 2  # 当前共享行为保留两种文本表示。
    with_attachment = parse_inbound_content(alternative(attachment=True))
    assert with_attachment.disposition == InboundDisposition.CANDIDATE
    assert "DO_NOT_DECODE_ATTACHMENT" not in with_attachment.guard_body
    # 因为附件被跳过，CANDIDATE不是无附件证明，不能据此放开trial读取。


@pytest.mark.parametrize("change", ["ungated", "alias", "attempt", "route", "sender", "employee", "revoked", "quoted"])
async def test_trial_search_cannot_expand_scope_or_inject_imap(change):
    gate = Gate()
    target = replace(Scope().grant, outbound_message_header=ALIAS)
    calls = []

    class Connection:
        def uid(self, *args):
            calls.append(args)
            return "OK", [b"1"]

    if change == "alias":
        target = replace(target, outbound_message_header="<other@qq.com>")
    elif change == "attempt":
        target = replace(target, attempt_id="mat_01ARZ3NDEKTSV4RRFFQ69G5FAW")
    elif change == "route":
        target = replace(target, route=ROUTE.model_copy(update={"mailbox_alias": "other"}))
    elif change == "sender":
        target = replace(target, sender="123456789@qq.com")
    elif change == "employee":
        target = replace(target, employee_id="emp_other")
    elif change == "revoked":
        gate.revoked = True
    elif change == "quoted":
        unsafe = '<bad"\\@qq.com>'
        gate.scope = gate.scope.model_copy(update={"provider_header": unsafe})
        target = replace(target, outbound_message_header=unsafe)
    with pytest.raises(InboundError):
        await _Session(Connection(), None if change == "ungated" else gate).search_replies(target, 0, 1)
    assert calls == []


async def test_revoked_trial_does_not_resolve_credentials_or_open_connection():
    from connectors.qq_mail.inbound_transport import QQImapInboundTransport

    class Secrets:
        def resolve(self, _):
            pytest.fail("停止后不能解析凭证")

    gate = Gate()
    gate.revoked = True
    with pytest.raises(InboundError):
        async with QQImapInboundTransport(TO, Secrets(), "fixture", trial_gate=gate).session():
            pytest.fail("停止后不能打开会话")
