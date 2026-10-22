"""独立 PostgreSQL 与固定假邮箱：真实行锁、迁移、预算与跨实例恢复边界。"""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.qq_mail.inbound import QQInboundReader, initial_cursor
from connectors.qq_mail.sent_alias import verify_sent_alias
from infra.db.qq_alias import provision_trial, record_verified_alias
from infra.db.qq_inbound import PostgresProjectReplyScope
from infra.db.qq_trial import SqlQQTrialGate
from infra.db.qq_trial_tables import QQTrialEventRow, QQTrialRow
from infra.db.session import create_engine_from
from infra.db.tables import (
    EmployeeRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    RawArtifactRow,
    ToolCallRow,
)
from shared.schemas.identifiers import new_id
from shared.schemas.qq_trial import (
    QQTrialLimits,
    QQTrialScope,
    QQTrialStopped,
    QQTrialWatermark,
)
from tests.integration.test_manual_email_send_api import _seed_employee
from tests.integration.test_qq_inbound_scope import seed
from tests.unit.test_qq_inbound import FROM, TO, Session, mail


@pytest_asyncio.fixture
async def trial(db_url, request):
    engine = create_engine_from(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = new_id("tn")
    route, owner, user, header, attempt, account, _point = await seed(sessions, tenant)
    # 旧 scope 夹具只读取授权投影；组合测试还要通过原 MessageAttempt 领域校验。
    async with sessions.begin() as s:
        await s.execute(
            update(OutreachMessageAttemptRow)
            .where(
                OutreachMessageAttemptRow.tenant_id == tenant,
                OutreachMessageAttemptRow.attempt_id == attempt,
            )
            .values(message_id=new_id("msg"))
        )
    route = route.model_copy(update={"mailbox_alias": "qq-bounded-fixture"})
    boss = new_id("emp")
    await _seed_employee(sessions, tenant, boss)
    async with sessions() as s:
        boss_user = await s.scalar(
            select(EmployeeRow.user_id).where(
                EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == boss
            )
        )
    now = datetime.now(UTC)
    alias = f"<{new_id('synthetic')}@qq.com>"
    scope = QQTrialScope(
        trial_id=new_id("qqt"),
        route=route,
        attempt_id=attempt,
        employee_id=owner,
        user_id=user,
        classifier_employee_id=boss,
        classifier_user_id=boss_user,
        model="fixture-model",
        model_configuration_version="fixture-v1",
        canonical_header=header,
        provider_header=alias,
        alias_evidence_id=new_id("qae"),
        authorization_ref="offline-fixture-not-live-approval",
        authorization_source="explicit_user_request",
        starts_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=599),
        limits=QQTrialLimits(),
    )
    folded_headers = False
    if getattr(request, "param", None):
        changes = dict(request.param)
        folded_headers = changes.pop("folded_headers", False)
        scope = QQTrialScope.model_validate({**scope.model_dump(), **changes})
    state = {"mailbox": True, "model_permission": True}

    class Binding:
        async def matches(self, t, e, u, address):
            return state["mailbox"] and (t, e, u, address) == (tenant, owner, user, TO)

    current = PostgresProjectReplyScope(
        sessions, route, owner, Binding(), attempt_id=attempt
    )

    async def authorize():
        return (
            state["model_permission"] and await current.target(route, owner) is not None
        )

    idem = header[1:].split("@")[0]
    raw = f"Message-ID: {alias}\r\nX-TradeOS-Idempotency-V1: {idem}\r\nFrom: {TO}\r\nTo: {FROM}\r\n\r\n".encode()
    if folded_headers:
        raw = raw.replace(b"X-TradeOS-Idempotency-V1: ", b"X-TradeOS-Idempotency-V1:\r\n ")
    proof = verify_sent_alias(
        (raw,),
        source="sender_sent",
        canonical_header=header,
        idempotency_header=idem,
        sender=TO,
        recipient=FROM,
    )
    artifact, call = new_id("art"), new_id("tcl")
    async with sessions.begin() as s:
        s.add(
            RawArtifactRow(
                tenant_id=tenant,
                artifact_id=artifact,
                kind="email_raw",
                mime_type="message/rfc822",
                content_hash=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                object_key=f"raw/{tenant}/{artifact}",
                uploaded_at=now,
            )
        )
        s.add(
            ToolCallRow(
                tenant_id=tenant,
                tool_call_id=call,
                tool_id="email.qq.sent_alias.read",
                tool_version="offline-fixture",
                risk_level="low",
                cost_class="free",
                status="succeeded",
                attempt_count=1,
                user_id=user,
                idempotency_key=f"fixture:{call}",
                request_fingerprint="a" * 64,
                fingerprint_version="v1",
                provider_ref=artifact,
                message_attempt_id=attempt,
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
    await record_verified_alias(
        sessions,
        scope,
        proof,
        artifact_id=artifact,
        tool_call_id=call,
        authorize=authorize,
    )
    await provision_trial(sessions, scope, authorize)
    gate = SqlQQTrialGate(sessions, scope, authorize)
    try:
        yield gate, sessions, current, state, account, proof, artifact, call
    finally:
        await engine.dispose()


async def snapshot(gate, sessions):
    async with sessions() as s:
        return await s.scalar(
            select(QQTrialRow).where(
                QQTrialRow.tenant_id == gate.scope.route.tenant_id,
                QQTrialRow.trial_id == gate.scope.trial_id,
            )
        )


async def received(gate, sessions):
    assert await gate.claim_scan()
    await gate.reserve("sessions")
    await gate.initialize(QQTrialWatermark(uidvalidity=7, high_uid=10))
    await gate.reserve("bodies")
    async with sessions.begin() as s:
        await gate.commit_scan(s, "msg_offline_fixture")


async def test_restart_preserves_watermark_and_refuses_unknown_scan(trial):
    gate, sessions, _, _, _, *_ = trial
    assert await gate.claim_scan()
    await gate.reserve("sessions")
    await gate.initialize(QQTrialWatermark(uidvalidity=7, high_uid=100))
    restarted = SqlQQTrialGate(sessions, gate.scope, gate.authorize)
    assert (await restarted.watermark()).high_uid == 100
    with pytest.raises(QQTrialStopped, match="QQ"):
        await restarted.claim_scan()
    assert (await snapshot(gate, sessions)).sessions == 1


async def test_concurrent_scan_claim_has_one_winner(trial):
    gate, sessions, *_ = trial
    other = SqlQQTrialGate(sessions, gate.scope, gate.authorize)
    result = await asyncio.gather(
        gate.claim_scan(), other.claim_scan(), return_exceptions=True
    )
    assert sum(x is True for x in result) == 1
    assert sum(isinstance(x, QQTrialStopped) for x in result) == 1


@pytest.mark.parametrize(
    "kind,limit", [("sessions", 20), ("searches", 20), ("headers", 20), ("bodies", 1)]
)
async def test_mail_budget_is_durable_and_cannot_be_exceeded(trial, kind, limit):
    gate, sessions, *_ = trial
    assert await gate.claim_scan()
    for _ in range(limit):
        await gate.reserve(kind)
    with pytest.raises(QQTrialStopped):
        await gate.reserve(kind)
    assert getattr(await snapshot(gate, sessions), kind) == limit


async def test_scan_interval_and_atomic_message_binding(trial):
    gate, sessions, *_ = trial
    assert await gate.claim_scan()
    await gate.reserve("sessions")
    async with sessions.begin() as s:
        await gate.commit_scan(s, None)
    assert not await gate.claim_scan()
    assert (await snapshot(gate, sessions)).scan_token is None


@pytest.mark.parametrize(
    "change",
    ["mailbox", "model_permission", "owner", "inactive", "classifier", "tenant"],
)
async def test_current_scope_changes_stop_before_dispatch(trial, change):
    gate, sessions, _, state, account, *_ = trial
    if change in state:
        state[change] = False
    elif change == "tenant":
        different = gate.scope.model_copy(
            update={
                "route": gate.scope.route.model_copy(update={"tenant_id": new_id("tn")})
            }
        )
        gate = SqlQQTrialGate(sessions, different, gate.authorize)
    else:
        async with sessions.begin() as s:
            if change == "owner":
                await s.execute(
                    update(OwnershipLockRow)
                    .where(
                        OwnershipLockRow.tenant_id == gate.scope.route.tenant_id,
                        OwnershipLockRow.account_id == account,
                    )
                    .values(owner=gate.scope.classifier_employee_id)
                )
            else:
                who = (
                    gate.scope.employee_id
                    if change == "inactive"
                    else gate.scope.classifier_employee_id
                )
                await s.execute(
                    update(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == gate.scope.route.tenant_id,
                        EmployeeRow.employee_id == who,
                    )
                    .values(is_active=False)
                )
    with pytest.raises(QQTrialStopped):
        await gate.claim_scan()


async def test_single_classification_provider_count_and_output_budget(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    await gate.reserve("classifications")
    with pytest.raises(QQTrialStopped):
        await gate.reserve("classifications")
    for _ in range(4):
        await gate.reserve("provider_requests", input_bytes=10)
        await gate.provider_finished(10, 128)
    with pytest.raises(QQTrialStopped):
        await gate.reserve("provider_requests")
    row = await snapshot(gate, sessions)
    assert (
        row.classifications,
        row.provider_requests,
        row.input_bytes,
        row.output_tokens,
    ) == (1, 4, 40, 40)


async def test_unknown_provider_and_restart_never_dispatch_again(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    await gate.reserve("classifications")
    await gate.reserve("provider_requests", input_bytes=10)
    other = SqlQQTrialGate(sessions, gate.scope, gate.authorize)
    with pytest.raises(QQTrialStopped):
        await other.reserve("provider_requests")
    assert (await snapshot(gate, sessions)).provider_requests == 1


async def test_total_model_input_limit_precedes_dispatch(trial):
    gate, sessions, *_ = trial
    await received(gate, sessions)
    await gate.reserve("classifications")
    await gate.reserve("provider_requests", input_bytes=32768)
    await gate.provider_finished(1, 10)
    with pytest.raises(QQTrialStopped):
        await gate.reserve("provider_requests", input_bytes=1)
    assert (await snapshot(gate, sessions)).provider_requests == 1


async def test_cancel_is_terminal_and_does_not_refund(trial):
    gate, sessions, *_ = trial
    assert await gate.claim_scan()
    await gate.reserve("sessions")
    await gate.stop("cancelled")
    with pytest.raises(QQTrialStopped):
        await gate.claim_scan()
    row = await snapshot(gate, sessions)
    assert row.sessions == 1 and row.state == "stopped"


async def test_alias_evidence_idempotency_and_no_new_budget_for_same_attempt(trial):
    gate, sessions, _, _, _, proof, artifact, call = trial
    await record_verified_alias(
        sessions,
        gate.scope,
        proof,
        artifact_id=artifact,
        tool_call_id=call,
        authorize=gate.authorize,
    )
    with pytest.raises(IntegrityError):
        await provision_trial(
            sessions,
            gate.scope.model_copy(
                update={
                    "trial_id": new_id("qqt"),
                    "route": gate.scope.route.model_copy(
                        update={"mailbox_alias": "another-fixture"}
                    ),
                }
            ),
            gate.authorize,
        )


async def test_fake_mailbox_initialization_then_explicit_new_reply(trial):
    gate, sessions, current, *_ = trial
    scope = gate.scope
    fake = Session({10: mail(reply=scope.provider_header)})
    reader = QQInboundReader(
        scope.route, scope.employee_id, fake, current, address=TO, trial_gate=gate
    )
    assert await gate.claim_scan()
    page = await reader.fetch_inbound_page(
        scope.route.mailbox_alias,
        initial_cursor(scope.route, attempt_id=scope.attempt_id),
        20,
    )
    assert page.items == () and fake.downloads == [] and fake.searches == []
    async with sessions.begin() as s:
        await gate.commit_scan(s, None)
    # 测试时钟边界：只把此合成行的下次扫描时间推进，不重置计数/起点。
    async with sessions.begin() as s:
        await s.execute(
            update(QQTrialRow)
            .where(
                QQTrialRow.tenant_id == scope.route.tenant_id,
                QQTrialRow.trial_id == scope.trial_id,
            )
            .values(next_scan_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    fake.messages[11] = mail(reply=scope.provider_header)
    fake.next_uid = 12
    assert await gate.claim_scan()
    page2 = await reader.fetch_inbound_page(
        scope.route.mailbox_alias, page.next_cursor, 20
    )
    assert fake.downloads == [11] and len(page2.items) == 1
    assert page2.items[0].in_reply_to == scope.canonical_header
    async with sessions.begin() as s:
        await gate.commit_scan(s, "msg_fixture_read")
    assert not await gate.claim_scan()
    row = await snapshot(gate, sessions)
    assert (row.sessions, row.searches, row.headers, row.bodies) == (2, 1, 1, 1)


async def test_page_transaction_rollback_keeps_unknown_claim_and_no_message(trial):
    gate, sessions, *_ = trial
    assert await gate.claim_scan()
    await gate.reserve("sessions")
    await gate.reserve("bodies")
    with pytest.raises(RuntimeError):
        async with sessions.begin() as s:
            await gate.commit_scan(s, "msg_fixture_rollback")
            raise RuntimeError("fixture rollback")
    row = await snapshot(gate, sessions)
    assert row.message_id is None and row.scan_token is not None and row.bodies == 1
    with pytest.raises(QQTrialStopped):
        await gate.claim_scan()


async def test_events_are_append_only(trial):
    gate, sessions, *_ = trial
    assert await gate.claim_scan()
    with pytest.raises(DBAPIError):
        async with sessions.begin() as s:
            await s.execute(
                update(QQTrialEventRow)
                .where(QQTrialEventRow.tenant_id == gate.scope.route.tenant_id)
                .values(kind="rewritten")
            )
