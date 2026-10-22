"""复现已观测缺陷类别的合成样本；不是 Q02 原件或逐字重建。"""

from email import policy
from email.parser import BytesHeaderParser

import pytest

from connectors.qq_mail.inbound import (
    QQInboundReader,
    _trial_plain_mime,
    reply_headers,
)
from shared.schemas.email_inbound import InboundError
from shared.schemas.qq_trial import QQTrialStopped
from tests.unit.test_qq_bounded_trial import ALIAS, Gate
from tests.unit.test_qq_inbound import EMPLOYEE, ROUTE, TO, Scope, Session
from tests.unit.test_qq_trial_wire import alternative, cursor


def malformed_alternative(names=("From", "To")):
    raw = alternative()
    # 相邻 encoded-word 及末尾缺少分隔，触发同类双 InvalidHeaderDefect。
    # 真实 Q02 原始地址字符串没有被复制；仅已知缺陷类别/数量相同。
    phrase = b"=?utf-8?b?VGVzdA==?==?utf-8?b?VGVzdA==?="
    for name, address in (("From", b"986647123@qq.com"), ("To", TO.encode())):
        if name in names:
            old = name.encode() + b": " + address + b"\r\n"
            new = name.encode() + b": " + phrase + b"<" + address + b">\r\n"
            assert old in raw
            raw = raw.replace(old, new)
    return raw


@pytest.mark.parametrize("names", [("From",), ("To",), ("From", "To")])
def test_address_defects_reject_reply_even_when_addresses_can_be_extracted(names):
    raw = malformed_alternative(names)
    h = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    for name in names:
        assert [type(d).__name__ for d in h[name].defects] == ["InvalidHeaderDefect"] * 2
        assert len(h[name].addresses) == 1
    assert reply_headers(raw, expected_reply=ALIAS) is None


async def test_invalid_associated_addresses_stop_before_mime_admission_and_body_read():
    gate, session = Gate(), Session({1: malformed_alternative()})
    reader = QQInboundReader(ROUTE, EMPLOYEE, session, Scope(), address=TO, trial_gate=gate)
    with pytest.raises(InboundError):
        await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
    assert session.downloads == []
    assert gate.calls == ["sessions", "searches", "headers"]
    assert gate.stopped == "reply_headers_unsafe"


def test_valid_addresses_do_not_remove_multipart_structure_blocker():
    raw = alternative()
    assert reply_headers(raw, expected_reply=ALIAS) is not None
    with pytest.raises(QQTrialStopped) as error:
        _trial_plain_mime(raw)
    assert error.value.reason == "mime_structure_unverified"


def test_bad_addresses_and_multipart_are_independent_checks():
    raw = malformed_alternative()
    assert reply_headers(raw, expected_reply=ALIAS) is None
    with pytest.raises(QQTrialStopped) as error:
        _trial_plain_mime(raw)
    assert error.value.reason == "mime_structure_unverified"
