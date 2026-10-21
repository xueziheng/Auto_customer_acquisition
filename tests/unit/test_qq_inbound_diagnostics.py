"""阶段、异常链、协议白名单及任务隔离；所有输入均为合成数据。"""
import asyncio
import imaplib
import ssl
import traceback

import pytest

from connectors.qq_mail import inbound as inbound_module
from connectors.qq_mail import inbound_transport as transport_module
from connectors.qq_mail.inbound import QQInboundReader, initial_cursor
from shared.schemas.email_inbound import InboundError
from shared.schemas.email_inbound_diagnostics import (
    DiagnosedInboundError,
    InboundDiagnostic,
    InboundFailureKind,
    InboundProtocolStatus,
    InboundStage,
    inbound_stage,
    require_protocol_ok,
)
from tests.unit.qq_synthetic_imap import MARKER, synthetic_imap
from tests.unit.test_qq_inbound import (
    ATTEMPT,
    EMPLOYEE,
    ROUTE,
    TO,
    Scope,
    Session,
    mail,
    reader,
)
from tool_gateway.handlers.email_inbound_diagnostics import InboundDiagnosticSlot


def assert_safe(error, stage, kind=None, status=None):
    assert type(error) is DiagnosedInboundError
    assert error.diagnostic.stage is stage
    if kind is not None:
        assert error.diagnostic.kind is kind
    if status is not None:
        assert error.diagnostic.protocol_status is status
    assert error.__context__ is None and error.__cause__ is None
    # 不包括测试源代码行，避免把故意植入的fixture字符串当作运行时泄漏。
    assert MARKER not in str(error) + repr(error) + repr(vars(error))
    assert "SYNTHETIC-SECRET" not in "".join(traceback.format_exception_only(error))
    assert 0 <= error.diagnostic.duration_ms <= 86_400_000


@pytest.mark.parametrize("stage", [
    InboundStage.AUTHENTICATE, InboundStage.SELECT, InboundStage.UID_STATE,
    InboundStage.SEARCH, InboundStage.HEADERS, InboundStage.BODY,
])
async def test_raw_imap_failure_reports_exact_stage_without_raw_text(monkeypatch, caplog, stage):
    with synthetic_imap(monkeypatch, fail=stage.value) as (transport, commands):
        qq = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO)
        with pytest.raises(DiagnosedInboundError) as caught:
            await qq.fetch_inbound_page(ROUTE.mailbox_alias, initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
        assert_safe(caught.value, stage, InboundFailureKind.PROTOCOL,
                    InboundProtocolStatus.UNKNOWN if stage is InboundStage.AUTHENTICATE
                    else InboundProtocolStatus.NO)
        assert commands[-1] == "LOGOUT"
        assert "SELECT" not in commands
        assert "SYNTHETIC-SECRET" not in caplog.text


async def test_raw_imap_success_preserves_targeted_read_and_cursor(monkeypatch):
    with synthetic_imap(monkeypatch) as (transport, commands):
        qq = QQInboundReader(ROUTE, EMPLOYEE, transport, Scope(), address=TO)
        page = await qq.fetch_inbound_page(ROUTE.mailbox_alias, initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
        assert len(page.items) == 1
        assert page.items[0].body == "Please add my WhatsApp."
        assert commands.count("UID") == 3
        assert "EXAMINE" in commands and commands[-1] == "LOGOUT"


@pytest.mark.parametrize(("failure", "kind"), [
    (TimeoutError(MARKER), InboundFailureKind.TIMEOUT),
    (ssl.SSLError(MARKER), InboundFailureKind.TLS),
    (OSError(MARKER), InboundFailureKind.NETWORK),
    (imaplib.IMAP4.error(MARKER), InboundFailureKind.PROTOCOL),
    (InboundError(), InboundFailureKind.INTEGRITY),
    (RuntimeError(MARKER), InboundFailureKind.UNEXPECTED),
])
async def test_connection_error_has_no_original_exception_chain(monkeypatch, failure, kind):
    class Secrets:
        def resolve(self, ref):
            return "synthetic-only"
    def fail():
        raise failure
    monkeypatch.setattr(transport_module, "_BoundedIMAP", fail)
    with pytest.raises(DiagnosedInboundError) as caught:
        async with transport_module.QQImapInboundTransport(TO, Secrets(), "fixture").session():
            pytest.fail("不可交付会话")
    assert_safe(caught.value, InboundStage.CONNECT, kind)


async def test_credential_failure_never_connects(monkeypatch):
    class Secrets:
        def resolve(self, ref):
            raise RuntimeError(MARKER)
    def forbidden():
        pytest.fail("凭证失败后不得连接")
    monkeypatch.setattr(transport_module, "_BoundedIMAP", forbidden)
    with pytest.raises(DiagnosedInboundError) as caught:
        async with transport_module.QQImapInboundTransport(TO, Secrets(), "fixture").session():
            pytest.fail("不可交付会话")
    assert_safe(caught.value, InboundStage.CREDENTIAL)


@pytest.mark.parametrize("stage", [InboundStage.TARGET, InboundStage.AUTHORIZE, InboundStage.PARSE])
async def test_scope_and_parse_failures_keep_stage(monkeypatch, stage):
    scope, session = Scope(), Session({1: mail()})
    async def fail(*args):
        raise RuntimeError(MARKER)
    def parse_fail(*args):
        raise RuntimeError(MARKER)
    if stage is InboundStage.TARGET:
        scope.target = fail
    elif stage is InboundStage.AUTHORIZE:
        scope.resolve = fail
    else:
        monkeypatch.setattr(inbound_module, "parse_inbound_message", parse_fail)
    with pytest.raises(DiagnosedInboundError) as caught:
        await reader(session, scope).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert_safe(caught.value, stage)
    if stage is not InboundStage.PARSE:
        assert session.downloads == []


@pytest.mark.parametrize("status", ["NO", "BAD", "BYE", "unknown\r\nSYNTHETIC-SECRET", b"NO", None])
def test_protocol_status_is_an_allowlist(status):
    with pytest.raises(DiagnosedInboundError) as caught, inbound_stage(InboundStage.SEARCH):
        require_protocol_ok(status)
    expected = {"NO": "no", "BAD": "bad", "BYE": "bye"}.get(status, "unknown")
    assert caught.value.diagnostic.protocol_status.value == expected
    assert_safe(caught.value, InboundStage.SEARCH)


def test_hostile_protocol_objects_are_never_stringified_or_compared():
    class Hostile:
        def __str__(self): raise AssertionError("str")
        def __repr__(self): raise AssertionError("repr")
        def __hash__(self): raise AssertionError("hash")
        def __eq__(self, other): raise AssertionError("eq")
    with pytest.raises(DiagnosedInboundError) as caught, inbound_stage(InboundStage.SEARCH) as observation:
        observation.protocol(Hostile())
        require_protocol_ok(Hostile())
    assert_safe(caught.value, InboundStage.SEARCH, status=InboundProtocolStatus.UNKNOWN)


async def test_slot_is_capacity_one_task_owned_and_take_consumes():
    slot = InboundDiagnosticSlot()
    first = InboundDiagnostic(InboundStage.CONNECT, InboundFailureKind.TIMEOUT, InboundProtocolStatus.UNKNOWN, 5)
    second = InboundDiagnostic(InboundStage.SEARCH, InboundFailureKind.PROTOCOL, InboundProtocolStatus.NO, 6)
    slot.put(first)
    async def child():
        assert slot.take() is None
        slot.put(second)
        assert slot.take() == second
        assert slot.take() is None
    await asyncio.create_task(child())
    assert slot.take() == first
    assert slot.take() is None
    slot.put(first)
    slot.put(second)
    assert slot.take() == second


@pytest.mark.parametrize("value", [-1, 86_400_001, True, "9"])
def test_diagnostic_rejects_unbounded_or_untyped_duration(value):
    with pytest.raises(ValueError, match="入站诊断字段无效"):
        InboundDiagnostic(InboundStage.PAGE, InboundFailureKind.UNEXPECTED, InboundProtocolStatus.UNKNOWN, value)


async def test_cancellation_stays_cancellation():
    with pytest.raises(asyncio.CancelledError), inbound_stage(InboundStage.BODY):
        raise asyncio.CancelledError()


@pytest.mark.parametrize("outcome", ["cancel", "rejected", "success"])
async def test_wrapper_clears_diagnostic_on_cancel_denial_and_success(outcome):
    from shared.schemas.email_inbound import ArchivedInboundPage
    from shared.schemas.identifiers import UserId, new_id
    from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
    from tool_gateway.handlers.email_inbound import ToolGatewayEmailInboundReader
    from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
    from tool_gateway.pipeline import CheckRejection, ToolCallResult

    diagnostics, pages, records = InboundDiagnosticSlot(), InboundPageSlot(), []
    diagnostic = InboundDiagnostic(InboundStage.PAGE, InboundFailureKind.UNEXPECTED,
                                   InboundProtocolStatus.UNKNOWN, 1)
    cursor = initial_cursor(ROUTE, attempt_id=ATTEMPT)
    class Sink:
        async def record(self, call_id, value):
            records.append((call_id, value))
    class Invoker:
        async def invoke(self, ctx):
            assert diagnostics.take() is None
            diagnostics.put(diagnostic)
            if outcome == "cancel":
                raise asyncio.CancelledError()
            if outcome == "rejected":
                return ToolCallResult(tool_id="email.inbound.fetch", status=ToolCallStatus.REJECTED,
                                      error_category=ToolErrorCategory.PERMISSION_DENIED,
                                      rejected=CheckRejection("permission", "denied", "测试拒绝"))
            page = ArchivedInboundPage(route=ROUTE, starting_cursor=cursor, next_cursor=cursor, items=())
            return ToolCallResult(tool_id="email.inbound.fetch", status=ToolCallStatus.SUCCEEDED,
                                  output={"provider_ref": pages.put(page)})
    diagnostics.put(diagnostic)
    wrapper = ToolGatewayEmailInboundReader(Invoker(), pages, UserId(new_id("usr")), ROUTE,
        cursor_validator=inbound_module.decode_cursor, diagnostics=diagnostics, diagnostic_sink=Sink())
    if outcome == "success":
        assert (await wrapper.fetch(ROUTE.tenant_id, "qikai", cursor, 20)).items == ()
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else ToolGatewayError):
            await wrapper.fetch(ROUTE.tenant_id, "qikai", cursor, 20)
    assert diagnostics.take() is None and records == []


def test_arbitrary_exception_chain_is_removed_and_formatted_error_is_safe():
    with pytest.raises(DiagnosedInboundError) as caught, inbound_stage(InboundStage.PARSE):
        try:
            raise ValueError(MARKER)
        except ValueError as original:
            raise RuntimeError(MARKER) from original
    assert_safe(caught.value, InboundStage.PARSE)
    assert "ValueError:" not in "".join(traceback.format_exception(caught.value))
    assert "RuntimeError:" not in "".join(traceback.format_exception(caught.value))
