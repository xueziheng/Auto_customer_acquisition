"""真实 IMAP 解析器配 socketpair；仅合成邮件，无 TCP、真实邮箱或模型。"""

import pytest

from connectors.qq_mail.inbound import QQInboundReader, initial_cursor
from shared.schemas.email_inbound import InboundDisposition, InboundError
from tests.unit.qq_address_fixture import (
    AddressGate,
    address_mail,
    bad_value,
    replace_field,
    word,
)
from tests.unit.qq_mime_fixture import structure_for, wire_value
from tests.unit.qq_synthetic_imap import synthetic_imap
from tests.unit.test_qq_bounded_trial import ALIAS
from tests.unit.test_qq_inbound import (
    ATTEMPT,
    EMPLOYEE,
    FROM,
    OUTBOUND,
    ROUTE,
    TO,
    Scope,
)
from tests.unit.test_qq_trial_wire import cursor


async def test_exact_syntax_passes_header_structure_body_and_retains_original(monkeypatch):
    gate, raw = AddressGate(), address_mail()
    with synthetic_imap(monkeypatch, raw=raw, trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        page = await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert len(page.items) == 1 and page.items[0].disposition is InboundDisposition.CANDIDATE
        assert page.items[0].raw_mime == raw and page.items[0].in_reply_to == OUTBOUND
        assert gate.address_audits == [("from", "to")]
        assert gate.calls == ["sessions", "searches", "headers", "bodies"]
        assert commands.count("UID") == 3


@pytest.mark.parametrize("case", ["invalid_base64", "nonzero_pad_bits", "unicode_display", "encoded_crlf",
    "two_words", "escaped", "folded", "comment", "two_addresses", "group", "wrong_party"])
async def test_unsafe_header_stops_before_body_and_no_compatibility_audit(monkeypatch, case):
    gate = AddressGate()
    raw = replace_field(address_mail(), b"From", bad_value(case, FROM.encode()))
    with synthetic_imap(monkeypatch, raw=raw, trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        with pytest.raises(InboundError):
            await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert gate.stopped == "reply_headers_unsafe"
        assert gate.calls == ["sessions", "searches", "headers"] and not gate.address_audits
        assert commands.count("UID") == 2


@pytest.mark.parametrize("case", ["label", "from", "to", "syntax", "fold", "new_defect", "cleaned"])
async def test_full_body_rechecks_same_raw_addresses_even_when_semantics_match(monkeypatch, case):
    gate, raw = AddressGate(), address_mail()
    changed = {
        "label": raw.replace(b"WHh4eA==", b"QUJDRA=="),
        "from": raw.replace(FROM.encode(), b"111111111@qq.com"),
        "to": raw.replace(TO.encode(), b"2222222222@qq.com"),
        "syntax": raw.replace(b"?B?", b"?b?", 1),
        "fold": raw.replace(b'" <', b'"\r\n <', 1),
        "new_defect": raw.replace(b"WHh4eA==", b"WHh4eB==", 1),
        "cleaned": replace_field(raw, b"From", word(b"Xxxx")[1:-1] + b" <" + FROM.encode() + b">"),
    }[case]
    # 同长度专门排除仅被 RFC822.SIZE 拦截的假阳性。
    if case in {"label", "from", "to", "syntax", "new_defect"}:
        assert len(changed) == len(raw)
    with synthetic_imap(monkeypatch, raw=raw, body_override=changed, trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        with pytest.raises(InboundError):
            await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        # 改变长度由 v4 wire 门拦截；等长的改动由地址后验检查拦截。
        expected = ("reply_headers_unsafe" if len(changed) == len(raw)
                    else "mime_body_mismatch" if len(changed) < len(raw) else None)
        assert gate.stopped == expected  # 超预算 literal 在读取前失败，由 driver 持久停止。
        assert gate.calls == ["sessions", "searches", "headers", "bodies"]
        assert commands.count("UID") == 3 and not gate.address_audits


@pytest.mark.parametrize("case,reason,bodies", [
    ("bcc", "bcc_not_allowed", False),
    ("attachment", "attachments_not_allowed", False),
    ("nested", "mime_structure_invalid", False),
    ("integrity", "mime_body_mismatch", True),
])
async def test_compatible_addresses_do_not_bypass_v4_mime_gates(monkeypatch, case, reason, bodies):
    gate, raw = AddressGate(), address_mail()
    if case == "bcc":
        raw = b"Bcc: 333333333@qq.com\r\n" + raw
    plan = structure_for(raw)
    if case == "attachment":
        plan[0][9] = [b"ATTACHMENT", None]
    elif case == "nested":
        plan[0] = [plan[0], b"MIXED", None, None, None, None]
    elif case == "integrity":
        plan[0][6] += 1
    with synthetic_imap(monkeypatch, raw=raw, bodystructure=wire_value(plan), trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        with pytest.raises(InboundError):
            await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert gate.stopped == reason and ("bodies" in gate.calls) is bodies
        assert commands.count("UID") == (3 if bodies else 2) and not gate.address_audits


async def test_legacy_reader_never_uses_trial_compatibility(monkeypatch):
    raw = address_mail().replace(ALIAS.encode(), OUTBOUND.encode())
    with synthetic_imap(monkeypatch, raw=raw) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO)
        page = await reader.fetch_inbound_page(ROUTE.mailbox_alias, initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
        assert not page.items and commands.count("UID") == 2


async def test_revoked_trial_never_opens_or_uses_compatibility(monkeypatch):
    gate = AddressGate()
    gate.revoked = True
    with synthetic_imap(monkeypatch, raw=address_mail(), trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        with pytest.raises(InboundError):
            await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert not commands and not gate.address_audits
