"""QQ 入站隐私/游标回归；所有邮件和权限事实均为 fixture。"""
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from connectors.qq_mail.inbound import (
    HeaderResult,
    ProjectReplyGrant,
    QQInboundReader,
    decode_cursor,
    initial_cursor,
)
from shared.schemas.email_inbound import InboundError, InboundRoute
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    SendingIdentityId,
    TenantId,
)

ROUTE = InboundRoute(
    tenant_id=TenantId("tn_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
    configured_identity_id=SendingIdentityId("sid_01ARZ3NDEKTSV4RRFFQ69G5FAV"),
    mailbox_alias="qikai", route_id="qq-test", config_version="v1",
)
EMPLOYEE = EmployeeId("emp_fixture_qikai")
OUTBOUND = "<qq-test." + "a" * 64 + "@messages.tradeos.invalid>"
ATTEMPT = "mat_01ARZ3NDEKTSV4RRFFQ69G5FAV"
FROM = "986647123@qq.com"
TO = "1249583590@qq.com"

def mail(*, sender=FROM, recipient=TO, reply=OUTBOUND, extra="", body="Please add my WhatsApp."):
    return (
        f"Message-ID: <reply1@qq.com>\r\nIn-Reply-To: {reply}\r\n"
        f"From: {sender}\r\nTo: {recipient}\r\n"
        "Date: Thu, 15 Oct 2026 10:00:00 +0000\r\n"
        f"{extra}Content-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    ).encode()

class Session:
    def __init__(self, messages):
        self.messages = messages
        self.validity = 7
        self.next_uid = max(messages, default=0) + 1
        self.downloads = []
        self.read_headers = []
        self.changed = None
        self.searches = []
        self.opens = 0
        self.search_result = None
    async def snapshot(self):
        return self.validity, self.next_uid
    async def search_replies(self, target, after_uid, before_uid):
        from email.parser import BytesHeaderParser
        self.searches.append((target.attempt_id, target.outbound_message_header, after_uid, before_uid))
        if self.search_result is not None:
            return self.search_result
        def matches(raw):
            h = BytesHeaderParser().parsebytes(raw.split(b"\r\n\r\n")[0])
            return ((target.outbound_message_header.lower() in str(h.get("In-Reply-To", "")).lower()
                or (target.provider_header is not None and target.outbound_message_header.lower() in str(h.get("References", "")).lower()))
                and target.recipient in str(h.get("From", ""))
                and target.sender in str(h.get("To", "")))
        return tuple(uid for uid, raw in sorted(self.messages.items())
            if after_uid < uid <= before_uid and matches(raw))
    async def headers(self, uid):
        self.read_headers.append(uid)
        if uid not in self.messages:
            return None
        raw = self.messages[uid]
        return HeaderResult(uid, len(raw), raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n")
    async def body(self, uid, size):
        self.downloads.append(uid)
        return self.changed or self.messages[uid]
    @asynccontextmanager
    async def session(self):
        self.opens += 1
        yield self

class Scope:
    def __init__(self):
        self.grant = ProjectReplyGrant(
            ROUTE, EMPLOYEE, MessageId("msg_fixture"),
            OUTBOUND, TO, FROM, ATTEMPT,
        )
        self.calls = 0
        self.revoke = False
    async def target(self, route, employee):
        return self.grant
    async def resolve(self, route, employee, headers):
        self.calls += 1
        if self.revoke and self.calls > 1:
            return None
        if headers.in_reply_to != OUTBOUND or headers.sender != FROM:
            return None
        return self.grant

def reader(session, scope=None):
    return QQInboundReader(ROUTE, EMPLOYEE, session, scope or Scope(), address=TO)

@pytest.mark.parametrize("raw", [
    mail(reply="<personal@qq.com>"),
    mail(sender="555555555@qq.com"),
    mail(recipient="111111111@qq.com"),
    mail(extra="From: 986647123@qq.com\r\n"),
    mail(extra="Cc: 123456789@qq.com\r\n"),
    mail(reply="<qq-test." + "b" * 64 + "@messages.tradeos.invalid>"),
    mail().replace(b"In-Reply-To:", b"X-Unrelated:"),
])
async def test_unrelated_or_ambiguous_headers_never_download_body(raw):
    session = Session({1: raw})
    page = await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert page.items == ()
    assert session.downloads == []
    assert decode_cursor(page.next_cursor, ROUTE).last_uid == 1

async def test_authorized_reply_replay_and_restart_have_stable_identity():
    session = Session({1: mail()})
    cursor = initial_cursor(ROUTE, attempt_id=ATTEMPT)
    a = await reader(session).fetch_inbound_page("qikai", cursor, 20)
    b = await reader(session).fetch_inbound_page("qikai", cursor, 20)
    assert a.next_cursor == b.next_cursor
    assert a.items[0].provider_ref_digest == b.items[0].provider_ref_digest
    assert a.items[0].body == "Please add my WhatsApp."
    c = await reader(session).fetch_inbound_page("qikai", a.next_cursor, 20)
    assert c.items == ()
    assert session.downloads == [1, 1]  # 未提交重试可重读；已提交游标不重复读取。

@pytest.mark.parametrize("change", ["tenant", "employee", "identity"])
async def test_current_scope_binding_mismatch_never_downloads(change):
    scope = Scope()
    if change == "employee":
        scope.grant = replace(scope.grant, employee_id=EmployeeId("emp_other"))
    else:
        key = "tenant_id" if change == "tenant" else "configured_identity_id"
        value = ("tn_" if change == "tenant" else "sid_") + "01ARZ3NDEKTSV4RRFFQ69G5FAW"
        scope.grant = replace(scope.grant, route=ROUTE.model_copy(update={key: value}))
    session = Session({1: mail()})
    with pytest.raises(InboundError):
        await reader(session, scope).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert session.downloads == []

async def test_revocation_or_body_header_change_never_returns_archive_candidate():
    for revoked in (True, False):
        scope = Scope()
        scope.revoke = revoked
        session = Session({1: mail()})
        if not revoked:
            session.changed = mail(sender="111111111@qq.com")
        with pytest.raises(InboundError):
            await reader(session, scope).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)

async def test_uidvalidity_reset_does_not_reinitialize_or_download():
    session = Session({1: mail()})
    page = await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    session.validity = 8
    with pytest.raises(InboundError):
        await reader(session).fetch_inbound_page("qikai", page.next_cursor, 20)
    assert session.downloads == [1]

async def test_stable_window_and_uid_gaps():
    session = Session({1: mail(), 3: mail()})
    first = await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 1)
    session.messages[4] = mail()
    session.next_uid = 5
    second = await reader(session).fetch_inbound_page("qikai", first.next_cursor, 20)
    assert decode_cursor(second.next_cursor, ROUTE).last_uid == 3
    third = await reader(session).fetch_inbound_page("qikai", second.next_cursor, 20)
    assert decode_cursor(third.next_cursor, ROUTE).last_uid == 4
    assert session.read_headers == [1, 3, 4]
    assert session.searches[0][1] == OUTBOUND

@pytest.mark.parametrize("cursor", ["", "gic1.bad", "qic1.not-base64", "qic1." + "x" * 40000])
async def test_bad_cursor_fails_before_mailbox_access(cursor):
    session = Session({1: mail()})
    with pytest.raises(InboundError):
        await reader(session).fetch_inbound_page("qikai", cursor, 20)
    assert session.read_headers == []

async def test_gateway_archives_only_authorized_qq_candidate():
    import hashlib

    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from shared.schemas.email_inbound import ArchivedInboundRaw
    from shared.schemas.identifiers import ArtifactId, UserId, new_id
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.handlers.email_inbound import EmailInboundFetchHandler
    from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
    from tool_gateway.pipeline import ToolCallContext

    archived = []
    class Archiver:
        async def archive(self, tenant_id, raw, *, maximum_bytes):
            archived.append(raw)
            return ArchivedInboundRaw(tenant_id=tenant_id, artifact_id=ArtifactId(new_id("art")),
                content_hash=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    session = Session({1: mail(reply="<personal@qq.com>", body="Private unrelated"),
                       2: mail(body="Please quote 500 units.")})
    slot = InboundPageSlot()
    handler = EmailInboundFetchHandler(ROUTE, lambda: reader(session),
        Archiver(), CredentialMarkerGuard(), slot, HmacFingerprintProvider("v1", b"x" * 32),
        cursor_validator=decode_cursor)
    context = ToolCallContext(tenant_id=ROUTE.tenant_id, user_id=UserId(new_id("usr")),
        tool_id="email.inbound.fetch", params={
            "mailbox_alias": "qikai", "cursor": initial_cursor(ROUTE, attempt_id=ATTEMPT), "page_limit": 20,
        })
    result = await handler.execute(ROUTE.tenant_id, await handler.prepare(context, None))
    page = slot.take(result["provider_ref"])
    assert len(page.items) == 1 and page.items[0].raw is not None
    assert archived == [session.messages[2]]
    assert session.downloads == [2]
    assert "Private unrelated" not in repr(page)

async def test_unrelated_large_message_does_not_block_later_project_reply():
    class Large(Session):
        async def headers(self, uid):
            result = await super().headers(uid)
            return replace(result, size=20 * 1024 * 1024) if uid == 1 else result
    session = Large({1: mail(reply="<unrelated@qq.com>"), 2: mail()})
    page = await reader(session).fetch_inbound_page("qikai", initial_cursor(ROUTE, attempt_id=ATTEMPT), 20)
    assert session.downloads == [2] and len(page.items) == 1

async def test_imap_cancel_waits_for_body_before_logout(monkeypatch):
    import asyncio
    import threading

    import connectors.qq_mail.inbound_transport as module
    entered, release = threading.Event(), threading.Event()
    order = []
    class Connection:
        def login(self, *args): return "OK", []
        def select(self, *args, **kwargs):
            assert kwargs == {"readonly": True}
            return "OK", []
        def uid(self, *args):
            order.append("body-start"); entered.set()
            assert release.wait(3)
            order.append("body-end")
            return "OK", [(b"1 (UID 1 BODY[] {4}", b"body"), b")"]
        def logout(self): order.append("logout")
    class Secrets:
        def resolve(self, ref): return "fixture-password"
    monkeypatch.setattr(module, "_BoundedIMAP", Connection)
    async def consume():
        async with module.QQImapInboundTransport(TO, Secrets(), "fixture").session() as session:
            await session.body(1, 4)
    task = asyncio.create_task(consume())
    for _ in range(100):
        if entered.is_set(): break
        await asyncio.sleep(.01)
    assert entered.is_set()
    task.cancel()
    await asyncio.sleep(.01)
    assert order == ["body-start"]
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert order == ["body-start", "body-end", "logout"]

async def test_imap_cancel_during_open_closes_returned_connection(monkeypatch):
    import asyncio
    import threading

    import connectors.qq_mail.inbound_transport as module
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Connection:
        def __init__(self):
            entered.set()
            assert release.wait(3)
        def shutdown(self): closed.set()
        def login(self, *args): pytest.fail("cancelled connection must not authenticate")
    class Secrets:
        def resolve(self, ref): return "fixture-password"
    monkeypatch.setattr(module, "_BoundedIMAP", Connection)
    async def consume():
        async with module.QQImapInboundTransport(TO, Secrets(), "fixture").session(): pass
    task = asyncio.create_task(consume())
    for _ in range(100):
        if entered.is_set(): break
        await asyncio.sleep(.01)
    assert entered.is_set()
    task.cancel(); release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert closed.is_set()

@pytest.mark.parametrize("metadata", [
    b"1 (UID 9 RFC822.SIZE 30 BODY[HEADER] {30}",
    b"1 (RFC822.SIZE 30 BODY[HEADER] {30}",
    b"1 (UID 1 UID 1 RFC822.SIZE 30 BODY[HEADER] {30}",
])
async def test_transport_rejects_missing_ambiguous_or_wrong_uid(metadata):
    from connectors.qq_mail.inbound_transport import _Session
    class Connection:
        def uid(self, *args):
            assert args[0] == "FETCH" and "BODY.PEEK[HEADER.FIELDS" in args[2]
            return "OK", [(metadata, b"fake headers"), b")"]
    with pytest.raises(InboundError):
        await _Session(Connection()).headers(1)
