"""精确已关联但不能安全解释的候选停止；无关包含匹配不阻塞。"""

import pytest

from connectors.qq_mail.inbound import QQInboundReader, decode_cursor
from shared.schemas.email_inbound import InboundError
from tests.unit.test_qq_bounded_trial import ALIAS, Gate
from tests.unit.test_qq_inbound import EMPLOYEE, FROM, ROUTE, TO, Scope, Session, mail
from tests.unit.test_qq_q02_compatibility import malformed_alternative
from tests.unit.test_qq_trial_wire import cursor


@pytest.mark.parametrize("change", ["both_defects", "from_duplicate", "to_group", "missing_date", "cc", "two_references", "multiple_reference_ids"])
async def test_associated_unsafe_candidate_stops_without_body_or_new_cursor(change):
    raw = mail(reply=ALIAS)
    if change == "both_defects":
        raw = malformed_alternative()
    elif change == "from_duplicate":
        raw = mail(reply=ALIAS, extra=f"From: {FROM}\r\n")
    elif change == "to_group":
        raw = mail(reply=ALIAS, recipient=f"Group: {TO};")
    elif change == "missing_date":
        raw = raw.replace(b"Date:", b"X-Date:")
    elif change == "cc":
        raw = mail(reply=ALIAS, extra="Cc: 111111111@qq.com\r\n")
    elif change == "two_references":
        raw = mail(reply=ALIAS, extra="In-Reply-To: <other@qq.com>\r\n")
    else:
        raw = mail(reply=ALIAS + " <other@qq.com>")
    gate, session = Gate(), Session({1: raw})
    reader = QQInboundReader(ROUTE, EMPLOYEE, session, Scope(), address=TO, trial_gate=gate)
    original = cursor(gate)
    with pytest.raises(InboundError):
        await reader.fetch_inbound_page(ROUTE.mailbox_alias, original, 20)
    assert gate.stopped == "reply_headers_unsafe" and not session.downloads
    opens = session.opens
    with pytest.raises(InboundError):
        await reader.fetch_inbound_page(ROUTE.mailbox_alias, original, 20)
    assert session.opens == opens


@pytest.mark.parametrize("raw", [
    mail(reply="<unrelated@qq.com>", extra=f"From: {FROM}\r\n"),
    mail(reply=ALIAS, sender="111111111@qq.com", extra=f"To: {TO}\r\n"),
    mail(reply=ALIAS, recipient="111111111@qq.com", extra=f"From: {FROM}\r\n"),
    mail(reply="prefix" + ALIAS, extra=f"From: {FROM}\r\n"),
])
async def test_definitely_unrelated_candidates_do_not_hold_trial(raw):
    gate, session = Gate(), Session({1: raw})
    session.search_result = (1,)
    reader = QQInboundReader(ROUTE, EMPLOYEE, session, Scope(), address=TO, trial_gate=gate)
    page = await reader.fetch_inbound_page(ROUTE.mailbox_alias, cursor(gate), 20)
    assert not page.items and not session.downloads and gate.stopped is None
    assert decode_cursor(page.next_cursor, ROUTE).last_uid == 1
