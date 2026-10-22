"""固定脱敏样本只复现 Q02 的折行形态，不读取真实 eml。"""

import hashlib

import pytest

from connectors.qq_mail.sent_alias import verify_sent_alias
from shared.schemas.qq_trial import QQTrialStopped
from tests.unit.test_qq_inbound import FROM, TO

MARKER = "pilot." + "a" * 64
CANONICAL = f"<{MARKER}@messages.tradeos.invalid>"
PROVIDER = "<tencent_SYNTHETIC_Q02@qq.com>"


def header(marker=MARKER):
    return (
        f"Message-ID: {PROVIDER}\r\nFrom: {TO}\r\nTo: {FROM}\r\n"
        f"X-TradeOS-Idempotency-V1: {marker}\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\nMIME-Version: 1.0\r\n\r\n'
    ).encode()


def verify(raw, *, source="recipient_export", expected=MARKER, canonical=CANONICAL):
    return verify_sent_alias(
        (raw,), source=source, canonical_header=canonical,
        idempotency_header=expected, sender=TO, recipient=FROM,
    )


@pytest.mark.parametrize("marker", [
    MARKER, " " + MARKER, "\t" + MARKER + "\t ",
    "\r\n " + MARKER, "\r\n\t" + MARKER + " \t",
    " \t\r\n \t" + MARKER,
])
@pytest.mark.parametrize("source", ["recipient_export", "sender_sent"])
def test_legal_outer_folding_preserves_exact_source_bytes_and_hash(marker, source):
    raw = header(marker)
    before = hashlib.sha256(raw).hexdigest()
    result = verify(raw, source=source)
    assert result.provider_header == PROVIDER and result.source == source
    assert result.raw_headers is raw and result.headers_sha256 == before
    assert "tencent" not in repr(result)


@pytest.mark.parametrize("marker", [
    MARKER[:12] + " " + MARKER[12:],
    MARKER[:12] + "\t" + MARKER[12:],
    MARKER[:12] + "\r\n " + MARKER[12:],
    MARKER[:12] + "\r\n\t" + MARKER[12:],
    "\n " + MARKER, "\r " + MARKER, "\r\n" + MARKER,
    "\v" + MARKER, MARKER + "\f", MARKER + "\x00", MARKER + "\x7f",
    "\u00a0" + MARKER, '"' + MARKER + '"', MARKER + " (comment)",
    "=?us-ascii?q?" + MARKER + "?=", MARKER.upper(), "b" * 64,
    MARKER + "\r\nInjected: yes\r\nX-TradeOS-Idempotency-V1: " + MARKER,
])
def test_invalid_folding_or_internal_whitespace_is_not_repaired(marker):
    with pytest.raises(QQTrialStopped):
        verify(header(marker))


@pytest.mark.parametrize("name", ["Message-ID", "From", "To", "X-TradeOS-Idempotency-V1"])
def test_duplicate_binding_headers_are_rejected_case_insensitively(name):
    raw = header()
    line = next(line for line in raw.split(b"\r\n") if line.startswith(name.encode() + b":"))
    duplicate = line.split(b":", 1)
    raw = raw[:-2] + duplicate[0].lower() + b":" + duplicate[1] + b"\r\n\r\n"
    with pytest.raises(QQTrialStopped):
        verify(raw)


@pytest.mark.parametrize("change", ["lf", "orphan", "space_name", "body", "wrong_expected", "wrong_canonical"])
def test_malformed_block_or_expected_binding_fails_closed(change):
    raw, args = header(), {}
    if change == "lf":
        raw = raw.replace(b"\r\n", b"\n")
    elif change == "orphan":
        raw = b" orphan\r\n" + raw
    elif change == "space_name":
        raw = raw.replace(b"X-TradeOS-Idempotency-V1:", b"X-TradeOS-Idempotency-V1 :")
    elif change == "body":
        raw += b"synthetic body"
    elif change == "wrong_expected":
        args["expected"] = " " + MARKER
    else:
        args["canonical"] = CANONICAL.replace("a" * 64, "b" * 64)
    with pytest.raises(QQTrialStopped):
        verify(raw, **args)


@pytest.mark.parametrize("source", ["", "unknown", "submitted_content", "automatic_collector", None])
def test_unknown_or_implicit_evidence_source_is_not_accepted(source):
    with pytest.raises(QQTrialStopped):
        verify(header(), source=source)


def test_missing_source_is_not_implicitly_a_successful_collector():
    with pytest.raises(TypeError):
        verify_sent_alias((header(),), canonical_header=CANONICAL,
                          idempotency_header=MARKER, sender=TO, recipient=FROM)


def test_legal_normalization_does_not_merge_two_candidate_messages():
    with pytest.raises(QQTrialStopped) as error:
        verify_sent_alias((header(), header("\r\n " + MARKER)), source="recipient_export",
                          canonical_header=CANONICAL, idempotency_header=MARKER,
                          sender=TO, recipient=FROM)
    assert error.value.reason == "alias_missing_or_ambiguous"
