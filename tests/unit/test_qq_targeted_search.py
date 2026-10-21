"""指定Attempt定向查询；所有IMAP命令均由内存端口接收，绝不连接QQ。"""
from dataclasses import replace

import pytest

from connectors.qq_mail.inbound import initial_cursor
from shared.schemas.email_inbound import InboundError
from tests.unit.test_qq_inbound import (
    ATTEMPT,
    OUTBOUND,
    ROUTE,
    Scope,
    Session,
    mail,
    reader,
)


async def test_missing_target_never_opens_mailbox():
    scope = Scope()
    scope.grant = None
    session = Session({1: mail()})
    with pytest.raises(InboundError):
        await reader(session, scope).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert session.opens == 0 and not session.searches and not session.read_headers


@pytest.mark.parametrize("raw", [
    mail(reply=OUTBOUND + " <other@qq.com>"),
    mail(reply="<other@qq.com>", extra="References: " + OUTBOUND + "\r\n"),
    mail(reply="<qq-test." + "b" * 64 + "@messages.tradeos.invalid>"),
])
async def test_server_false_positive_or_references_never_downloads_body(raw):
    session = Session({1: raw})
    session.search_result = (1,)
    page = await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert not page.items and session.read_headers == [1] and not session.downloads


async def test_target_change_rejects_old_cursor_before_mailbox():
    session = Session({1: mail()})
    first = await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    scope = Scope()
    scope.grant = replace(scope.grant, attempt_id="mat_01ARZ3NDEKTSV4RRFFQ69G5FAW")
    with pytest.raises(InboundError):
        await reader(session, scope).fetch_inbound_page("qikai", first.next_cursor, 20)
    assert session.opens == 1 and session.downloads == [1]


@pytest.mark.parametrize("result", [
    tuple(range(1, 102)), (0,), (999,), (2, 1), (1, 1), (True,), [1],
])
async def test_bad_or_excessive_search_result_never_fetches_headers(result):
    session = Session({1: mail(), 2: mail()})
    session.search_result = result
    with pytest.raises(InboundError):
        await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert not session.read_headers and not session.downloads


@pytest.mark.parametrize("failure", ["unsupported", "network", "validity", "revoked"])
async def test_search_failure_or_changed_authority_has_no_scan_fallback(failure):
    scope = Scope()
    class Changed(Session):
        async def search_replies(self, *args):
            if failure == "unsupported":
                raise InboundError()
            if failure == "network":
                raise OSError("fixture unavailable")
            value = await super().search_replies(*args)
            if failure == "validity":
                self.validity += 1
            if failure == "revoked":
                scope.grant = None
            return value
    session = Changed({1: mail()})
    with pytest.raises(InboundError):
        await reader(session, scope).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert not session.read_headers and not session.downloads


async def test_exact_server_query_and_bounded_response():
    from connectors.qq_mail.inbound_transport import _Session
    commands = []
    class Connection:
        def uid(self, *args):
            commands.append(args)
            return "OK", [b"2 9"]
    result = await _Session(Connection()).search_replies(Scope().grant, 1, 10)
    assert result == (2, 9)
    assert commands == [("SEARCH", None, "UID", "2:10",
        "HEADER", "In-Reply-To", '"' + OUTBOUND + '"')]


@pytest.mark.parametrize("status,data", [
    ("NO", [b""]), ("BAD", [b""]), ("OK", [None]), ("OK", [b"1", b"2"]),
    ("OK", [b"1 " * 101]), ("OK", [b"1 1"]), ("OK", [b"2 1"]),
    ("OK", [b"0"]), ("OK", [b"01"]), ("OK", [b"11"]), ("OK", [b"1:*"]),
])
async def test_transport_search_rejects_unsupported_or_invalid_response(status, data):
    from connectors.qq_mail.inbound_transport import _Session
    calls = []
    class Connection:
        def uid(self, *args):
            calls.append(args)
            return status, data
    with pytest.raises(InboundError):
        await _Session(Connection()).search_replies(Scope().grant, 0, 10)
    assert len(calls) == 1 and calls[0][0] == "SEARCH"


def test_qq_attempt_is_explicit_and_validated(tmp_path):
    from infra.standalone.settings import QQReplySettings
    from shared.schemas.identifiers import new_id
    raw = {
        "employee_id": new_id("emp"), "user_id": new_id("usr"),
        "classifier_employee_id": new_id("emp"), "mailbox_directory": str(tmp_path),
    }
    assert QQReplySettings.model_validate(raw).attempt_id is None
    for bad in ["", "other", "<message@qq.com>", 123]:
        with pytest.raises(ValueError):
            QQReplySettings.model_validate({**raw, "attempt_id": bad})
    attempt = new_id("mat")
    assert QQReplySettings.model_validate({**raw, "attempt_id": attempt}).attempt_id == attempt


def test_initial_gateway_cursor_cannot_alias_another_attempt():
    from connectors.qq_mail.inbound import decode_cursor
    first = initial_cursor(ROUTE, attempt_id=ATTEMPT)
    second = initial_cursor(ROUTE, attempt_id="mat_01ARZ3NDEKTSV4RRFFQ69G5FAW")
    assert first != second
    assert decode_cursor(first, ROUTE).attempt_id == ATTEMPT
    with pytest.raises(InboundError):
        initial_cursor(ROUTE, attempt_id=None)


def test_composition_rejects_cursor_for_other_target_without_io(tmp_path):
    from apps.composition_support.email_inbound import qq_adapter
    from infra.standalone.settings import QQReplySettings
    cfg = QQReplySettings(employee_id="emp_fixture", user_id="usr_fixture",
        classifier_employee_id="emp_boss", mailbox_directory=tmp_path, attempt_id=ATTEMPT)
    adapter = qq_adapter(cfg, ROUTE.tenant_id, None)
    first = adapter.initial(ROUTE, None, 0)
    adapter.validate(first, ROUTE)
    other = initial_cursor(ROUTE, attempt_id="mat_01ARZ3NDEKTSV4RRFFQ69G5FAW")
    with pytest.raises(InboundError):
        adapter.validate(other, ROUTE)
    disabled = qq_adapter(cfg.model_copy(update={"attempt_id": None}), ROUTE.tenant_id, None)
    with pytest.raises(InboundError):
        disabled.initial(ROUTE, None, 0)
    with pytest.raises(InboundError):
        disabled.validate(first, ROUTE)



async def test_continuous_queries_two_individual_headers_and_deduplicates_uids():
    from connectors.qq_mail.inbound_transport import _Session
    calls = []
    class Connection:
        def uid(self, *args):
            calls.append(args)
            return "OK", [b"2 5" if args[-2] == "In-Reply-To" else b"5 9"]
    target = replace(Scope().grant, provider_header="<verified@qq.com>",
        outbound_message_header="<verified@qq.com>")
    assert await _Session(Connection(), verified_alias=True).search_replies(target,1,10) == (2,5,9)
    assert [call[-2] for call in calls] == ["In-Reply-To","References"]
    assert all(call[:4] == ("SEARCH",None,"UID","2:10") for call in calls)
