"""固定合成结构与字节；仅纯函数和 socketpair，无真实 FETCH。"""

import copy

import pytest

from connectors.qq_mail.inbound import QQInboundReader
from connectors.qq_mail.mime_structure import (
    parse_body_fetch,
    parse_header_fetch,
    validate_structure,
    verify_mime,
)
from shared.schemas.email_inbound import InboundError
from shared.schemas.qq_trial import QQTrialStopped
from tests.unit.qq_mime_fixture import structure_for, wire_value
from tests.unit.qq_synthetic_imap import synthetic_imap
from tests.unit.test_qq_bounded_trial import Gate
from tests.unit.test_qq_inbound import EMPLOYEE, ROUTE, TO, Scope, mail
from tests.unit.test_qq_trial_wire import ALIAS, alternative, cursor

FIELDS = "MESSAGE-ID"
HEAD = b"Message-ID: <fixture@qq.com>\r\n\r\n"
PLAIN = [b"TEXT", b"PLAIN", [b"CHARSET", b"utf-8"], None, None, b"7BIT", 5, 1, None, None, None, None]


def response(structure=None, *, suffix=b")", uid=1, header=HEAD, total=500):
    structure = wire_value(PLAIN) if structure is None else structure
    prefix = (b"1 (UID " + str(uid).encode() + b" RFC822.SIZE " + str(total).encode()
              + b" BODYSTRUCTURE " + structure + b" BODY[HEADER.FIELDS (MESSAGE-ID)] {" + str(len(header)).encode() + b"}")
    return [(prefix, header), suffix]


def test_imap_literals_inside_structure_are_not_confused_with_header_or_uid():
    a = b"1 (UID 1 RFC822.SIZE 500 BODYSTRUCTURE (\"TEXT\" \"PLAIN\" (\"CHARSET\" {5}"
    b = b") NIL NIL \"7BIT\" 5 1 NIL NIL NIL NIL) BODY[HEADER.FIELDS (MESSAGE-ID)] {" + str(len(HEAD)).encode() + b"}"
    result = [(a, b"utf-8"), (b, HEAD), b")"]
    size, raw, bs = parse_header_fetch(result, 1, FIELDS)
    assert size == 500 and raw == HEAD and bs == PLAIN


@pytest.mark.parametrize("change", [
    "truncated", "duplicate_uid", "wrong_uid", "extra_response", "unknown_attribute",
    "nil_structure", "missing_structure", "bad_literal_size", "partial_header", "bare_newline",
    "adjacent_strings", "deep", "many_nodes", "large", "oversize_total", "quoted_uid",
])
def test_malformed_or_unbounded_fetch_response_is_rejected(change):
    result = response()
    meta, raw = result[0]
    if change == "truncated":
        result[-1] = b""
    elif change == "duplicate_uid":
        meta = meta.replace(b"UID 1", b"UID 1 UID 1")
    elif change == "wrong_uid":
        meta = meta.replace(b"UID 1", b"UID 2")
    elif change == "extra_response":
        result += [b"2 (UID 2)"]
    elif change == "unknown_attribute":
        meta = meta.replace(b"UID 1", b"UID 1 UNKNOWN NIL")
    elif change == "nil_structure":
        meta = meta.replace(wire_value(PLAIN), b"NIL")
    elif change == "missing_structure":
        meta = meta.replace(b"BODYSTRUCTURE " + wire_value(PLAIN) + b" ", b"")
    elif change == "bad_literal_size":
        meta = meta.replace(str(len(raw)).encode() + b"}", b"999}")
    elif change == "partial_header":
        meta = meta.replace(b"] {", b"]<0> {")
    elif change == "bare_newline":
        meta = b"\n" + meta
    elif change == "adjacent_strings":
        meta = meta.replace(b'"TEXT" "PLAIN"', b'"TEXT""PLAIN"')
    elif change == "deep":
        meta = meta.replace(wire_value(PLAIN), b"(" * 15 + b"NIL" + b")" * 15)
    elif change == "many_nodes":
        meta = meta.replace(wire_value(PLAIN), b"(" + b"NIL " * 300 + b")")
    elif change == "large":
        meta += b" " * 131073
    elif change == "oversize_total":
        meta = meta.replace(b"SIZE 500", b"SIZE 262145")
    else:
        meta = meta.replace(b"UID 1", b'UID "1"')
    result[0] = meta, raw
    with pytest.raises(QQTrialStopped):
        parse_header_fetch(result, 1, FIELDS)


@pytest.mark.parametrize("change", [
    "third_part", "nested", "message", "mixed", "duplicate_type", "reversed",
    "filename", "name_star", "duplicate_parameter", "attachment", "inline_filename",
    "unknown_encoding", "huge_leaf", "negative_lines", "huge_lines", "unknown_extension",
    "content_id", "language", "bad_boundary", "no_boundary", "unknown_charset",
])
def test_structure_admission_rejects_attachments_unknowns_and_ambiguity(change):
    bs = copy.deepcopy(structure_for(alternative()))
    if change == "third_part":
        bs.insert(0, copy.deepcopy(bs[0]))
    elif change == "nested":
        bs[0] = copy.deepcopy(bs)
    elif change == "message":
        bs[0][0:2] = [b"MESSAGE", b"RFC822"]
    elif change == "mixed":
        bs[2] = b"MIXED"
    elif change == "duplicate_type":
        bs[1][1] = b"PLAIN"
    elif change == "reversed":
        bs[0], bs[1] = bs[1], bs[0]
    elif change == "filename":
        bs[0][2] += [b"NAME", b"secret.txt"]
    elif change == "name_star":
        bs[0][2] += [b"NAME*", b"utf-8''secret.txt"]
    elif change == "duplicate_parameter":
        bs[0][2] += [b"charset", b"utf-8"]
    elif change == "attachment":
        bs[0][9] = [b"ATTACHMENT", None]
    elif change == "inline_filename":
        bs[0][9] = [b"INLINE", [b"FILENAME", b"x.txt"]]
    elif change == "unknown_encoding":
        bs[0][5] = b"BINARY"
    elif change == "huge_leaf":
        bs[0][6] = 262145
    elif change == "negative_lines":
        bs[0][7] = -1
    elif change == "huge_lines":
        bs[0][7] = 262144
    elif change == "unknown_extension":
        bs.append([None])
    elif change == "content_id":
        bs[0][3] = b"cid:hidden"
    elif change == "language":
        bs[0][10] = b"en"
    elif change == "bad_boundary":
        bs[3] = [b"BOUNDARY", b"bad\r\nboundary"]
    elif change == "no_boundary":
        bs[3] = None
    else:
        bs[0][2] = [b"CHARSET", b"utf-7"]
    with pytest.raises(QQTrialStopped):
        validate_structure(bs, len(alternative()))


@pytest.mark.parametrize("raw", [mail(reply=ALIAS), alternative()])
def test_raw_mime_matches_independent_structure(raw):
    plan = validate_structure(structure_for(raw), len(raw))
    verify_mime(raw, plan, len(raw))


@pytest.mark.parametrize("encoding,body", [
    ("base64", "SGVsbG8=\r\n"), ("quoted-printable", "Hello=20world\r\n"),
    ("7bit", "Hello\r\n"), ("8bit", "Hello\r\n"),
])
def test_supported_leaf_encodings_preserve_raw_bytes(encoding, body):
    raw = mail(reply=ALIAS, extra=f"Content-Transfer-Encoding: {encoding}\r\n", body=body)
    verify_mime(raw, validate_structure(structure_for(raw), len(raw)), len(raw))


@pytest.mark.parametrize("encoding,body", [("base64", "bad_@@\r\n"), ("quoted-printable", "bad=XY\r\n")])
def test_declared_encoding_with_invalid_payload_does_not_archive(encoding, body):
    raw = mail(reply=ALIAS, extra=f"Content-Transfer-Encoding: {encoding}\r\n", body=body)
    with pytest.raises(QQTrialStopped) as error:
        verify_mime(raw, validate_structure(structure_for(raw), len(raw)), len(raw))
    assert error.value.reason == "mime_body_mismatch"


def test_metadata_after_header_literal_is_fully_consumed():
    result = [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {" + str(len(HEAD)).encode() + b"}", HEAD),
              b" UID 1 RFC822.SIZE 500 BODYSTRUCTURE " + wire_value(PLAIN) + b")"]
    assert parse_header_fetch(result, 1, FIELDS) == (500, HEAD, PLAIN)


@pytest.mark.parametrize("change", [
    "filename", "disposition", "extra_part", "truncated_boundary", "preamble", "epilogue",
    "duplicate_header", "boundary_mismatch", "encoding", "size", "leaf_size", "lines", "mime_version",
])
def test_post_read_structure_or_integrity_changes_are_rejected(change):
    raw = alternative()
    bs = structure_for(raw)
    if change == "filename":
        raw = raw.replace(b'charset="utf-8"', b'charset="utf-8"; name="x.txt"', 1)
    elif change == "disposition":
        raw = raw.replace(b"Content-Type: text/plain", b"Content-Disposition: attachment\r\nContent-Type: text/plain")
    elif change == "extra_part":
        raw = raw.replace(b"--fixture-alternative-only--", b"--fixture-alternative-only\r\nContent-Type: text/plain\r\n\r\nextra\r\n\r\n--fixture-alternative-only--")
    elif change == "truncated_boundary":
        raw = raw[:-4]
    elif change == "preamble":
        raw = raw.replace(b"\r\n\r\n--fixture", b"\r\n\r\npreamble\r\n--fixture", 1)
    elif change == "epilogue":
        raw += b"hidden"
    elif change == "duplicate_header":
        raw = raw.replace(b"Content-Type: text/plain", b"Content-Type: text/plain\r\nContent-Type: text/plain")
    elif change == "boundary_mismatch":
        bs[3] = [b"BOUNDARY", b"other"]
    elif change == "encoding":
        raw = raw.replace(b"Content-Transfer-Encoding: 7bit", b"Content-Transfer-Encoding: base64", 1)
    elif change == "size":
        with pytest.raises(QQTrialStopped):
            verify_mime(raw, validate_structure(bs, len(raw)), len(raw) + 1)
        return
    elif change == "leaf_size":
        bs[0][6] += 1
    elif change == "lines":
        bs[0][7] += 1
    else:
        raw = raw.replace(b"MIME-Version: 1.0", b"MIME-Version: 2.0")
    with pytest.raises(QQTrialStopped) as error:
        verify_mime(raw, validate_structure(bs, len(raw)), len(raw))
    assert error.value.reason == "mime_body_mismatch"


@pytest.mark.parametrize("change", ["truncated", "wrong_uid", "size", "duplicate", "extra"])
def test_body_literal_requires_complete_unambiguous_fetch_envelope(change):
    result = [(b"1 (UID 1 BODY[] {3}", b"abc"), b")"]
    if change == "truncated":
        result[-1] = b""
    elif change == "wrong_uid":
        result[0] = (b"1 (UID 2 BODY[] {3}", b"abc")
    elif change == "size":
        result[0] = (b"1 (UID 1 BODY[] {2}", b"abc")
    elif change == "duplicate":
        result[0] = (b"1 (UID 1 UID 1 BODY[] {3}", b"abc")
    else:
        result += [b"2 (UID 2)"]
    with pytest.raises(QQTrialStopped):
        parse_body_fetch(result, 1, 3)


async def test_socketpair_alternative_passes_with_one_header_and_one_body(monkeypatch):
    gate, raw = Gate(), alternative()
    with synthetic_imap(monkeypatch, raw=raw, trial_gate=gate) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        page = await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert len(page.items) == 1 and page.items[0].raw_mime == raw
        assert commands.count("UID") == 3 and gate.calls == ["sessions", "searches", "headers", "bodies"]


@pytest.mark.parametrize("change", ["attachment", "truncated", "nested"])
async def test_socketpair_structure_stops_before_body(monkeypatch, change):
    gate, raw = Gate(), alternative()
    bs = structure_for(raw)
    if change == "attachment":
        bs[0][9] = [b"ATTACHMENT", None]
    elif change == "nested":
        bs[0] = copy.deepcopy(bs)
    wire = wire_value(bs)
    if change == "truncated":
        wire = wire[:-1]
    with synthetic_imap(monkeypatch, raw=raw, trial_gate=gate, bodystructure=wire) as (transport, commands):
        reader = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO, trial_gate=gate)
        with pytest.raises(InboundError):
            await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
        assert gate.stopped is not None and "bodies" not in gate.calls and commands.count("UID") == 2
