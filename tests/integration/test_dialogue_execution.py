# ruff: noqa: F811 -- pytest 显式复用持久化夹具
"""真实PG验证同源唯一、租约fencing与未知外调禁止再派。"""
from datetime import UTC, datetime, timedelta

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_dialogue_source import (  # noqa: F401
    dialogue_source_runtime,
    owned_infrastructure,
    page_runtime,
)
from tests.integration.test_outreach_campaign_lifecycle import (
    lifecycle_engine,  # noqa: F401
)


@pytest.mark.asyncio
async def test_execution_fences_expired_claim_and_never_replays_started(dialogue_source_runtime):
    from domains.conversations.dialogue_execution import DialogueExecutionState as State
    from infra.db.dialogue_execution import SqlDialogueExecutionStore
    runtime, source, _ = dialogue_source_runtime
    sessions, tenant, message, binding = runtime["factory"], source.tenant_id, source.admitted_message_id, source.binding_id
    clock = [datetime(2026, 10, 22, tzinfo=UTC)]
    store = SqlDialogueExecutionStore(sessions, tenant, now=lambda: clock[0])
    await store.enqueue(message, binding)
    await store.enqueue(message, binding)
    with pytest.raises(ValidationError):
        await store.enqueue(message, new_id("dlg"))
    first = await store.claim(lease_duration=timedelta(seconds=10))
    assert first is not None
    assert await store.claim(lease_duration=timedelta(seconds=10)) is None
    clock[0] += timedelta(seconds=11)
    second = await store.claim(lease_duration=timedelta(seconds=10))
    assert second is not None and first.run_id == second.run_id
    assert first.lease_token != second.lease_token
    with pytest.raises(ValidationError):
        await store.advance(first, State.MODEL_STARTED)
    started = await store.advance(second, State.MODEL_STARTED)
    clock[0] += timedelta(seconds=11)
    restarted = SqlDialogueExecutionStore(sessions, tenant, now=lambda: clock[0])
    assert await restarted.claim(lease_duration=timedelta(seconds=10)) is None
    result = await restarted.read(message)
    assert result.state is State.UNKNOWN and result.run_id == started.run_id
    foreign = SqlDialogueExecutionStore(sessions, TenantId(new_id("tn")), now=lambda: clock[0])
    assert await foreign.read(message) is None
    with pytest.raises(ValidationError):
        await foreign.enqueue(message, binding)


@pytest.mark.asyncio
async def test_sending_unknown_reconciles_only_exact_original_attempt(dialogue_source_runtime):
    from domains.conversations.dialogue_execution import DialogueExecutionState as State
    from infra.db.dialogue_execution import SqlDialogueExecutionStore
    runtime, source, _ = dialogue_source_runtime
    sessions, tenant, message, binding = runtime["factory"], source.tenant_id, source.admitted_message_id, source.binding_id
    clock = [datetime(2026, 10, 22, tzinfo=UTC)]
    store = SqlDialogueExecutionStore(sessions, tenant, now=lambda: clock[0])
    await store.enqueue(message, binding)
    claim = await store.claim(lease_duration=timedelta(seconds=10))
    started = await store.advance(claim, State.MODEL_STARTED)
    planned = await store.advance(started, State.PLANNED,
        plan_artifact_id=new_id("art"), plan_sha256="a" * 64)
    attempt = new_id("mat")
    header = "<dialogue." + attempt + "@example.test>"
    sending = await store.advance(planned, State.SENDING, attempt_id=attempt,
        deterministic_message_id=header, idempotency_key="dialogue:" + attempt)
    clock[0] += timedelta(seconds=11)
    assert await store.claim(lease_duration=timedelta(seconds=10)) is None
    with pytest.raises(ValidationError):
        await store.reconcile_sent(message, attempt_id=new_id("mat"),
            deterministic_message_id=header, provider_ref="provider-exact")
    sent = await store.reconcile_sent(message, attempt_id=attempt,
        deterministic_message_id=header, provider_ref="provider-exact")
    assert sent.state is State.SENT and sent.run_id == sending.run_id
    assert await store.claim(lease_duration=timedelta(seconds=10)) is None
    same = await store.reconcile_sent(message, attempt_id=attempt,
        deterministic_message_id=header, provider_ref="provider-exact")
    assert same.version == sent.version
    with pytest.raises(ValidationError):
        await store.reconcile_sent(message, attempt_id=attempt,
            deterministic_message_id=header, provider_ref="different-result")


@pytest.mark.asyncio
async def test_execution_migration_roundtrip_preserves_metadata_and_rls(lifecycle_engine):
    from uuid import uuid4

    from sqlalchemy import inspect, text

    from infra.db.session import create_engine_from
    from infra.db.tables import Base
    from tests.integration.test_migrations import _run_alembic
    database = "test_dialogue_" + uuid4().hex
    async with lifecycle_engine.connect() as conn:
        admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database}"'))
    url = lifecycle_engine.url.set(database=database).render_as_string(False)
    engine = create_engine_from(url)
    try:
        _run_alembic(url, "upgrade", "0101")
        async with engine.connect() as conn:
            columns = await conn.run_sync(lambda c: inspect(c).get_columns("dialogue_executions"))
            assert {c["name"]: c["nullable"] for c in columns} == {
                c.name: c.nullable for c in Base.metadata.tables["dialogue_executions"].columns
            }
            for table in ("dialogue_executions", "dialogue_bindings", "dialogue_messages", "dialogue_model_reservations", "dialogue_business_receipts", "business_mail_messages", "conversation_reply_work"):
                actual = await conn.run_sync(lambda c, table=table: inspect(c).get_columns(table))
                assert {c["name"]: c["nullable"] for c in actual} == {
                    c.name: c.nullable for c in Base.metadata.tables[table].columns
                }
                assert await conn.scalar(text("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname=:table"), {"table": table})
        _run_alembic(url, "downgrade", "0093")
        async with engine.connect() as conn:
            assert "dialogue_executions" not in await conn.run_sync(lambda c: inspect(c).get_table_names())
        _run_alembic(url, "upgrade", "0101")
    finally:
        await engine.dispose()
        async with lifecycle_engine.connect() as conn:
            admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))


@pytest.mark.asyncio
async def test_ordinary_send_reserves_shared_campaign_total_once(dialogue_source_runtime):
    from sqlalchemy import select

    from domains.conversations.dialogue_execution import DialogueExecutionState as State
    from domains.outreach.errors import CampaignQuotaExceededError
    from infra.db.dialogue_execution import SqlDialogueExecutionStore
    from infra.db.tables import OutreachCampaignVersionRow, OutreachDailyQuotaRow
    runtime, source, _ = dialogue_source_runtime
    clock = [datetime.now(UTC)]
    store = SqlDialogueExecutionStore(runtime['factory'], source.tenant_id, now=lambda: clock[0])
    await store.enqueue(source.admitted_message_id, source.binding_id)
    claim = await store.claim(lease_duration=timedelta(minutes=2))
    claim = await store.advance(claim, State.MODEL_STARTED)
    claim = await store.advance(claim, State.PLANNED, plan_artifact_id=new_id('art'), plan_sha256='a'*64)
    claim = await store.prepare_send(claim, attempt_id=new_id('mat'),
        deterministic_message_id='<dialogue.quota@example.test>', idempotency_key='dialogue.quota')
    async with runtime['factory'].begin() as db:
        version = await db.get(OutreachCampaignVersionRow, (source.tenant_id, source.campaign_id, source.campaign_version))
        limit = version.daily_total_message_limit
        # 原冷发送已占同一个批准总量。
        db.add(OutreachDailyQuotaRow(tenant_id=source.tenant_id, campaign_id=source.campaign_id,
            on_day=datetime.now(UTC).date(), new_contacts_reserved=0, messages_reserved=limit))
    with pytest.raises(CampaignQuotaExceededError):
        await store.reserve_campaign_slot(claim)
    clock[0] += timedelta(days=1)
    claim = await store.claim(lease_duration=timedelta(minutes=2))
    await store.reserve_campaign_slot(claim)
    restarted = SqlDialogueExecutionStore(runtime['factory'], source.tenant_id, now=lambda: clock[0])
    await restarted.reserve_campaign_slot(claim)
    claim = await store.advance(claim, State.SENDING)
    await store.reserve_campaign_slot(claim)
    async with runtime['factory']() as db:
        quota = await db.scalar(select(OutreachDailyQuotaRow).where(OutreachDailyQuotaRow.tenant_id==source.tenant_id,
            OutreachDailyQuotaRow.campaign_id==source.campaign_id, OutreachDailyQuotaRow.on_day==clock[0].date()))
        assert quota.messages_reserved == 1


async def test_two_runtime_tenant_roles_cannot_read_or_mutate_dialogue_identity(dialogue_source_runtime, db_url):
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from infra.db.dialogue_execution import SqlDialogueExecutionStore
    from infra.db.dialogue_execution_tables import (
        DialogueBindingRow,
        DialogueExecutionRow,
    )
    from infra.db.dialogue_source import SqlDialogueSources
    from infra.db.session import create_engine_from
    from tests.runtime_database_fixtures import runtime_database_scope
    runtime, source, _ = dialogue_source_runtime
    store = SqlDialogueExecutionStore(runtime['factory'], source.tenant_id)
    await store.enqueue(source.admitted_message_id,source.binding_id)
    foreign_tenant = TenantId(new_id('tn'))
    async with runtime_database_scope(db_url) as provision:
        foreign_engine=create_engine_from(await provision(str(foreign_tenant)))
        foreign_sessions=async_sessionmaker(foreign_engine,expire_on_commit=False)
        try:
            async with foreign_sessions() as db:
                # 故意不加过滤，验证真正role RLS，不能以应用WHERE代替隔离证据。
                assert await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.binding_id==source.binding_id)) is None
                assert (await db.execute(text('UPDATE dialogue_bindings SET state=\'review\' WHERE binding_id=:b'),{'b':source.binding_id})).rowcount==0
            foreign=SqlDialogueExecutionStore(foreign_sessions,foreign_tenant)
            assert await foreign.read(source.admitted_message_id) is None
            assert await foreign.claim(lease_duration=timedelta(seconds=10)) is None
            with pytest.raises(ValidationError):
                await foreign.enqueue(source.admitted_message_id,source.binding_id)
            reader=SqlDialogueSources(foreign_sessions,foreign_tenant)
            assert await reader.for_message(foreign_tenant,source.admitted_message_id) is None
            assert await reader.for_sent_header(foreign_tenant,runtime['outbound']) is None
            with pytest.raises(ValidationError):
                await reader.for_message(source.tenant_id,source.admitted_message_id)
        finally:
            await foreign_engine.dispose()
    async with runtime['factory']() as db:
        state = await db.scalar(select(DialogueExecutionRow.state).where(
            DialogueExecutionRow.tenant_id == source.tenant_id,
            DialogueExecutionRow.source_message_id == source.admitted_message_id,
        ))
        assert state == 'queued'
