"""实际脱敏地址语法的正反例；不访问 private 原头或真实正文。"""

import hashlib
from email import policy
from email.errors import InvalidHeaderDefect, ObsoleteHeaderDefect
from email.parser import BytesHeaderParser

import pytest

from connectors.qq_mail.inbound import reply_headers
from connectors.qq_mail.trial_address import KNOWN_DEFECTS, trial_address
from tests.unit.qq_address_fixture import (
    BAD_CASES,
    FIXTURE,
    address_mail,
    bad_value,
    replace_field,
    word,
)
from tests.unit.test_qq_bounded_trial import ALIAS, trial_scope
from tests.unit.test_qq_inbound import FROM, TO


def parse(raw):
    return reply_headers(raw, expected_reply=ALIAS, trial_scope=trial_scope())


def test_exact_redacted_fixture_hash_defects_and_strict_standard_agreement():
    raw = FIXTURE.read_bytes()
    assert len(raw) == 107
    assert hashlib.sha256(raw).hexdigest() == "34525bb52270f13d3060e4203a94fcaba0643b96549578bc8e44d35372151430"
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    for name, expected in (("From", "111111111@qq.com"), ("To", "2222222222@qq.com")):
        result = trial_address(h, raw, name, expected)
        assert result is not None and result.defects == KNOWN_DEFECTS
        assert tuple(str(d) for d in h[name].defects) == KNOWN_DEFECTS
        assert all(type(d) is InvalidHeaderDefect for d in h[name].defects)
        assert result.raw_field in raw and expected not in repr(result)


@pytest.mark.parametrize("changed", [None, "From", "To"])
def test_controlled_exact_parties_accept_one_or_both_compatible_fields(changed):
    raw = address_mail()
    if changed:
        raw = replace_field(raw, changed.encode(), (FROM if changed == "From" else TO).encode())
    result = parse(raw)
    assert result is not None and (result.sender, result.recipient) == (FROM, TO)
    assert [e.field for e in result.address_evidence if e.defects] == (
        ["from", "to"] if changed is None else ["to" if changed == "From" else "from"])
    assert FROM not in repr(result)


@pytest.mark.parametrize("size", [1, 4, 10, 32])
def test_display_is_bounded_presentation_and_not_customer_identity(size):
    first = parse(address_mail())
    second = parse(replace_field(address_mail(), b"From", word(b"Z" * size) + b" <" + FROM.encode() + b">"))
    assert second is not None and second.sender == first.sender
    assert second != first  # 原始显示名变化仍被后验原头比较发现。


@pytest.mark.parametrize("name,mailbox", [(b"From", FROM.encode()), (b"To", TO.encode())])
@pytest.mark.parametrize("case", BAD_CASES)
def test_unknown_syntax_or_party_cannot_use_compatibility(name, mailbox, case):
    assert parse(replace_field(address_mail(), name, bad_value(case, mailbox))) is None


@pytest.mark.parametrize("name", [b"From", b"To", b"Date", b"In-Reply-To", b"Message-ID"])
def test_duplicate_required_headers_stay_rejected(name):
    raw = address_mail()
    line = next(line for line in raw.split(b"\r\n") if line.startswith(name + b":"))
    assert parse(line + b"\r\n" + raw) is None


@pytest.mark.parametrize("mode", ["no_scope", "only_alias", "scope_without_alias", "other_alias"])
def test_no_implicit_compatibility_for_legacy_or_alias_only(mode):
    args = {
        "no_scope": {}, "only_alias": {"expected_reply": ALIAS},
        "scope_without_alias": {"trial_scope": trial_scope()},
        "other_alias": {"trial_scope": trial_scope(), "expected_reply": "<different@qq.com>"},
    }[mode]
    assert reply_headers(address_mail(), **args) is None


@pytest.mark.parametrize("mode", ["extra", "different_message", "different_type", "missing", "none", "duplicate"])
def test_exact_defect_types_messages_and_multiplicity_are_required(mode):
    raw = address_mail()
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    header = h["From"]
    defects = list(header.defects)
    if mode == "extra":
        defects.append(InvalidHeaderDefect("new parser defect"))
    elif mode == "different_message":
        defects[1] = InvalidHeaderDefect("another defect")
    elif mode == "different_type":
        defects[0] = ObsoleteHeaderDefect(KNOWN_DEFECTS[0])
    elif mode == "missing":
        defects.pop()
    elif mode == "none":
        defects = []
    else:
        defects[1] = defects[0]
    header._defects = defects
    h.replace_header("From", header)
    assert trial_address(h, raw, "From", FROM) is None


@pytest.mark.parametrize("mode", ["mailbox", "label", "group"])
def test_standard_parser_and_raw_bytes_must_agree(mode):
    raw = address_mail()
    altered = replace_field(raw, b"From", word(b"ABCD") + b" <" + FROM.encode() + b">")
    if mode == "mailbox":
        altered = raw.replace(FROM.encode(), b"111111111@qq.com")
    elif mode == "group":
        altered = replace_field(raw, b"From", b"Friends: " + word(b"Xxxx") + b" <" + FROM.encode() + b">;")
    h = BytesHeaderParser(policy=policy.default).parsebytes(altered)
    assert trial_address(h, raw, "From", FROM) is None
