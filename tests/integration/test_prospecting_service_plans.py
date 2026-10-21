"""服务档位真实持久化：额度、重启、周期授权与来源租户隔离。"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.errors import InvalidStateTransition, TransientError, ValidationError

NOW = datetime(2026, 10, 10, 8, tzinfo=UTC)
TENANT = "ten_service_plans"


@asynccontextmanager
async def store_db(tmp_path):
    """每例独立文件库，跨 store 实例及并发连接共享真实已提交状态。"""
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore
    from infra.db.prospecting_service_tables import SERVICE_PLAN_TABLES
    from infra.db.tables import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'plans.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        # 只需真实账户主键；本测试不伪造业务字段或调用账户服务。
        await connection.execute(
            text(
                "CREATE TABLE prospect_accounts (tenant_id VARCHAR(40) NOT NULL, "
                "account_id VARCHAR(40) NOT NULL, PRIMARY KEY (tenant_id, account_id))"
            )
        )
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=SERVICE_PLAN_TABLES)
        )
        await connection.execute(
            text(
                "INSERT INTO prospect_accounts (tenant_id, account_id) "
                "VALUES (:tenant, 'acc_one'), ('ten_other', 'acc_other')"
            ),
            {"tenant": TENANT},
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield PostgresProspectingServicePlanStore(factory), factory
    finally:
        await engine.dispose()


async def assigned(store, tier="basic"):
    return await store.assign(
        TENANT, tier, expected_version=None, actor_id="emp_owner", now=NOW
    )


async def scheduled(store, tier="basic"):
    state = await assigned(store, tier)
    return await store.configure_schedule(
        TENANT,
        proposal_id="dpr_approved",
        acting_user_id="usr_owner",
        enabled=True,
        expected_version=state.version,
        now=NOW,
    )


async def test_unstarted_cycle_can_be_rebound_but_stale_driver_cannot_claim(tmp_path):
    from infra.db.prospecting_service_tables import ProspectingDiscoveryCycleRow

    async with store_db(tmp_path) as (store, factory):
        state = await scheduled(store)
        original = await store.begin_cycle(TENANT, NOW)
        changed = await store.configure_schedule(
            TENANT,
            proposal_id="dpr_replacement",
            acting_user_id="usr_other",
            enabled=True,
            expected_version=state.version,
            now=NOW,
        )
        assert not await store.claim_cycle_start(
            TENANT,
            key=original.key,
            expected_version=state.version,
            now=NOW,
        )
        async with factory() as session:
            old = await session.get(
                ProspectingDiscoveryCycleRow, (TENANT, original.key)
            )
            assert old.superseded_at is not None
            assert old.superseded_by_version == changed.version
            assert old.start_attempted_at is None
        replacement = await store.begin_cycle(TENANT, NOW)
        assert replacement.key != original.key
        assert replacement.proposal_id == "dpr_replacement"
        assert await store.claim_cycle_start(
            TENANT,
            key=replacement.key,
            expected_version=changed.version,
            now=NOW,
        )


async def test_claimed_unknown_cycle_survives_restart_and_cannot_rebind(tmp_path):
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore

    async with store_db(tmp_path) as (store, factory):
        state = await scheduled(store)
        original = await store.begin_cycle(TENANT, NOW)
        assert await store.claim_cycle_start(
            TENANT,
            key=original.key,
            expected_version=state.version,
            now=NOW,
        )
        restarted = PostgresProspectingServicePlanStore(factory)
        with pytest.raises(InvalidStateTransition):
            await restarted.configure_schedule(
                TENANT,
                proposal_id="dpr_replacement",
                acting_user_id="usr_other",
                enabled=True,
                expected_version=state.version,
                now=NOW,
            )
        assert await restarted.begin_cycle(TENANT, NOW) == original
        assert await restarted.claim_cycle_start(
            TENANT,
            key=original.key,
            expected_version=state.version,
            now=NOW,
        )


async def test_prepared_cycle_cannot_bind_and_pause_or_stale_version_cannot_claim(
    tmp_path,
):
    async with store_db(tmp_path) as (store, _factory):
        state = await scheduled(store)
        cycle = await store.begin_cycle(TENANT, NOW)
        with pytest.raises(InvalidStateTransition):
            await store.bind_cycle(TENANT, key=cycle.key, run_id="run_forged", now=NOW)
        assert not await store.claim_cycle_start(
            TENANT,
            key=cycle.key,
            expected_version=state.version + 1,
            now=NOW,
        )
        paused = await store.configure_schedule(
            TENANT,
            proposal_id=state.proposal_id,
            acting_user_id=state.acting_user_id,
            enabled=False,
            expected_version=state.version,
            now=NOW,
        )
        assert not await store.claim_cycle_start(
            TENANT,
            key=cycle.key,
            expected_version=paused.version,
            now=NOW,
        )


async def inventory_scheduled(store):
    from domains.prospecting.service import InventoryQuery
    state = await assigned(store)
    return await store.configure_inventory_schedule(TENANT,
        query=InventoryQuery(product_tags=("hinges",), country="DE", confirmed_refs=({"document_id":"doc", "revision_id":"rev"},)),
        employee_id="emp_owner", acting_user_id="usr_owner", enabled=True,
        expected_version=state.version, now=NOW)


@pytest.mark.parametrize("committed", [False, True])
async def test_driver_unknown_request_keeps_identity_and_recovers_exact_key(tmp_path, committed):
    from hashlib import sha256
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from apps.scheduler_worker.scheduled_inventory import ScheduledInventoryDriver
    saved = {}
    calls = []
    async def request(tenant, employee, query, count, key):
        calls.append(key)
        if committed or len(calls) > 1:
            saved[key] = sha256(key.encode()).hexdigest()
        if len(calls) == 1:
            raise TransientError("回执待确认")
    async def find(key): return saved.get(key)
    async with store_db(tmp_path) as (store, _):
        state = await inventory_scheduled(store)
        driver = ScheduledInventoryDriver(SimpleNamespace(scan=AsyncMock(return_value=0)), tenant_id=TENANT,
            store=store, requests=SimpleNamespace(request=request), receipts=SimpleNamespace(find_request=find, request_reservation=AsyncMock(return_value=None)),
            authority=SimpleNamespace(authorize_schedule=AsyncMock()), context=SimpleNamespace(read=AsyncMock()), now=lambda:NOW)
        with pytest.raises(TransientError): await driver.submit_due()
        original = await store.begin_cycle(TENANT, NOW)
        with pytest.raises(InvalidStateTransition):
            await store.configure_inventory_schedule(TENANT, query=state.query, employee_id="other", acting_user_id="other",
                enabled=True, expected_version=state.version, now=NOW)
        assert await driver.submit_due() == 1
        assert calls == [original.key] * (1 if committed else 2)
        assert await store.begin_cycle(TENANT, NOW) is None


async def test_driver_recovers_request_after_pause_and_expiry_and_never_starts_legacy(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from apps.scheduler_worker.scheduled_inventory import ScheduledInventoryDriver
    async with store_db(tmp_path) as (store, _):
        state = await inventory_scheduled(store)
        cycle = await store.begin_cycle(TENANT, NOW)
        assert await store.claim_cycle_start(TENANT, key=cycle.key, expected_version=state.version, now=NOW)
        await store.configure_inventory_schedule(TENANT, query=state.query, employee_id=state.employee_id,
            acting_user_id=state.acting_user_id, enabled=False, expected_version=state.version, now=NOW)
        request = AsyncMock(side_effect=AssertionError("不得新增请求"))
        expiry = NOW + timedelta(days=30)
        driver = ScheduledInventoryDriver(SimpleNamespace(scan=AsyncMock(return_value=0)), tenant_id=TENANT,
            store=store, requests=SimpleNamespace(request=request), receipts=SimpleNamespace(find_request=AsyncMock(return_value="a"*64)),
            authority=SimpleNamespace(authorize_schedule=AsyncMock(side_effect=AssertionError("只补回执"))),
            context=SimpleNamespace(read=AsyncMock(side_effect=AssertionError("只补回执"))), now=lambda:expiry)
        assert await driver.submit_due() == 1
        assert (await store.get(TENANT)).last_run_at == expiry
        request.assert_not_called()


@pytest.mark.parametrize("renew", [False, True])
async def test_real_research_receipt_blocks_old_period_and_caps_inflight_upgrade(
    tmp_path, renew
):
    from types import SimpleNamespace

    from tests.unit.test_tiered_demand_discovery import Reader
    from workflows.demand_discovery.service_tiers import (
        TieredDiscoveryTaskReader,
        TieredResearchSearcher,
        TieredResearchStep,
    )

    class Searcher:
        calls = 0

        async def search(self, *args):
            self.calls += 1

    async with store_db(tmp_path) as (store, _factory):
        state = await assigned(store)
        clock = [NOW]
        delegate = Searcher()
        searcher = TieredResearchSearcher(delegate, store, now=lambda: clock[0])
        reader = TieredDiscoveryTaskReader(Reader(), store, now=lambda: clock[0])

        class Handler:
            async def execute(self, run):
                if renew:
                    clock[0] = NOW + timedelta(days=30)
                await store.assign(
                    TENANT,
                    "advanced",
                    expected_version=state.version,
                    actor_id="emp_owner",
                    now=clock[0],
                )
                for _ in range(11):
                    await searcher.search(
                        TENANT, run.run_id, "query", "US", "valves", 5
                    )
                raise AssertionError("新的档位或账期不应扩张已有研究预留")

        run = SimpleNamespace(
            tenant_id=TENANT,
            run_id="run_research",
            subject_ref="proposal",
            context={"proposal_id": "proposal", "acting_user_id": "boss"},
        )
        wrapped = TieredResearchStep(Handler(), reader, store, now=lambda: clock[0])
        with pytest.raises(ValidationError):
            await wrapped.execute(run)
        assert delegate.calls == (0 if renew else 10)
        assert (await store.get(TENANT)).searches_reserved == (0 if renew else 10)


async def test_real_research_port_cannot_use_reservation_outside_trusted_step(tmp_path):
    from workflows.demand_discovery.service_tiers import TieredResearchSearcher

    class Searcher:
        async def search(self, *args):
            raise AssertionError("未持研究上下文不得调用外部端口")

    async with store_db(tmp_path) as (store, _factory):
        await assigned(store)
        await store.reserve(
            TENANT,
            key="run_direct:research",
            candidates=10,
            searches=10,
            contacts=0,
            now=NOW,
        )
        searcher = TieredResearchSearcher(Searcher(), store, now=lambda: NOW)
        with pytest.raises(ValidationError):
            await searcher.search(TENANT, "run_direct", "query", "US", "valves", 5)


async def test_assignment_is_durable_and_switching_preserves_period_and_usage(tmp_path):
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore

    async with store_db(tmp_path) as (store, factory):
        assert await store.get(TENANT) is None
        state = await assigned(store)
        assert state.period_ends_at == NOW + timedelta(days=30)
        assert state.schedule_enabled is False
        await store.reserve(
            TENANT, key="batch-one", candidates=12, searches=8, contacts=3, now=NOW
        )
        other = PostgresProspectingServicePlanStore(factory)
        state = await other.assign(
            TENANT,
            "advanced",
            expected_version=state.version,
            actor_id="emp_owner",
            now=NOW + timedelta(days=1),
        )
        assert (
            state.candidates_reserved,
            state.searches_reserved,
            state.contacts_reserved,
        ) == (12, 8, 3)
        assert state.period_started_at == NOW
        assert state.period_ends_at == NOW + timedelta(days=30)
        assert await other.get("ten_other") is None
        with pytest.raises(InvalidStateTransition):
            await other.assign(
                TENANT, "basic", expected_version=1, actor_id="emp_owner", now=NOW
            )


async def test_reservation_replay_conflict_and_parallel_last_credit(tmp_path):
    async with store_db(tmp_path) as (store, _factory):
        await assigned(store)
        await store.reserve(
            TENANT, key="first", candidates=99, searches=199, contacts=19, now=NOW
        )
        await store.reserve(
            TENANT, key="first", candidates=99, searches=199, contacts=19, now=NOW
        )
        with pytest.raises(InvalidStateTransition):
            await store.reserve(
                TENANT, key="first", candidates=98, searches=199, contacts=19, now=NOW
            )
        results = await asyncio.gather(
            *(
                store.reserve(
                    TENANT, key=key, candidates=1, searches=1, contacts=1, now=NOW
                )
                for key in ("last-a", "last-b")
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert (
            sum(isinstance(result, InvalidStateTransition) for result in results) == 1
        )
        state = await store.get(TENANT)
        assert (
            state.candidates_reserved,
            state.searches_reserved,
            state.contacts_reserved,
        ) == (100, 200, 20)


async def test_expired_plan_requires_assignment_and_old_keys_cannot_enter_new_period(
    tmp_path,
):
    async with store_db(tmp_path) as (store, _factory):
        state = await assigned(store)
        await store.reserve(
            TENANT, key="before-expiry", candidates=10, searches=10, contacts=1, now=NOW
        )
        expiry = NOW + timedelta(days=30)
        with pytest.raises(InvalidStateTransition):
            await store.reserve(
                TENANT,
                key="after-expiry",
                candidates=1,
                searches=1,
                contacts=1,
                now=expiry,
            )
        state = await store.assign(
            TENANT,
            "standard",
            expected_version=state.version,
            actor_id="emp_owner",
            now=expiry,
        )
        assert state.period_started_at == expiry
        assert state.period_ends_at == NOW + timedelta(days=60)
        with pytest.raises(InvalidStateTransition):
            await store.reserve(
                TENANT,
                key="before-expiry",
                candidates=10,
                searches=10,
                contacts=1,
                now=expiry,
            )
        state = await store.get(TENANT)
        assert (
            state.candidates_reserved,
            state.searches_reserved,
            state.contacts_reserved,
        ) == (0, 0, 0)


async def test_reservation_receipt_is_tenant_and_current_period_bound(tmp_path):
    async with store_db(tmp_path) as (store, _factory):
        state = await assigned(store)
        await store.reserve(
            TENANT, key="contact", candidates=0, searches=0, contacts=1, now=NOW
        )
        receipt = await store.reservation(TENANT, key="contact", now=NOW)
        assert (
            receipt.key,
            receipt.candidates,
            receipt.searches,
            receipt.contacts,
        ) == ("contact", 0, 0, 1)
        assert receipt.period_started_at == NOW
        assert receipt.tier == "basic"
        assert receipt.created_at == NOW
        upgraded = await store.assign(
            TENANT,
            "advanced",
            expected_version=state.version,
            actor_id="emp_owner",
            now=NOW + timedelta(days=1),
        )
        assert (
            await store.reservation(TENANT, key="contact", now=NOW + timedelta(days=1))
            == receipt
        )
        state = upgraded
        assert await store.reservation("ten_other", key="contact", now=NOW) is None
        assert await store.reservation(TENANT, key="missing", now=NOW) is None
        expiry = NOW + timedelta(days=30)
        assert await store.reservation(TENANT, key="contact", now=expiry) is None
        await store.assign(
            TENANT,
            "basic",
            expected_version=state.version,
            actor_id="emp_owner",
            now=expiry,
        )
        assert await store.reservation(TENANT, key="contact", now=expiry) is None


async def test_bound_receipt_can_finish_after_expiry_without_starting_new_cycle(
    tmp_path,
):
    async with store_db(tmp_path) as (store, _factory):
        state = await scheduled(store)
        cycle = await store.begin_cycle(TENANT, NOW)
        assert await store.claim_cycle_start(
            TENANT, key=cycle.key, expected_version=state.version, now=NOW
        )
        expiry = NOW + timedelta(days=30)
        assert await store.begin_cycle(TENANT, expiry) == cycle
        state = await store.bind_cycle(
            TENANT, key=cycle.key, run_id="run_old", now=expiry
        )
        assert state.last_run_at == expiry
        assert await store.begin_cycle(TENANT, expiry) is None


async def test_pending_cycle_recovers_same_authorization_and_bind_is_idempotent(
    tmp_path,
):
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore

    async with store_db(tmp_path) as (store, factory):
        state = await scheduled(store)
        cycle = await store.begin_cycle(TENANT, NOW)
        assert cycle.proposal_id == "dpr_approved"
        assert cycle.acting_user_id == "usr_owner"
        restarted = PostgresProspectingServicePlanStore(factory)
        assert await restarted.begin_cycle(TENANT, NOW + timedelta(hours=1)) == cycle
        assert await restarted.claim_cycle_start(
            TENANT, key=cycle.key, expected_version=state.version, now=NOW
        )
        with pytest.raises(InvalidStateTransition):
            await restarted.configure_schedule(
                TENANT,
                proposal_id="dpr_replacement",
                acting_user_id="usr_other",
                enabled=True,
                expected_version=state.version,
                now=NOW,
            )
        bound = await restarted.bind_cycle(
            TENANT, key=cycle.key, run_id="run_first", now=NOW
        )
        assert bound.last_run_at == NOW
        assert bound.next_run_at == NOW + timedelta(days=7)
        assert (
            await restarted.bind_cycle(
                TENANT, key=cycle.key, run_id="run_first", now=NOW + timedelta(days=1)
            )
            == bound
        )
        with pytest.raises(InvalidStateTransition):
            await restarted.bind_cycle(
                TENANT, key=cycle.key, run_id="run_second", now=NOW
            )
        assert await restarted.begin_cycle(TENANT, NOW + timedelta(days=6)) is None
        later = await restarted.begin_cycle(TENANT, NOW + timedelta(days=12))
        assert await restarted.claim_cycle_start(
            TENANT,
            key=later.key,
            expected_version=bound.version,
            now=NOW + timedelta(days=12),
        )
        bound = await restarted.bind_cycle(
            TENANT, key=later.key, run_id="run_later", now=NOW + timedelta(days=12)
        )
        assert bound.next_run_at == NOW + timedelta(days=19)


async def test_reconfigure_and_tier_switch_use_last_run_not_reset_to_now(tmp_path):
    async with store_db(tmp_path) as (store, _factory):
        state = await scheduled(store, "advanced")
        cycle = await store.begin_cycle(TENANT, NOW)
        assert await store.claim_cycle_start(
            TENANT, key=cycle.key, expected_version=state.version, now=NOW
        )
        state = await store.bind_cycle(
            TENANT, key=cycle.key, run_id="run_first", now=NOW
        )
        state = await store.assign(
            TENANT,
            "basic",
            expected_version=state.version,
            actor_id="emp_owner",
            now=NOW + timedelta(hours=1),
        )
        assert state.next_run_at == NOW + timedelta(days=7)
        state = await store.configure_schedule(
            TENANT,
            proposal_id="dpr_approved",
            acting_user_id="usr_owner",
            enabled=False,
            expected_version=state.version,
            now=NOW + timedelta(hours=2),
        )
        assert await store.begin_cycle(TENANT, NOW + timedelta(days=8)) is None
        await store.configure_schedule(
            TENANT,
            proposal_id="dpr_approved",
            acting_user_id="usr_owner",
            enabled=True,
            expected_version=state.version,
            now=NOW + timedelta(hours=3),
        )
        assert await store.begin_cycle(TENANT, NOW + timedelta(days=1)) is None


async def test_invalid_inputs_and_unconfigured_plan_fail_closed(tmp_path):
    async with store_db(tmp_path) as (store, _factory):
        with pytest.raises(InvalidStateTransition):
            await store.reserve(
                TENANT, key="missing", candidates=1, searches=1, contacts=1, now=NOW
            )
        assert await store.begin_cycle(TENANT, NOW) is None
        with pytest.raises(ValidationError):
            await store.assign(
                TENANT, "unknown", expected_version=None, actor_id="emp_owner", now=NOW
            )
        state = await assigned(store)
        with pytest.raises(ValidationError):
            await store.configure_schedule(
                TENANT,
                proposal_id=None,
                acting_user_id=None,
                enabled=True,
                expected_version=state.version,
                now=NOW,
            )
        for amount in (-1, True):
            with pytest.raises(ValidationError):
                await store.reserve(
                    TENANT,
                    key="invalid",
                    candidates=amount,
                    searches=1,
                    contacts=1,
                    now=NOW,
                )
        with pytest.raises(ValidationError):
            await store.reserve(
                TENANT,
                key="naive",
                candidates=1,
                searches=1,
                contacts=1,
                now=NOW.replace(tzinfo=None),
            )


async def test_evidence_is_content_idempotent_and_bound_to_existing_tenant_account(
    tmp_path,
):
    from domains.prospecting.service_tiers import ScreeningEvidence

    async with store_db(tmp_path) as (store, _factory):
        evidence = ScreeningEvidence(
            tenant_id=TENANT,
            account_id="acc_one",
            kind="business_match",
            source_ref="sig_source",
            source_url="https://buyer.example.com/about",
            source_quote="We distribute hardware.",
            recorded_at=NOW,
        )
        await store.append_evidence(TENANT, "acc_one", evidence)
        await store.append_evidence(TENANT, "acc_one", evidence)
        assert await store.get_evidence(TENANT, "acc_one") == (evidence,)
        with pytest.raises(ValidationError):
            await store.append_evidence("ten_other", "acc_other", evidence)
        with pytest.raises(ValidationError):
            await store.append_evidence(
                TENANT,
                "acc_missing",
                evidence.model_copy(update={"account_id": "acc_missing"}),
            )
        assert await store.get_evidence("ten_other", "acc_one") == ()


async def test_evidence_corruption_is_rejected_instead_of_becoming_new_facts(tmp_path):
    from domains.prospecting.service_tiers import ScreeningEvidence
    from infra.db.prospecting_service_tables import ProspectingScreeningEvidenceRow

    async with store_db(tmp_path) as (store, factory):
        evidence = ScreeningEvidence(
            tenant_id=TENANT,
            account_id="acc_one",
            kind="business_match",
            source_ref="sig_source",
            source_url="https://buyer.example.com/about",
            source_quote="We distribute hardware.",
            recorded_at=NOW,
        )
        await store.append_evidence(TENANT, "acc_one", evidence)
        async with factory() as session, session.begin():
            from sqlalchemy import select

            row = await session.scalar(
                select(ProspectingScreeningEvidenceRow).where(
                    ProspectingScreeningEvidenceRow.tenant_id == TENANT,
                    ProspectingScreeningEvidenceRow.account_id == "acc_one",
                )
            )
            row.payload = {**row.payload, "source_quote": "Unsupported replacement"}
        with pytest.raises(ValidationError):
            await store.get_evidence(TENANT, "acc_one")


async def test_logical_evidence_replay_preserves_first_times_but_new_facts_append(
    tmp_path,
):
    from domains.prospecting.service_tiers import ScreeningEvidence

    async with store_db(tmp_path) as (store, _factory):
        evidence = ScreeningEvidence(
            tenant_id=TENANT,
            account_id="acc_one",
            kind="human_review",
            source_ref="sig_expansion",
            source_url="https://buyer.example.com/news",
            source_quote="We are expanding our warehouse.",
            occurred_at=NOW - timedelta(days=3),
            recorded_at=NOW,
            reviewed_by="emp_owner",
            reviewed_at=NOW,
        )
        await store.append_evidence(TENANT, "acc_one", evidence)
        replay = evidence.model_copy(
            update={
                "recorded_at": NOW + timedelta(hours=1),
                "reviewed_at": NOW + timedelta(hours=1),
            }
        )
        await store.append_evidence(TENANT, "acc_one", replay)
        assert await store.get_evidence(TENANT, "acc_one") == (evidence,)
        changed_event = replay.model_copy(
            update={"occurred_at": NOW - timedelta(days=2)}
        )
        changed_reviewer = replay.model_copy(update={"reviewed_by": "emp_second"})
        await store.append_evidence(TENANT, "acc_one", changed_event)
        await store.append_evidence(TENANT, "acc_one", changed_reviewer)
        saved = await store.get_evidence(TENANT, "acc_one")
        assert len(saved) == 3
        assert (
            evidence in saved and changed_event in saved and changed_reviewer in saved
        )


@pytest.mark.parametrize("operation", ["get", "reserve", "evidence"])
async def test_database_failure_is_fixed_error_without_database_details(
    tmp_path, operation
):
    async with store_db(tmp_path) as (store, factory):
        async with factory() as session, session.begin():
            table = (
                "prospecting_screening_evidence"
                if operation == "evidence"
                else "prospecting_service_plans"
            )
            await session.execute(text(f"DROP TABLE {table}"))
        with pytest.raises(TransientError) as caught:
            if operation == "get":
                await store.get(TENANT)
            elif operation == "evidence":
                await store.get_evidence(TENANT, "acc_one")
            else:
                await store.reserve(
                    TENANT, key="missing", candidates=1, searches=1, contacts=1, now=NOW
                )
        assert "SELECT" not in str(caught.value)
        assert "prospecting_service_plans" not in str(caught.value)


async def test_migration_sqlite_roundtrip_and_nonempty_downgrade_is_blocked(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore
    from infra.db.prospecting_service_tables import SERVICE_PLAN_TABLES

    migration = importlib.import_module(
        "migrations.versions.0102_prospecting_service_plans"
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")

    def run_migration(connection, name):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, name)()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE prospect_accounts (tenant_id VARCHAR(40) NOT NULL, "
                    "account_id VARCHAR(40) NOT NULL, PRIMARY KEY (tenant_id, account_id))"
                )
            )
            await connection.run_sync(lambda sync: run_migration(sync, "upgrade"))
            for table in SERVICE_PLAN_TABLES:
                columns = await connection.run_sync(
                    lambda sync, name=table.name: inspect(sync).get_columns(name)
                )
                assert {column["name"]: column["nullable"] for column in columns} == {
                    column.name: column.nullable for column in table.columns
                }
            await connection.run_sync(lambda sync: run_migration(sync, "downgrade"))
            await connection.run_sync(lambda sync: run_migration(sync, "upgrade"))
        await assigned(PostgresProspectingServicePlanStore(async_sessionmaker(engine)))
        async with engine.begin() as connection:
            with pytest.raises(
                RuntimeError, match="prospecting_service_data_prevents_downgrade"
            ):
                await connection.run_sync(lambda sync: run_migration(sync, "downgrade"))
    finally:
        await engine.dispose()


@pytest.mark.db
async def test_migration_postgres_roundtrip_matches_orm(_postgres_container):
    """仅临时 testcontainers；直接 Operations 不加载仓库 .env。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from infra.db.prospecting_service_tables import SERVICE_PLAN_TABLES
    from tests.integration.conftest import _to_asyncpg

    admin_engine = create_async_engine(
        _to_asyncpg(_postgres_container.get_connection_url())
    )
    database_name = "test_prospecting_plans_" + uuid4().hex
    async with admin_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_async_engine(admin_engine.url.set(database=database_name))
    migration = importlib.import_module(
        "migrations.versions.0102_prospecting_service_plans"
    )

    def run_migration(connection, name):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, name)()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS prospect_accounts (tenant_id VARCHAR(40) NOT NULL, "
                    "account_id VARCHAR(40) NOT NULL, PRIMARY KEY (tenant_id, account_id))"
                )
            )
            await connection.run_sync(lambda sync: run_migration(sync, "upgrade"))
            for table in SERVICE_PLAN_TABLES:
                columns = await connection.run_sync(
                    lambda sync, name=table.name: inspect(sync).get_columns(name)
                )
                assert {column["name"]: column["nullable"] for column in columns} == {
                    column.name: column.nullable for column in table.columns
                }
            await connection.run_sync(lambda sync: run_migration(sync, "downgrade"))
            await connection.run_sync(lambda sync: run_migration(sync, "upgrade"))
            await connection.run_sync(lambda sync: run_migration(sync, "downgrade"))
    finally:
        await engine.dispose()
        async with admin_engine.connect() as connection:
            admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database_name}"'))
        await admin_engine.dispose()


@asynccontextmanager
async def postgres_store_db(container):
    """每例从0070真实DDL建立独立数据库，不使用外部配置库。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore
    from tests.integration.conftest import _to_asyncpg

    admin_engine = create_async_engine(_to_asyncpg(container.get_connection_url()))
    database_name = "test_prospecting_runtime_" + uuid4().hex
    async with admin_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_async_engine(admin_engine.url.set(database=database_name))
    migration = importlib.import_module(
        "migrations.versions.0102_prospecting_service_plans"
    )

    def upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE prospect_accounts (tenant_id VARCHAR(40) NOT NULL, "
                    "account_id VARCHAR(40) NOT NULL, PRIMARY KEY (tenant_id, account_id))"
                )
            )
            await connection.run_sync(upgrade)
            await connection.execute(
                text(
                    "INSERT INTO prospect_accounts (tenant_id, account_id) "
                    "VALUES (:tenant, 'acc_one'), ('ten_other', 'acc_other')"
                ),
                {"tenant": TENANT},
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        yield PostgresProspectingServicePlanStore(factory), factory
    finally:
        await engine.dispose()
        async with admin_engine.connect() as connection:
            admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database_name}"'))
        await admin_engine.dispose()


@pytest.mark.db
async def test_postgres_last_quota_serializes_across_independent_stores(
    _postgres_container,
):
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore

    async with postgres_store_db(_postgres_container) as (store, factory):
        await assigned(store)
        await store.reserve(
            TENANT, key="initial", candidates=99, searches=199, contacts=19, now=NOW
        )
        competing = PostgresProspectingServicePlanStore(factory)
        results = await asyncio.gather(
            store.reserve(
                TENANT, key="last-a", candidates=1, searches=1, contacts=1, now=NOW
            ),
            competing.reserve(
                TENANT, key="last-b", candidates=1, searches=1, contacts=1, now=NOW
            ),
            return_exceptions=True,
        )
        assert sum(result is None for result in results) == 1
        assert (
            sum(isinstance(result, InvalidStateTransition) for result in results) == 1
        )
        current = await competing.get(TENANT)
        assert (
            current.candidates_reserved,
            current.searches_reserved,
            current.contacts_reserved,
        ) == (100, 200, 20)
        assert await competing.get("ten_other") is None


@pytest.mark.db
async def test_postgres_restart_preserves_pending_cycle_and_current_period_receipts(
    _postgres_container,
):
    from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore

    async with postgres_store_db(_postgres_container) as (store, factory):
        state = await scheduled(store)
        original = await store.begin_cycle(TENANT, NOW)
        assert await store.claim_cycle_start(
            TENANT, key=original.key, expected_version=state.version, now=NOW
        )
        await store.reserve(
            TENANT, key="contact", candidates=0, searches=0, contacts=1, now=NOW
        )
        restarted = PostgresProspectingServicePlanStore(factory)
        assert (
            await restarted.begin_cycle(TENANT, NOW + timedelta(minutes=1)) == original
        )
        assert (
            await restarted.reservation(TENANT, key="contact", now=NOW)
        ).contacts == 1
        bound = await restarted.bind_cycle(
            TENANT, key=original.key, run_id="run_pg", now=NOW
        )
        assert (
            await store.bind_cycle(TENANT, key=original.key, run_id="run_pg", now=NOW)
            == bound
        )
        expiry = NOW + timedelta(days=30)
        await restarted.assign(
            TENANT,
            "standard",
            expected_version=bound.version,
            actor_id="emp_owner",
            now=expiry,
        )
        assert await store.reservation(TENANT, key="contact", now=expiry) is None
        with pytest.raises(InvalidStateTransition):
            await store.reserve(
                TENANT, key="contact", candidates=0, searches=0, contacts=1, now=expiry
            )


@pytest.mark.db
async def test_postgres_evidence_concurrent_replay_and_tenant_account_foreign_key(
    _postgres_container,
):
    from sqlalchemy.exc import IntegrityError

    from domains.prospecting.service_tiers import ScreeningEvidence
    from infra.db.prospecting_service_tables import ProspectingScreeningEvidenceRow

    async with postgres_store_db(_postgres_container) as (store, factory):
        evidence = ScreeningEvidence(
            tenant_id=TENANT,
            account_id="acc_one",
            kind="business_match",
            source_ref="sig_pg",
            source_url="https://buyer.example.com/about",
            source_quote="We distribute hardware.",
            recorded_at=NOW,
        )
        await asyncio.gather(
            store.append_evidence(TENANT, "acc_one", evidence),
            store.append_evidence(
                TENANT,
                "acc_one",
                evidence.model_copy(update={"recorded_at": NOW + timedelta(minutes=1)}),
            ),
        )
        assert len(await store.get_evidence(TENANT, "acc_one")) == 1
        async with factory() as session:
            session.add(
                ProspectingScreeningEvidenceRow(
                    tenant_id="ten_other",
                    account_id="acc_one",
                    content_hash="0" * 64,
                    payload=evidence.model_dump(mode="json"),
                    recorded_at=NOW,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
        assert await store.get_evidence("ten_other", "acc_one") == ()


async def test_inventory_schedule_keeps_explicit_target_and_request_receipt(tmp_path):
    from domains.prospecting.inventory import InventoryQuery
    async with store_db(tmp_path) as (store, factory):
        plan = await assigned(store)
        query = InventoryQuery(product_tags=('hinges',), country='DE',
            confirmed_refs=({'document_id':'doc-confirmed', 'revision_id':'rev-confirmed'},))
        plan = await store.configure_inventory_schedule(TENANT, query=query,
            employee_id='emp_owner', acting_user_id='usr_owner', enabled=True,
            expected_version=plan.version, now=NOW)
        cycle = await store.begin_cycle(TENANT, NOW)
        assert cycle.target_kind == 'inventory_request' and cycle.query == query
        assert await store.claim_cycle_start(TENANT, key=cycle.key, expected_version=plan.version, now=NOW)
        await store.bind_inventory_cycle(TENANT, key=cycle.key, request_key_hash='a'*64, now=NOW)
        from infra.db.prospecting_service_tables import ProspectingDiscoveryCycleRow
        async with factory() as session:
            row = await session.get(ProspectingDiscoveryCycleRow, (TENANT, cycle.key))
            assert row.run_id is None and row.request_key_hash == 'a'*64
        assert await store.begin_cycle(TENANT, NOW) is None

@pytest.mark.asyncio
async def test_inventory_request_reservation_caps_and_unknown_survive_restart(tmp_path):
    from infra.db.inventory_service_plan import InventoryServicePlanBudget
    from shared.errors import PermissionDenied
    async with store_db(tmp_path) as (store, factory):
        await assigned(store)
        budget = InventoryServicePlanBudget(factory, TENANT, now=lambda: NOW)
        await budget.reserve_request('a' * 64, 10)
        await InventoryServicePlanBudget(factory, TENANT, now=lambda: NOW).reserve_request('a' * 64, 10)
        with pytest.raises(PermissionDenied):
            await budget.reserve_request('b' * 64, 50)
        state = await store.get(TENANT)
        assert (state.candidates_reserved, state.searches_reserved) == (10, 10)


async def test_reserved_inventory_quantity_survives_downgrade_but_cannot_change_or_renew(tmp_path):
    from infra.db.inventory_service_plan import InventoryServicePlanBudget
    from shared.errors import PermissionDenied

    async with store_db(tmp_path) as (store, factory):
        state = await assigned(store, 'advanced')
        budget = InventoryServicePlanBudget(factory, TENANT, now=lambda: NOW)
        await budget.reserve_request('a' * 64, 30)
        state = await store.assign(TENANT, 'basic', expected_version=state.version, actor_id='operator', now=NOW)
        restarted = InventoryServicePlanBudget(factory, TENANT, now=lambda: NOW)
        await restarted.reserve_request('a' * 64, 30)
        for changed_count in (10, 31):
            with pytest.raises(InvalidStateTransition):
                await restarted.reserve_request('a' * 64, changed_count)
        with pytest.raises(PermissionDenied):
            await restarted.reserve_request('b' * 64, 30)
        current = await store.get(TENANT)
        assert (current.candidates_reserved, current.searches_reserved) == (30, 30)
        expiry = NOW + timedelta(days=30)
        expired = InventoryServicePlanBudget(factory, TENANT, now=lambda: expiry)
        with pytest.raises(InvalidStateTransition):
            await expired.reserve_request('a' * 64, 10)
        await store.assign(TENANT, 'basic', expected_version=state.version, actor_id='operator', now=expiry)
        with pytest.raises(InvalidStateTransition):
            await expired.reserve_request('a' * 64, 10)
        current = await store.get(TENANT)
        assert (current.candidates_reserved, current.searches_reserved) == (0, 0)
