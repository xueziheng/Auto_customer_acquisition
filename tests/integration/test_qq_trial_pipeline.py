"""假邮箱/对象存储，原 Gateway、页事务和原分类/工作项服务的组合验收。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from domains.conversations.schemas import (
    ReplyCategory,
    ReplyWorkAction,
    ReplyWorkActionRequest,
)
from domains.conversations.service_impl import ConversationServiceImpl
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.qq_trial_tables import QQTrialRow
from infra.db.tables import (
    EmailInboundReceiptRow,
    MessageRow,
    OutboxEventRow,
    OutreachMessageAttemptRow,
)
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration import qq_reply_chain as c
from tests.integration.test_qq_trial_ledger import snapshot
from tests.integration.test_qq_trial_ledger import (
    trial as trial,  # noqa: PLC0414 - pytest fixture re-export
)


async def setup_pipeline(trial):
    gate, sessions, current, *_ = trial
    scope = gate.scope
    now = lambda: datetime.now(UTC)
    route = scope.route
    tenant = route.tenant_id
    qq = c.Session({10: c.mail(reply=scope.provider_header)})
    blobs = c.Blobs()
    raw = c.RawArtifactStoreImpl(
        lambda t: c.SqlAlchemyArtifactUnitOfWork(sessions, t),
        blobs,
        c.MIME_BYTES,
        now,
        c.new_id,
        bounded_transport=blobs,
    )
    slot, fingerprints = c.InboundPageSlot(), c.HmacFingerprintProvider("v1", b"f" * 32)
    registry = c.ToolRegistry()
    registry.register(
        c.MANIFEST,
        c.EmailInboundFetchHandler(
            route,
            lambda: c.QQInboundReader(
                route,
                scope.employee_id,
                qq,
                current,
                address=scope.sender,
                trial_gate=gate,
            ),
            c.InboundRawArtifactArchiver(raw, raw, before_archive=gate.check),
            c.CredentialMarkerGuard(),
            slot,
            fingerprints,
            cursor_validator=c.decode_cursor,
        ),
    )

    async def allowed(ctx, state):
        return ctx.tenant_id == tenant and ctx.user_id == scope.user_id

    gateway = c.ToolGateway(
        registry,
        {
            "tenant": c.InboundTenantCheck(route),
            "permission": c.PermissionCheck(allowed),
        },
        lambda t: c.SqlAlchemyToolGatewayUnitOfWork(sessions, t, now=now),
        lease_duration=timedelta(seconds=60),
        lease_owner="offline-trial",
        now=now,
        id_factory=c.new_id,
    )
    reader = c.ToolGatewayEmailInboundReader(
        gateway, slot, scope.user_id, route, cursor_validator=c.decode_cursor
    )
    store = c.InboundStore(sessions, tenant, route.mailbox_alias, now=now)
    await store.bind(
        route,
        c.initial_cursor(route, attempt_id=scope.attempt_id),
        now(),
        int(now().timestamp()),
        scope.employee_id,
    )

    def outreach(factory, audit):
        return c.OutreachServiceImpl(
            factory,
            None,
            None,
            None,
            None,
            c.Phase1OutreachAuthorizer(tenant),
            audit,
            now=now,
        )

    def actor(identity):
        return c.OA(
            "system:email-inbound",
            c.OutreachScope(
                level=c.ScopeLevel.SYSTEM,
                allowed_sending_identity_ids=frozenset({identity}),
            ),
            "system",
        )

    processor = c.InboundPageProcessor(
        store,
        lambda expected: c.SqlAlchemyInboundPageUnitOfWork(
            store,
            expected,
            outreach_builder=outreach,
            audit_sink=c.StandardAuditLogger(),
            trial_commit=gate.commit_scan,
        ),
        actor,
    )
    driver = c.InboundDriver(
        SimpleNamespace(
            store=store,
            reader_for=lambda route: reader,
            processor=processor,
            trial_gate=gate,
        ),
        now=now,
    )
    assert await driver.scan_once() == 1
    assert not qq.downloads and not qq.searches
    async with sessions.begin() as s:
        await s.execute(
            update(QQTrialRow)
            .where(
                QQTrialRow.tenant_id == tenant, QQTrialRow.trial_id == scope.trial_id
            )
            .values(next_scan_at=now() - timedelta(seconds=1))
        )
    qq.messages[11] = c.mail(
        reply=scope.provider_header, body="I am interested in purchasing."
    )
    qq.next_uid = 12
    return driver, store, qq, blobs, processor, reader


async def test_new_uid_through_gateway_atomic_page_and_guarded_classification_work(
    trial,
):
    gate, sessions, _, _, account, *_ = trial
    driver, _store, qq, blobs, *_ = await setup_pipeline(trial)
    assert await driver.scan_once() == 1
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "received" and row.message_id
    assert qq.downloads == [11] and list(blobs.values.values()) == [qq.messages[11]]
    scope = gate.scope
    tenant = scope.route.tenant_id
    async with sessions() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(
                    MessageRow.tenant_id == tenant, MessageRow.direction == "inbound"
                )
            )
            == 1
        )
        assert (
            await s.scalar(
                select(func.count())
                .select_from(EmailInboundReceiptRow)
                .where(EmailInboundReceiptRow.tenant_id == tenant)
            )
            == 1
        )
        attempt = await s.scalar(
            select(OutreachMessageAttemptRow).where(
                OutreachMessageAttemptRow.tenant_id == tenant,
                OutreachMessageAttemptRow.attempt_id == scope.attempt_id,
            )
        )
    now = lambda: datetime.now(UTC)
    service = ConversationServiceImpl(
        lambda t: SqlAlchemyConversationsUnitOfWork(
            sessions, t, now=now, trial_commit=gate.commit_message
        ),
        now=now,
    )
    # 分类值是明确标注的离线夹具，原 14 类和 needs_review 不做重定义。
    await service.record_classification(
        tenant,
        row.message_id,
        ReplyCategory.CLEAR_INTEREST,
        "offline-fixture",
        outbound_message_id=attempt.message_id,
    )
    request = ReplyWorkActionRequest(
        message_id=row.message_id,
        outbound_message_id=attempt.message_id,
        enrollment_id=attempt.enrollment_id,
        account_id=account,
        contact_point_id="cp_fixture",
        action=ReplyWorkAction.START_QUALIFICATION,
        idempotency_key=f"reply:start_qualification:{row.message_id}",
    )
    first = await service.enqueue_reply_work_action(tenant, request)
    assert await service.enqueue_reply_work_action(tenant, request) == first
    async with sessions() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(
                    OutboxEventRow.tenant_id == tenant,
                    OutboxEventRow.event_type == "ReplyReceived",
                )
            )
            == 1
        )
    await gate.stop("cancelled")
    with pytest.raises(QQTrialStopped):
        await service.record_classification(
            tenant,
            row.message_id,
            ReplyCategory.CLEAR_INTEREST,
            "offline-fixture",
            outbound_message_id=attempt.message_id,
        )


async def test_scope_revocation_at_original_page_commit_rolls_back(trial):
    gate, sessions, _, state, *_ = trial
    driver, store, qq, _, _, _ = await setup_pipeline(trial)
    original = gate.commit_scan

    # 在原整页事务提交门禁处撤销，正文已消耗也不能重试/伪造已入库。
    async def revoke(session, message):
        if message is not None:
            state["mailbox"] = False
        await original(session, message)

    gate.commit_scan = revoke
    # setup_pipeline 的闭包在构造每页 UoW 时取当前回调。
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "stopped" and row.message_id is None and row.bodies == 1
    assert (await store.read_cursor()).next_retry_at is None
    assert await driver.scan_once() == 0 and qq.downloads == [11]
    async with sessions() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(MessageRow)
                .where(
                    MessageRow.tenant_id == gate.scope.route.tenant_id,
                    MessageRow.direction == "inbound",
                )
            )
            == 0
        )
