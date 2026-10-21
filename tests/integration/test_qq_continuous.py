"""服务器独立测试库：历史试验停止后沿原 Gateway、事务和游标连续接收。"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from connectors.qq_mail.inbound import QQCursor, encode_cursor
from infra.db.qq_continuous import PostgresContinuousReplyScope
from infra.db.qq_trial_tables import QQTrialRow
from infra.db.tables import (
    EmailInboundReceiptRow,
    MessageRow,
    OutboxEventRow,
    ToolCallRow,
)
from infra.standalone.settings import QQReplySettings
from shared.schemas.email_inbound import InboundError
from tests.integration import qq_reply_chain as c
from tests.integration.test_qq_trial_ledger import trial as trial  # noqa: PLC0414


async def continuous(trial, tmp_path, *, stopped=True):
    gate, sessions, base, *_ = trial
    scope = gate.scope
    if stopped:
        await gate.stop("cancelled")
    async with sessions.begin() as session:
        await session.execute(update(ToolCallRow).where(
            ToolCallRow.tenant_id == scope.route.tenant_id,
            ToolCallRow.tool_call_id == trial[-1]).values(tool_version="v1"))
    settings = QQReplySettings.model_validate({
        "employee_id": scope.employee_id, "user_id": scope.user_id,
        "classifier_employee_id": scope.classifier_employee_id, "mailbox_directory": tmp_path,
        "attempt_id": scope.attempt_id, "continuous": {"route": scope.route.model_dump(),
            "source_trial_id": scope.trial_id, "alias_evidence_id": scope.alias_evidence_id,
            "authorization_ref": "explicit-offline-fixture"}})
    return PostgresContinuousReplyScope(sessions, base, settings)


@pytest.mark.parametrize("change", ["none", "mailbox", "active_trial", "failed_collector", "cross_tenant"])
async def test_continuous_rechecks_current_scope_and_original_provenance(trial, tmp_path, change):
    gate, sessions, _, state, *_ = trial
    scope = gate.scope
    current = await continuous(trial, tmp_path, stopped=change != "active_trial")
    if change == "mailbox":
        state["mailbox"] = False
    elif change == "failed_collector":
        async with sessions.begin() as s:
            await s.execute(update(ToolCallRow).where(ToolCallRow.tenant_id == scope.route.tenant_id,
                ToolCallRow.tool_call_id == trial[-1]).values(status="failed_permanent",
                    error_category="provider_permanent", provider_ref=None))
    route = scope.route
    if change == "cross_tenant":
        route = route.model_copy(update={"tenant_id": c.new_id("tn")})
    if change in ("active_trial", "failed_collector", "cross_tenant"):
        with pytest.raises(InboundError):
            await current.target(route, scope.employee_id)
    else:
        grant = await current.target(route, scope.employee_id)
        assert (grant is None) == (change == "mailbox")
        if grant:
            assert grant.provider_header == scope.provider_header


@pytest.mark.parametrize("reconcile", [False, True])
async def test_restart_delayed_mail_and_repeated_page_are_exactly_once(trial, tmp_path, reconcile):
    gate, sessions, *_ = trial
    scope, now = gate.scope, lambda: datetime.now(UTC)
    tenant, route = scope.route.tenant_id, scope.route
    current = await continuous(trial, tmp_path)
    mailbox = c.Session({11: c.mail(reply=scope.provider_header)})
    blobs = c.Blobs()
    raw = c.RawArtifactStoreImpl(lambda t: c.SqlAlchemyArtifactUnitOfWork(sessions, t),
        blobs, c.MIME_BYTES, now, c.new_id, bounded_transport=blobs)
    slot, fingerprints = c.InboundPageSlot(), c.HmacFingerprintProvider("v1", b"f"*32)
    from connectors.qq_mail.reconciliation import QQContinuousReconciliationReader
    def provider():
        raw_reader = c.QQInboundReader(route, scope.employee_id, mailbox, current, address=scope.sender)
        return QQContinuousReconciliationReader(raw_reader, uidvalidity=7, uid=11) if reconcile else raw_reader
    registry = c.ToolRegistry()
    registry.register(c.MANIFEST, c.EmailInboundFetchHandler(route,
        provider,
        c.InboundRawArtifactArchiver(raw, raw), c.CredentialMarkerGuard(), slot, fingerprints,
        cursor_validator=c.decode_cursor))
    async def allowed(ctx, state):
        return ctx.tenant_id == tenant and ctx.user_id == scope.user_id
    gateway = c.ToolGateway(registry, {"tenant": c.InboundTenantCheck(route),
        "permission": c.PermissionCheck(allowed)},
        lambda t: c.SqlAlchemyToolGatewayUnitOfWork(sessions, t, now=now),
        lease_duration=timedelta(seconds=60), lease_owner="continuous-fixture", now=now, id_factory=c.new_id)
    reader = c.ToolGatewayEmailInboundReader(gateway, slot, scope.user_id, route, cursor_validator=c.decode_cursor)
    store = c.InboundStore(sessions, tenant, route.mailbox_alias, now=now)
    await store.bind(route, encode_cursor(QQCursor(route=route, attempt_id=scope.attempt_id,
        outbound_message_header=scope.canonical_header, uidvalidity=7, last_uid=11 if reconcile else 10)),
        now(), int(now().timestamp()), scope.employee_id)
    def outreach(factory, audit):
        return c.OutreachServiceImpl(factory, None, None, None, None,
            c.Phase1OutreachAuthorizer(tenant), audit, now=now)
    def actor(identity):
        return c.OA("system:email-inbound", c.OutreachScope(level=c.ScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity})), "system")
    def processor(store):
        return c.InboundPageProcessor(store, lambda expected: c.SqlAlchemyInboundPageUnitOfWork(
            store, expected, outreach_builder=outreach, audit_sink=c.StandardAuditLogger()), actor)
    first = await store.read_cursor()
    page = await reader.fetch(tenant, route.mailbox_alias, first.cursor, 20)
    await processor(store).process(first, page)
    # 重复交付不会产生第二条 Message/事件；游标已提交的重放只核验已有事实。
    await processor(store).process(first, page)
    fresh_store = c.InboundStore(sessions, tenant, route.mailbox_alias, now=now)
    saved = await fresh_store.read_cursor()
    assert c.decode_cursor(saved.cursor, route).last_uid == 11
    reconcile = False
    mailbox.messages[12] = c.mail(reply="<reply1@qq.com>",
        extra=f"References: {scope.provider_header} <reply1@qq.com>\r\n",
        body="Delayed after restart").replace(b"Message-ID: <reply1@", b"Message-ID: <reply2@")
    mailbox.next_uid = 13
    driver = c.InboundDriver(SimpleNamespace(store=fresh_store, reader_for=lambda route: reader,
        processor=processor(fresh_store), trial_gate=None), now=now)
    assert await driver.scan_once() == 1
    assert await driver.scan_once() == 1
    assert mailbox.downloads == [11, 12]
    async with sessions() as s:
        for table, conditions in [
            (MessageRow, [MessageRow.direction == "inbound"]),
            (EmailInboundReceiptRow, []),
            (OutboxEventRow, [OutboxEventRow.event_type == "InboundMessageStored"]),
        ]:
            assert await s.scalar(select(func.count()).select_from(table).where(
                table.tenant_id == tenant, *conditions)) == 2
        old = await s.scalar(select(QQTrialRow).where(QQTrialRow.tenant_id == tenant,
            QQTrialRow.trial_id == scope.trial_id))
        assert old.state == "stopped" and old.bodies == 0
