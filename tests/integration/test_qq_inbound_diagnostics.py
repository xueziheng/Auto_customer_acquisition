"""原 Gateway、真实隔离 PG 与原游标；只替换合成 IMAP 和失败原件端口。"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from apps.scheduler_worker.inbound_driver import InboundDriver
from connectors.qq_mail.inbound import QQInboundReader, decode_cursor, initial_cursor
from infra.db.qq_inbound import PostgresProjectReplyScope
from infra.db.repositories.email_inbound import InboundStore
from infra.db.session import create_engine_from
from infra.db.tables import (
    ConversationReplyWorkRow,
    MessageRow,
    ModelInvocationRow,
    RawArtifactRow,
    ToolCallEventRow,
    ToolCallRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.email_inbound import InboundError
from shared.schemas.email_inbound_diagnostics import (
    InboundDiagnostic,
    InboundFailureKind,
    InboundProtocolStatus,
    InboundStage,
)
from shared.schemas.identifiers import new_id
from tests.integration.test_manual_email_send_api import _NOW
from tests.integration.test_qq_inbound_scope import seed
from tests.unit.qq_synthetic_imap import MARKER, synthetic_imap
from tests.unit.test_qq_inbound import FROM, TO, mail
from tool_gateway.checks.email_inbound import InboundTenantCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_inbound import (
    MANIFEST,
    EmailInboundFetchHandler,
    ToolGatewayEmailInboundReader,
)
from tool_gateway.handlers.email_inbound_diagnostics import (
    GatewayInboundDiagnosticSink,
    InboundDiagnosticSlot,
)
from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway


@pytest.mark.parametrize(("failure", "mode"), [
    ("authenticate", "on"), ("select_readonly", "on"), ("uid_state", "on"),
    ("search", "on"), ("headers", "on"), ("body", "on"),
    ("search", "off"), ("search", "sink_failure"),
    ("archive", "on"), ("archive", "off"), ("archive", "sink_failure"),
])
async def test_original_gateway_cursor_and_redacted_ledger(db_url, monkeypatch, caplog, failure, mode):
    engine = create_engine_from(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = new_id("tn")
    try:
        route, owner, user, header, attempt, _, _ = await seed(sessions, tenant)
        class Binding:
            async def matches(self, t, e, u, address):
                return (t, e, u, address) == (tenant, owner, user, TO)
        scope = PostgresProjectReplyScope(sessions, route, owner, Binding(), attempt_id=attempt)
        slot = InboundPageSlot()
        diagnostics = InboundDiagnosticSlot() if mode != "off" else None
        now = lambda: _NOW
        factory = lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t, now=now)
        real_sink = GatewayInboundDiagnosticSink(tenant, user, factory, now=now)
        class FailingSink:
            async def record(self, call_id, diagnostic):
                raise RuntimeError(MARKER)
        sink = FailingSink() if mode == "sink_failure" else real_sink
        class Archiver:
            async def archive(self, *args, **kwargs):
                assert failure == "archive", "失败前不应归档"
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT, retry_after_seconds=43)
        class NeverProcess:
            async def process(self, *args):
                pytest.fail("失败不得提交消息、分类或工作项")
        with synthetic_imap(monkeypatch, fail=failure, raw=mail(reply=header)) as (transport, commands):
            registry = ToolRegistry()
            registry.register(MANIFEST, EmailInboundFetchHandler(route,
                lambda: QQInboundReader(route, owner, transport, scope, address=TO),
                Archiver(), CredentialMarkerGuard(), slot, HmacFingerprintProvider("v1", b"x" * 32),
                cursor_validator=decode_cursor, diagnostics=diagnostics))
            async def allowed(ctx, state):
                return ctx.tenant_id == tenant and ctx.user_id == user
            gateway = ToolGateway(registry, {
                "tenant": InboundTenantCheck(route), "permission": PermissionCheck(allowed),
            }, factory, lease_duration=timedelta(seconds=60), lease_owner="synthetic-diagnostic",
                now=now, id_factory=new_id)
            reader = ToolGatewayEmailInboundReader(gateway, slot, user, route,
                cursor_validator=decode_cursor, diagnostics=diagnostics,
                diagnostic_sink=sink if diagnostics is not None else None)
            store = InboundStore(sessions, tenant, route.mailbox_alias, now=now)
            first = await store.bind(route, initial_cursor(route, attempt_id=attempt), _NOW,
                                     int(_NOW.timestamp()), owner)
            driver = InboundDriver(SimpleNamespace(store=store, reader_for=lambda _: reader,
                                                   processor=NeverProcess()), now=now)
            assert await driver.scan_once() == 0
            blocked = await store.read_cursor()
            assert blocked.cursor == first.cursor
            assert blocked.version == first.version + 1
            transient = failure == "archive"
            assert blocked.blocked_reason == ("provider_transient" if transient else "provider_permanent")
            assert blocked.next_retry_at == (_NOW + timedelta(seconds=43) if transient else None)
            before = len(commands)
            assert await driver.scan_once() == 0
            if not transient:
                restart = InboundDriver(SimpleNamespace(
                    store=InboundStore(sessions, tenant, route.mailbox_alias, now=now),
                    reader_for=lambda _: reader, processor=NeverProcess()),
                    now=lambda: _NOW + timedelta(days=1))
                assert await restart.scan_once() == 0
            assert len(commands) == before
            if diagnostics is not None:
                assert diagnostics.take() is None

        async with sessions() as db:
            calls = list((await db.scalars(select(ToolCallRow).where(ToolCallRow.tenant_id == tenant))).all())
            events = list((await db.scalars(select(ToolCallEventRow).where(ToolCallEventRow.tenant_id == tenant))).all())
            assert len(calls) == 1
            call = calls[0]
            assert call.status == ("failed_transient" if failure == "archive" else "failed_permanent")
            assert call.error_category == ("provider_transient" if failure == "archive" else "unexpected")
            diagnostic_events = [e for e in events if e.stage.startswith("inbound.")]
            assert len(diagnostic_events) == int(mode == "on")
            if diagnostic_events:
                event = diagnostic_events[0]
                assert event.stage == "inbound." + failure
                assert event.tool_call_id == call.tool_call_id
                assert event.actor_id == user and event.tenant_id == tenant
                assert event.category == call.error_category
                assert event.rule == ("unexpected:unknown" if failure == "archive" else
                                      "protocol:unknown" if failure == "authenticate" else "protocol:no")
            for model in (MessageRow, ModelInvocationRow, ConversationReplyWorkRow, RawArtifactRow):
                assert await db.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant)) == 0
            serialized = json.dumps([{c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
                                     for row in [*calls, *events]])
            for forbidden in (FROM, TO, header, "Please add my WhatsApp", "SYNTHETIC-SECRET", "FORGED-LOG-LINE", first.cursor):
                assert forbidden not in serialized + caplog.text

        # 现有真实失败调用也不能被其他租户、用户、错误工具或不存在的调用挂接诊断。
        safe = InboundDiagnostic(InboundStage.SEARCH, InboundFailureKind.PROTOCOL, InboundProtocolStatus.NO, 1)
        for bad_sink, call_id in [
            (GatewayInboundDiagnosticSink(new_id("tn"), user, factory, now=now), call.tool_call_id),
            (GatewayInboundDiagnosticSink(tenant, new_id("usr"), factory, now=now), call.tool_call_id),
            (real_sink, new_id("tcl")),
        ]:
            with pytest.raises(InboundError):
                await bad_sink.record(call_id, safe)
    finally:
        await engine.dispose()
