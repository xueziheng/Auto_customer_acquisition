"""真实Postgres独立连接并发、持久恢复和迁移验收。"""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.errors import IdempotencyConflict, PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId
from tests.unit.test_prospect_inventory import candidate

NOW = datetime(2026, 10, 13, 8, tzinfo=UTC)


def module():
    try:
        return importlib.import_module("infra.db.prospect_inventory")
    except ModuleNotFoundError:
        pytest.fail("RED：原子库存仓储尚未实现")


@pytest.fixture
async def inventory(integration_engine):
    m = module()
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    control = TenantId("ctl_" + uuid4().hex)
    tenant = TenantId("biz_" + uuid4().hex[:26])
    catalog = m.PostgresPublicCatalog(factory, control, now=lambda: NOW)
    service = m.PostgresInventoryService(factory, tenant, catalog, now=lambda: NOW)
    return service, catalog, factory, tenant, control


async def test_duplicate_domain_email_and_other_company(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, c, factory, t, _ = inventory
    await c.save_public(
        (candidate(), candidate(id="pci_other", canonical_domain="other.com"))
    )
    q = InventoryQuery(product_tags=("hinges",))
    a, b = await asyncio.gather(
        s.request(t, EmployeeId("a"), q, 5, "a"),
        s.request(t, EmployeeId("b"), q, 5, "b"),
    )
    assert len(a.allocations) + len(b.allocations) == 1
    second = module().PostgresInventoryService(
        factory, TenantId("biz_" + uuid4().hex[:26]), c, now=lambda: NOW
    )
    assert (
        len(
            (
                await second.request(second.tenant_id, EmployeeId("c"), q, 5, "a")
            ).allocations
        )
        == 1
    )
    assert len(await s.list_allocations(t, (), 10)) == 0
    with pytest.raises(ValueError):
        await s.status(second.tenant_id)


async def test_idempotence_conflict_quota_and_expiration(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, c, _, t, _ = inventory
    await c.save_public(
        tuple(
            candidate(id=f"pci_{i}", canonical_domain=f"acme{i}.com", public_emails=())
            for i in range(5)
        )
    )
    q = InventoryQuery(product_tags=("hinges",))
    await s.set_daily_limit(t, 2)
    results = await asyncio.gather(
        *(s.request(t, EmployeeId(str(i)), q, 2, str(i)) for i in range(5))
    )
    assert sum(len(r.allocations) for r in results) == 2
    assert (await s.status(t)).remaining_today == 0
    assert await s.request(t, EmployeeId("0"), q, 2, "0") == results[0]
    with pytest.raises(IdempotencyConflict):
        await s.request(t, EmployeeId("different"), q, 2, "0")
    assert (await s.status(t)).reset_at == datetime(2026, 10, 13, 16, tzinfo=UTC)
    await c.save_public(
        (
            candidate(
                id="pci_exp",
                canonical_domain="exp.com",
                expires_at=NOW - timedelta(hours=1),
            ),
        )
    )
    await s.set_daily_limit(t, 10)
    assert (await s.status(t)).fresh_candidates == 5


async def test_job_lease_recovery_fencing_and_original_requester(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, c, factory, t, _ = inventory
    q = InventoryQuery(product_tags=("hinges",))
    result = await s.request(t, EmployeeId("original"), q, 2, "job")
    assert result.job.remaining_count == 2
    job = await s.claim_job(t, timedelta(seconds=30))
    assert job.status == "processing"
    assert await s.claim_job(t, timedelta(seconds=30)) is None
    restarted = module().PostgresInventoryService(
        factory, t, c, now=lambda: NOW + timedelta(seconds=31)
    )
    retry = await restarted.claim_job(t, timedelta(seconds=30))
    assert retry.attempts == 2 and retry.lease_token != job.lease_token
    with pytest.raises(ValueError):
        await restarted.deliver_job(t, job.id, job.lease_token)
    await restarted.record_source_attempt(
        t, retry.id, retry.lease_token, "company_news", "complete", 1
    )
    await c.save_public((candidate(),))
    delivered = await restarted.deliver_job(t, retry.id, retry.lease_token)
    assert len(delivered.allocations) == 1
    assert delivered.allocations[0].employee_id is None
    assert delivered.job.remaining_count == 1 and delivered.job.status == "queued"
    repeat = await restarted.request(t, EmployeeId("original"), q, 2, "job")
    assert repeat == delivered


async def test_list_jobs_visibility_and_failure_transition(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, _, _, t, _ = inventory
    result = await s.request(
        t,
        EmployeeId("owner"),
        InventoryQuery(region="africa", product_tags=("三轮车",)),
        1,
        "africa",
    )
    assert len(await s.list_jobs(t, (EmployeeId("owner"),), 10)) == 1
    assert await s.list_jobs(t, (), 10) == ()
    claimed = await s.claim_job(t, timedelta(seconds=30))
    await s.fail_job(t, claimed.id, claimed.lease_token, "sources_unavailable")
    failed = await s.get_job(t, result.job.id)
    assert failed.status == "failed" and failed.failure_reason == "sources_unavailable"
    assert failed.created_at == NOW and failed.updated_at == NOW


async def test_same_key_concurrency_replays_one_allocation(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, c, _, t, _ = inventory
    await c.save_public((candidate(),))
    results = await asyncio.gather(
        *(
            s.request(
                t, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "same"
            )
            for _ in range(8)
        )
    )
    assert all(r == results[0] for r in results)
    assert len(results[0].allocations) == 1
    assert (await s.status(t)).used_today == 1


async def test_quota_resets_company_midnight_and_fresh_matching(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, c, factory, t, _ = inventory
    await c.save_public(
        (
            candidate(country="KE"),
            candidate(
                id="pci_us", canonical_domain="us.com", country="US", public_emails=()
            ),
        )
    )
    await s.set_daily_limit(t, 1)
    q = InventoryQuery(region="africa", product_tags=("hinges",))
    assert len((await s.request(t, EmployeeId("a"), q, 1, "a")).allocations) == 1
    before = module().PostgresInventoryService(
        factory, t, c, now=lambda: datetime(2026, 10, 13, 15, 59, tzinfo=UTC)
    )
    after = module().PostgresInventoryService(
        factory, t, c, now=lambda: datetime(2026, 10, 13, 16, tzinfo=UTC)
    )
    assert (await before.status(t)).remaining_today == 0
    assert (await after.status(t)).remaining_today == 1


async def test_0073_roundtrip_matches_metadata(integration_engine):
    from sqlalchemy import inspect, text

    from infra.db.session import create_engine_from
    from infra.db.tables import Base
    from tests.integration.test_migrations import _run_alembic

    names = (
        "public_candidates",
        "inventory_quotas",
        "inventory_requests",
        "candidate_allocations",
        "candidate_allocation_emails",
        "acquisition_jobs",
        "acquisition_attempts",
    )
    database = "test_inventory_roundtrip_" + uuid4().hex
    async with integration_engine.connect() as conn:
        admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database}"'))
    url = integration_engine.url.set(database=database).render_as_string(False)
    engine = create_engine_from(url)
    try:
        _run_alembic(url, "upgrade", "head")
        async with engine.connect() as conn:
            inspector = await conn.run_sync(
                lambda c: {n: inspect(c).get_columns(n) for n in names}
            )
            for n in names:
                assert {v["name"]: v["nullable"] for v in inspector[n]} == {
                    c.name: c.nullable for c in Base.metadata.tables[n].columns
                }
            assert (
                await conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_class WHERE relname = ANY(:names) AND relrowsecurity AND relforcerowsecurity"
                    ),
                    {"names": list(names)},
                )
                == 7
            )
        _run_alembic(url, "downgrade", "0073")
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: inspect(c).get_columns("acquisition_attempts")
            )
            assert "diagnostic" not in {v["name"] for v in columns}
        _run_alembic(url, "upgrade", "0074")
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda c: inspect(c).get_columns("acquisition_attempts")
            )
            diagnostic = next(v for v in columns if v["name"] == "diagnostic")
            assert diagnostic["nullable"] and str(diagnostic["type"]) == "JSONB"
        _run_alembic(url, "downgrade", "0072")
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
            assert not set(names) & set(tables)
        _run_alembic(url, "upgrade", "head")
        async with engine.connect() as conn:
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "0075"
            )
    finally:
        await engine.dispose()
        async with integration_engine.connect() as conn:
            admin = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database}"'))


async def test_rls_business_role_cannot_read_public_or_other_company(
    inventory, integration_engine
):
    from sqlalchemy import text

    from infra.db.tables import Base
    from infra.db.tenant_security import tenant_database_role

    s, c, _, t, control = inventory
    await c.save_public((candidate(),))
    from domains.prospecting.inventory import InventoryQuery

    await s.request(
        t, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "rls"
    )
    role = tenant_database_role(t)
    async with integration_engine.begin() as conn:
        await conn.execute(
            text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS')
        )
        names = [
            n
            for n in Base.metadata.tables
            if n
            in (
                "public_candidates",
                "inventory_quotas",
                "inventory_requests",
                "candidate_allocations",
                "candidate_allocation_emails",
                "acquisition_jobs",
                "acquisition_attempts",
            )
        ]
        await conn.execute(text("GRANT SELECT ON " + ",".join(names) + f' TO "{role}"'))
    try:
        async with integration_engine.begin() as conn:
            await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert (
                await conn.scalar(text("SELECT count(*) FROM candidate_allocations"))
                == 1
            )
            assert (
                await conn.scalar(text("SELECT count(*) FROM public_candidates")) == 0
            )
            await conn.execute(
                text("SELECT set_config('app.tenant_id',:other,true)"),
                {"other": control},
            )
            assert (
                await conn.scalar(text("SELECT count(*) FROM public_candidates")) == 0
            )
    finally:
        async with integration_engine.begin() as conn:
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))


async def test_source_history_counts_actual_results(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, _, _, t, _ = inventory
    await s.request(
        t, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "history"
    )
    job = await s.claim_job(t, timedelta(seconds=30))
    await s.record_source_attempt(t, job.id, job.lease_token, "pncp", "complete", 3)
    history = await s.source_history(t)
    assert history[0].source_id == "pncp"
    assert history[0].attempts == 1 and history[0].valid_new_count == 3
    assert history[0].last_used_at == NOW


async def test_public_refresh_keeps_identity_and_old_allocation_snapshot(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, c, _, t, _ = inventory
    await c.save_public((candidate(),))
    result = await s.request(
        t, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "snapshot"
    )
    await c.save_public((candidate(id="pci_new", name="New Acme", observed_at=NOW),))
    refreshed = await c.fresh(None, NOW)
    assert refreshed[0].id == "pci_test" and refreshed[0].name == "New Acme"
    replay = await s.request(
        t, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "snapshot"
    )
    assert replay == result and replay.allocations[0].candidate.name == "Acme"


async def test_public_save_reports_only_new_fresh_domains(inventory):
    _, c, _, _, _ = inventory
    assert await c.save_public((candidate(),)) == 1
    assert await c.save_public((candidate(),)) == 0
    assert (
        await c.save_public(
            (
                candidate(
                    id="pci_expired",
                    canonical_domain="expired.com",
                    expires_at=NOW - timedelta(hours=1),
                ),
            )
        )
        == 0
    )


async def test_lease_rechecked_after_waiting_for_row_lock(inventory):
    from sqlalchemy import select

    from domains.prospecting.inventory import InventoryQuery
    from infra.db.prospect_inventory_tables import AcquisitionJobRow

    s, _, factory, t, _ = inventory
    clock = [NOW]
    s.now = lambda: clock[0]
    await s.request(
        t, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "blocked"
    )
    job = await s.claim_job(t, timedelta(seconds=30))
    async with factory() as blocker, blocker.begin():
        await blocker.execute(
            select(AcquisitionJobRow)
            .where(AcquisitionJobRow.tenant_id == t, AcquisitionJobRow.job_id == job.id)
            .with_for_update()
        )
        task = asyncio.create_task(
            s.record_source_attempt(t, job.id, job.lease_token, "pncp", "complete", 1)
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        clock[0] = NOW + timedelta(seconds=31)
    with pytest.raises(ValueError):
        await task


@pytest.mark.parametrize(
    "new_first",
    [False, True],
    ids=["refresh_with_microseconds", "ignore_old_whole_second"],
)
async def test_public_refresh_uses_timestamp_order_with_same_second(
    inventory, new_first
):
    _, catalog, _, _, _ = inventory
    old = candidate(
        observed_at=NOW,
        name="Old",
        expires_at=NOW + timedelta(days=1),
        public_emails=("old@acme.com",),
    )
    new = candidate(
        observed_at=NOW + timedelta(microseconds=100000),
        name="New",
        expires_at=NOW + timedelta(days=2),
        public_emails=("new@acme.com",),
    )
    first, second = (new, old) if new_first else (old, new)
    await catalog.save_public((first,))
    await catalog.save_public((second,))
    values = await catalog.fresh(None, NOW + timedelta(seconds=1))
    assert len(values) == 1
    assert values[0].observed_at == NOW + timedelta(microseconds=100000)
    assert values[0].name == "New"
    assert values[0].public_emails == ("new@acme.com",)
    assert values[0].expires_at == NOW + timedelta(days=2)


@pytest.mark.parametrize(
    "used_yesterday", [1, 2], ids=["yesterday_has_capacity", "yesterday_exhausted"]
)
async def test_allocation_crossing_midnight_realigns_locked_quota(
    inventory, used_yesterday
):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from domains.prospecting.inventory import InventoryQuery

    s, catalog, factory, tenant, _ = inventory
    clock = [datetime(2026, 10, 13, 15, 59, 59, tzinfo=UTC)]
    s.now = lambda: clock[0]
    await catalog.save_public(
        tuple(
            candidate(
                id=f"pci_midnight_{i}",
                canonical_domain=f"midnight{i}.com",
                public_emails=(),
            )
            for i in range(5)
        )
    )
    await s.set_daily_limit(tenant, 2)
    query = InventoryQuery(product_tags=("hinges",))
    assert (
        len(
            (
                await s.request(
                    tenant, EmployeeId("owner"), query, used_yesterday, "yesterday"
                )
            ).allocations
        )
        == used_yesterday
    )
    async with factory() as blocker, blocker.begin():
        await blocker.execute(
            text("LOCK TABLE candidate_allocations IN ACCESS EXCLUSIVE MODE")
        )
        task = asyncio.create_task(
            s.request(tenant, EmployeeId("owner"), query, 1, "crossing")
        )
        await asyncio.sleep(0.05)
        assert not task.done()
        # 另一连接NOWAIT确认公司额度锁已取得；暂停位置确在分配查询，而非_quota之前。
        with pytest.raises(DBAPIError) as locked:
            async with factory() as probe, probe.begin():
                await probe.execute(
                    text(
                        "SELECT tenant_id FROM inventory_quotas WHERE tenant_id=:tenant FOR UPDATE NOWAIT"
                    ),
                    {"tenant": tenant},
                )
        assert locked.value.orig.sqlstate == "55P03"
        clock[0] = datetime(2026, 10, 13, 16, tzinfo=UTC)
    result = await task
    assert len(result.allocations) == 1
    assert result.allocations[0].allocated_at == datetime(2026, 10, 13, 16, tzinfo=UTC)
    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT quota_day,used_today FROM inventory_quotas WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant},
            )
        ).one()
        assert str(row.quota_day) == "2026-10-14" and row.used_today == 1
    next_result = await s.request(tenant, EmployeeId("owner"), query, 2, "today_next")
    assert len(next_result.allocations) == 1
    assert (await s.status(tenant)).used_today == 2


async def test_inventory_driver_retains_surplus_and_finishes_empty_jobs(inventory):
    from apps.scheduler_worker.inventory_acquisition import InventoryAcquisitionDriver
    from domains.prospecting.collection_result import CollectionResult
    from domains.prospecting.inventory_access import InventoryAccessScope
    from domains.prospecting.projection_diagnostic import ProjectionSummary
    from domains.prospecting.service import InventoryQuery, SourceState

    s, c, _, tenant, _ = inventory

    class Authority:
        async def current_scope(self, t, e):
            return InventoryAccessScope(t, e, frozenset({e}))

    class Sources:
        async def list_sources(self, t):
            return (
                SourceState(
                    source_id="company_websites",
                    availability="available",
                    cost_class="free",
                    relevant=True,
                ),
            )

    class Collector:
        calls = 0

        async def collect(self, job, source, guard, **kwargs):
            self.calls += 1
            await guard()
            return CollectionResult(
                tuple(
                    candidate(
                        id=f"pci_surplus_{i}",
                        canonical_domain=f"surplus{i}.example",
                        public_emails=(),
                    )
                    for i in range(4)
                ),
                ProjectionSummary(
                    input_shape="valid",
                    raw_count=4,
                    projected_count=4,
                    projection_complete=True,
                ),
            )

    collector = Collector()
    driver = InventoryAcquisitionDriver(
        tenant_id=tenant,
        backend=s,
        catalog=c,
        authority=Authority(),
        sources=Sources(),
        collector=collector,
        now=lambda: NOW,
    )
    result = await s.request(
        tenant,
        EmployeeId("original"),
        InventoryQuery(product_tags=("hinges",)),
        1,
        "one",
    )
    assert await driver.scan() == 1
    assert (await s.get_job(tenant, result.job.id)).status == "complete"
    assert len(await c.fresh(None, NOW)) == 4
    assert len(await s.list_allocations(tenant, None, 10)) == 1
    assert (await s.source_history(tenant))[0].valid_new_count == 4
    result = await s.request(
        tenant,
        EmployeeId("original"),
        InventoryQuery(product_tags=("other",)),
        1,
        "empty",
    )
    assert await driver.scan() == 1
    assert await driver.scan() == 1
    assert (await s.get_job(tenant, result.job.id)).status == "failed"
    calls = collector.calls
    assert await driver.scan() == 0 and collector.calls == calls


async def test_inventory_public_save_fences_expired_worker(inventory):
    from domains.prospecting.service import InventoryQuery

    s, c, factory, t, _ = inventory
    await s.request(
        t,
        EmployeeId("owner"),
        InventoryQuery(product_tags=("hinges",)),
        1,
        "save-fence",
    )
    job = await s.claim_job(t, timedelta(seconds=1))
    assert hasattr(s, "save_collected"), "RED: 公共写入尚未持job锁核验lease"
    expired = module().PostgresInventoryService(
        factory, t, c, now=lambda: NOW + timedelta(seconds=2)
    )
    with pytest.raises(ValueError):
        await expired.save_collected(t, job.id, job.lease_token, (candidate(),))
    assert await c.fresh(None, NOW) == ()


async def test_history_cursor_traverses_over_200_and_rechecks_owner_and_tenant(
    inventory,
):
    from domains.prospecting.inventory import InventoryQuery

    s, c, factory, tenant, _ = inventory
    await c.save_public(
        tuple(
            candidate(
                id=f"pci_page_{i}", canonical_domain=f"page{i}.test", public_emails=()
            )
            for i in range(211)
        )
    )
    await s.set_daily_limit(tenant, 500)
    q = InventoryQuery(product_tags=("hinges",))
    for i in range(3):
        await s.request(tenant, EmployeeId("owner"), q, 70, f"page-{i}")
    other = await s.request(tenant, EmployeeId("other"), q, 1, "other")
    from sqlalchemy import select

    from infra.db.prospect_inventory_tables import CandidateAllocationRow

    async with factory() as session, session.begin():
        rows = (
            await session.scalars(
                select(CandidateAllocationRow).where(
                    CandidateAllocationRow.tenant_id == tenant
                )
            )
        ).all()
        for row in rows:
            owner = "other" if row.allocation_id == other.allocations[0].id else "owner"
            row.employee_id = owner
            row.payload = {**row.payload, "employee_id": owner}
    seen, cursor = [], None
    while True:
        page = await s.list_allocations(tenant, (EmployeeId("owner"),), 37, cursor)
        if not page:
            break
        seen.extend(str(a.id) for a in page)
        cursor = str(page[-1].id)
    assert len(seen) == len(set(seen)) == 210
    assert seen == sorted(seen)  # 同一时间按唯一ID升序，跨页无漏无重。
    for invalid in ("unknown", str(other.allocations[0].id)):
        with pytest.raises(ValueError):
            await s.list_allocations(tenant, (EmployeeId("owner"),), 37, invalid)
    second = module().PostgresInventoryService(
        factory, TenantId("biz_" + uuid4().hex[:26]), c, now=lambda: NOW
    )
    with pytest.raises(ValueError):
        await second.list_allocations(second.tenant_id, None, 37, seen[0])
    with pytest.raises(ValueError):
        await s.list_allocations(tenant, (), 37, seen[0])
    await c.save_public(
        (
            candidate(
                id="pci_new_page", canonical_domain="new-page.test", public_emails=()
            ),
        )
    )
    s.now = lambda: NOW + timedelta(seconds=1)
    await s.request(tenant, EmployeeId("owner"), q, 1, "new-page")
    assert [
        str(a.id)
        for a in await s.list_allocations(
            tenant, (EmployeeId("owner"),), 1000, seen[36]
        )
    ] == seen[37:]


@pytest.mark.parametrize(
    "outcome",
    [
        "failed:provider:req:3",
        "failed:page_robots:unknown:3",
        "failed:browser_init:res:4",
    ],
)
async def test_source_attempt_persists_only_safe_bounded_diagnostic(inventory, outcome):
    from sqlalchemy import select

    from domains.prospecting.inventory import InventoryQuery
    from infra.db.prospect_inventory_tables import AcquisitionAttemptRow

    service, _, factory, tenant, _ = inventory
    await service.request(
        tenant,
        EmployeeId("worker"),
        InventoryQuery(product_tags=("hinges",)),
        1,
        "diagnostic",
    )
    job = await service.claim_job(tenant, timedelta(minutes=10))
    await service.record_source_attempt(
        tenant, job.id, job.lease_token, "company_websites", outcome, 0
    )
    with pytest.raises(ValueError, match="来源尝试记录无效"):
        await service.record_source_attempt(
            tenant,
            job.id,
            job.lease_token,
            "company_websites",
            "failed:provider:PRIVATE:3",
            0,
        )
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(AcquisitionAttemptRow).where(
                        AcquisitionAttemptRow.tenant_id == tenant,
                        AcquisitionAttemptRow.job_id == job.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [row.outcome for row in rows] == [outcome]


async def seed_assignment_employees(factory, tenant):
    from infra.db.tables import EmployeeRow

    prefix = uuid4().hex[:10]
    values = {
        role: EmployeeId(f"emp_{prefix}_{role}")
        for role in ("boss", "sales", "manager", "viewer", "inactive")
    }
    async with factory() as session, session.begin():
        for role, employee in values.items():
            session.add(
                EmployeeRow(
                    tenant_id=tenant,
                    employee_id=employee,
                    name="测试员工",
                    role="sales" if role == "inactive" else role,
                    is_active=role != "inactive",
                )
            )
    return values


async def test_new_delivery_is_unassigned_and_assignment_is_atomic_idempotent(
    inventory,
):
    from domains.prospecting.inventory import InventoryQuery

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public(
        tuple(
            candidate(
                id=f"pci_assign_{i}",
                canonical_domain=f"assign{i}.test",
                public_emails=(),
            )
            for i in range(3)
        )
    )
    result = await s.request(
        tenant, employees["boss"], InventoryQuery(product_tags=("hinges",)), 3, "assign"
    )
    assert all(
        a.employee_id is None
        and a.assigned_at is None
        and a.assigned_by is None
        and a.account_id is None
        for a in result.allocations
    )
    ids = tuple(a.id for a in result.allocations)
    for invalid_target in (
        employees["inactive"],
        employees["viewer"],
        EmployeeId("missing"),
    ):
        with pytest.raises(PermissionDenied):
            await s.assign(
                tenant, ids, invalid_target, acting_employee_id=employees["boss"]
            )
    for invalid_actor in (
        employees["sales"],
        employees["manager"],
        employees["inactive"],
    ):
        with pytest.raises(PermissionDenied):
            await s.assign(
                tenant, ids, employees["sales"], acting_employee_id=invalid_actor
            )
    assert all(
        a.employee_id is None for a in await s.list_allocations(tenant, None, 10)
    )
    values = await s.assign(
        tenant, ids[:1], employees["sales"], acting_employee_id=employees["boss"]
    )
    assert (
        values[0].employee_id == employees["sales"]
        and values[0].assigned_by == employees["boss"]
        and values[0].assigned_at == NOW
    )
    assert (
        await s.assign(
            tenant, ids[:1], employees["sales"], acting_employee_id=employees["boss"]
        )
        == values
    )
    with pytest.raises(IdempotencyConflict):
        await s.assign(
            tenant, ids, employees["manager"], acting_employee_id=employees["boss"]
        )
    assert len(await s.list_allocations(tenant, (employees["sales"],), 10)) == 1
    assert (
        len(
            [
                a
                for a in await s.list_allocations(tenant, None, 10)
                if a.employee_id is None
            ]
        )
        == 2
    )
    assert (await s.status(tenant)).used_today == 3
    assert await s.get_allocation(tenant, ids[0]) == values[0]


async def test_assignment_cross_tenant_and_unknown_ids_roll_back(inventory):
    from domains.prospecting.inventory import InventoryQuery
    from shared.schemas.identifiers import CandidateAllocationId

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    foreign = TenantId("biz_" + uuid4().hex[:26])
    foreign_employees = await seed_assignment_employees(factory, foreign)
    await catalog.save_public((candidate(),))
    value = (
        await s.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "cross",
        )
    ).allocations[0]
    for target in (foreign_employees["sales"], employees["sales"]):
        ids = (
            (value.id,)
            if target != employees["sales"]
            else (value.id, CandidateAllocationId("missing"))
        )
        with pytest.raises(PermissionDenied):
            await s.assign(tenant, ids, target, acting_employee_id=employees["boss"])
    assert (await s.get_allocation(tenant, value.id)).employee_id is None
    other = module().PostgresInventoryService(
        factory, foreign, catalog, now=lambda: NOW
    )
    assert await other.get_allocation(foreign, value.id) is None


async def test_concurrent_assignment_has_one_owner_and_unchanged_quota(inventory):
    from domains.prospecting.inventory import InventoryQuery

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    value = (
        await s.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "concurrent",
        )
    ).allocations[0]
    results = await asyncio.gather(
        *(
            s.assign(
                tenant,
                (value.id,),
                employees[role],
                acting_employee_id=employees["boss"],
            )
            for role in ("sales", "manager")
        ),
        return_exceptions=True,
    )
    assert len([r for r in results if isinstance(r, tuple)]) == 1
    assert len([r for r in results if isinstance(r, IdempotencyConflict)]) == 1
    assert (await s.status(tenant)).used_today == 1


async def test_assignment_rechecks_employee_after_lock_wait(inventory):
    from sqlalchemy import select

    from domains.prospecting.inventory import InventoryQuery
    from infra.db.tables import EmployeeRow

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    value = (
        await s.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "revoked",
        )
    ).allocations[0]
    async with factory() as blocker, blocker.begin():
        target = (
            await blocker.scalars(
                select(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == tenant,
                    EmployeeRow.employee_id == employees["sales"],
                )
                .with_for_update()
            )
        ).one()
        pending = asyncio.create_task(
            s.assign(
                tenant,
                (value.id,),
                employees["sales"],
                acting_employee_id=employees["boss"],
            )
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        target.is_active = False
    with pytest.raises(PermissionDenied):
        await pending
    assert (await s.get_allocation(tenant, value.id)).employee_id is None


async def test_existing_private_account_other_owner_refuses_assignment(inventory):
    from domains.prospecting.inventory import InventoryQuery
    from infra.db.tables import OwnershipLockRow, ProspectAccountRow

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    allocation = (
        await s.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "existing",
        )
    ).allocations[0]
    account_id = "acc_" + uuid4().hex[:26]
    async with factory() as session, session.begin():
        session.add(
            ProspectAccountRow(
                tenant_id=tenant,
                account_id=account_id,
                name="Private company",
                country="US",
                website_domain="acme.com",
                source_signal_refs=[],
                field_provenance={},
                created_at=NOW,
            )
        )
        session.add(
            OwnershipLockRow(
                tenant_id=tenant,
                lock_id=uuid4().hex,
                account_id=account_id,
                owner=employees["manager"],
                locked_at=NOW,
                locked_by_rule="test",
            )
        )
    with pytest.raises(IdempotencyConflict):
        await s.assign(
            tenant,
            (allocation.id,),
            employees["sales"],
            acting_employee_id=employees["boss"],
        )
    assert (await s.get_allocation(tenant, allocation.id)).employee_id is None


async def test_assignment_bridge_is_same_transaction_and_retry_can_fill_legacy_mapping(
    inventory,
):
    from domains.prospecting.inventory import InventoryQuery
    from infra.db.tables import ProspectAccountRow
    from shared.schemas.identifiers import ProspectAccountId

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    allocation = (
        await s.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "bridge",
        )
    ).allocations[0]
    calls = []

    class Bridge:
        fail = True

        async def bind(
            self, session, tenant_id, value, employee_id, acting_employee_id
        ):
            assert session.in_transaction() and tenant_id == tenant
            calls.append(value.id)
            account_id = ProspectAccountId("acc_" + uuid4().hex[:26])
            session.add(
                ProspectAccountRow(
                    tenant_id=tenant,
                    account_id=account_id,
                    name="Company",
                    country="US",
                    website_domain="acme.com",
                    source_signal_refs=[],
                    field_provenance={},
                    created_at=NOW,
                )
            )
            await session.flush()
            if self.fail:
                raise ValueError("safe bridge failure")
            return account_id

    bridge = Bridge()
    bridged = module().PostgresInventoryService(
        factory, tenant, catalog, now=lambda: NOW, assignment_bridge=bridge
    )
    with pytest.raises(ValueError):
        await bridged.assign(
            tenant,
            (allocation.id,),
            employees["sales"],
            acting_employee_id=employees["boss"],
        )
    assert (await s.get_allocation(tenant, allocation.id)).employee_id is None
    legacy = (
        await s.assign(
            tenant,
            (allocation.id,),
            employees["sales"],
            acting_employee_id=employees["boss"],
        )
    )[0]
    from sqlalchemy import select

    from infra.db.prospect_inventory_tables import CandidateAllocationRow

    async with factory() as session, session.begin():
        row = (
            await session.scalars(
                select(CandidateAllocationRow).where(
                    CandidateAllocationRow.tenant_id == tenant,
                    CandidateAllocationRow.allocation_id == allocation.id,
                )
            )
        ).one()
        row.assigned_at = row.assigned_by = None
    legacy = await s.get_allocation(tenant, allocation.id)
    bridge.fail = False
    mapped = (
        await bridged.assign(
            tenant,
            (allocation.id,),
            employees["sales"],
            acting_employee_id=employees["boss"],
        )
    )[0]
    assert mapped.account_id is not None and mapped.assigned_at == legacy.assigned_at
    assert await bridged.assign(
        tenant,
        (allocation.id,),
        employees["sales"],
        acting_employee_id=employees["boss"],
    ) == (mapped,)
    assert len(calls) == 2


@pytest.mark.parametrize("target_role", ["boss", "manager", "sales"])
async def test_assignment_target_accepts_current_active_business_roles(
    inventory, target_role
):
    from domains.prospecting.inventory import InventoryQuery

    s, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    value = (
        await s.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "target-role",
        )
    ).allocations[0]
    assigned = await s.assign(
        tenant,
        (value.id,),
        employees[target_role],
        acting_employee_id=employees["boss"],
    )
    assert assigned[0].employee_id == employees[target_role]


async def test_0075_keeps_legacy_owner_and_refuses_lossy_downgrade(integration_engine):
    import json

    from sqlalchemy import text

    from infra.db.session import create_engine_from
    from tests.integration.test_migrations import _run_alembic

    database = "test_inventory_0075_" + uuid4().hex
    async with integration_engine.connect() as connection:
        admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await admin.execute(text(f'CREATE DATABASE "{database}"'))
    url = integration_engine.url.set(database=database).render_as_string(False)
    engine = create_engine_from(url)
    try:
        _run_alembic(url, "upgrade", "0074")
        payload = json.dumps(
            {
                "tenant_id": "tn_legacy",
                "id": "cal_legacy",
                "employee_id": "emp_legacy",
                "candidate": candidate().model_dump(mode="json"),
                "allocated_at": NOW.isoformat(),
                "request_key": "legacy",
            }
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO inventory_requests (tenant_id,key_hash,request_hash) VALUES ('tn_legacy',:key,:key)"
                ),
                {"key": "a" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO candidate_allocations (tenant_id,allocation_id,canonical_domain,employee_id,key_hash,allocated_at,payload) VALUES ('tn_legacy','cal_legacy','acme.com','emp_legacy',:key,:now,CAST(:payload AS jsonb))"
                ),
                {"key": "a" * 64, "now": NOW, "payload": payload},
            )
        _run_alembic(url, "upgrade", "0075")
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT employee_id,assigned_at,assigned_by,account_id FROM candidate_allocations WHERE tenant_id='tn_legacy'"
                    )
                )
            ).one()
            assert tuple(row) == ("emp_legacy", None, None, None)
        _run_alembic(url, "downgrade", "0074")
        _run_alembic(url, "upgrade", "0075")
        for changes in ("employee_id=NULL", "assigned_at=now(),assigned_by='emp_boss'"):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE candidate_allocations SET "
                        + changes
                        + " WHERE tenant_id='tn_legacy'"
                    )
                )
            with pytest.raises(AssertionError, match="alembic downgrade 0074 失败"):
                _run_alembic(url, "downgrade", "0074")
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    == "0075"
                )
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE candidate_allocations SET employee_id='emp_legacy',assigned_at=NULL,assigned_by=NULL WHERE tenant_id='tn_legacy'"
                    )
                )
    finally:
        await engine.dispose()
        async with integration_engine.connect() as connection:
            admin = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin.execute(text(f'DROP DATABASE "{database}"'))


@pytest.mark.parametrize("transfer_to", ["sales", "boss"])
async def test_assignment_and_existing_transfer_do_not_deadlock(inventory, transfer_to):
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.ext.asyncio import AsyncSession

    from domains.prospecting.inventory import InventoryQuery
    from infra.db.repositories.employees import OwnershipRepositoryImpl
    from infra.db.tables import EmployeeRow, OwnershipLockRow, ProspectAccountRow
    from shared.schemas.identifiers import ProspectAccountId, new_id
    from tests.integration.test_employees_repositories import (
        OwnershipLock,
        OwnershipTransfer,
    )

    service, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    allocation = (
        await service.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "transfer-race",
        )
    ).allocations[0]
    account_id = ProspectAccountId(new_id("acc"))
    async with factory() as session, session.begin():
        session.add(
            ProspectAccountRow(
                tenant_id=tenant,
                account_id=account_id,
                name="Private company",
                country="US",
                website_domain="acme.com",
                source_signal_refs=[],
                field_provenance={},
                created_at=NOW,
            )
        )
        session.add(
            OwnershipLockRow(
                tenant_id=tenant,
                lock_id=new_id("loc"),
                account_id=account_id,
                owner=employees["manager"],
                locked_at=NOW,
                locked_by_rule="test",
            )
        )
    employee_locked, resume_assignment, ownership_locked = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    employee_sql = []

    class PausedEmployeeSession(AsyncSession):
        async def scalars(self, statement, *args, **kwargs):
            result = await super().scalars(statement, *args, **kwargs)
            if any(
                description.get("entity") is EmployeeRow
                for description in statement.column_descriptions
            ):
                employee_sql.append(
                    str(statement.compile(dialect=postgresql.dialect()))
                )
                employee_locked.set()
                await resume_assignment.wait()
            return result

    paused_factory = async_sessionmaker(
        factory.kw["bind"], class_=PausedEmployeeSession, expire_on_commit=False
    )
    assignment = module().PostgresInventoryService(
        paused_factory, tenant, catalog, now=lambda: NOW
    )

    async def transfer():
        async with factory() as session, session.begin():
            await session.execute(
                select(OwnershipLockRow)
                .where(
                    OwnershipLockRow.tenant_id == tenant,
                    OwnershipLockRow.account_id == account_id,
                )
                .with_for_update()
            )
            ownership_locked.set()
            await OwnershipRepositoryImpl(session, tenant).replace(
                tenant,
                OwnershipLock(
                    tenant_id=tenant,
                    account_id=account_id,
                    owner=employees[transfer_to],
                    locked_at=NOW,
                    locked_by_rule="test-transfer",
                ),
                OwnershipTransfer(
                    transfer_id=new_id("trf"),
                    tenant_id=tenant,
                    account_id=account_id,
                    from_owner=employees["manager"],
                    to_owner=employees[transfer_to],
                    transferred_by=employees["boss"],
                    transferred_at=NOW,
                    reason="并发归属验收",
                ),
            )

    assignment_task = asyncio.create_task(
        assignment.assign(
            tenant,
            (allocation.id,),
            employees["sales"],
            acting_employee_id=employees["boss"],
        )
    )
    transfer_task = None
    try:
        await asyncio.wait_for(employee_locked.wait(), timeout=5)
        transfer_task = asyncio.create_task(transfer())
        await asyncio.wait_for(ownership_locked.wait(), timeout=5)
        resume_assignment.set()
        assigned, transferred = await asyncio.wait_for(
            asyncio.gather(assignment_task, transfer_task, return_exceptions=True),
            timeout=10,
        )
        assert transferred is None, "既有转派必须完成，不得产生死锁错误"
        if transfer_to == "sales":
            assert isinstance(assigned, tuple), "同归属分配应正常完成，不得产生死锁错误"
            assert assigned[0].employee_id == employees["sales"]
        else:
            assert isinstance(assigned, IdempotencyConflict), (
                "他人归属必须确定性冲突并原子回滚"
            )
        assert len(employee_sql) == 1 and employee_sql[0].endswith("FOR NO KEY UPDATE")
        current = await service.get_allocation(tenant, allocation.id)
        assert current.employee_id == (
            employees["sales"] if transfer_to == "sales" else None
        )
        assert (await service.status(tenant)).used_today == 1
        async with factory() as session:
            locks = (
                await session.scalars(
                    select(OwnershipLockRow).where(
                        OwnershipLockRow.tenant_id == tenant,
                        OwnershipLockRow.account_id == account_id,
                    )
                )
            ).all()
            assert len(locks) == 1 and locks[0].owner == employees[transfer_to]
    finally:
        resume_assignment.set()
        pending = [
            task for task in (assignment_task, transfer_task) if task is not None
        ]
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize("lock_owner", ["sales", "boss"])
async def test_assignment_employee_lock_allows_existing_try_lock(inventory, lock_owner):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from domains.prospecting.inventory import InventoryQuery
    from infra.db.repositories.employees import OwnershipRepositoryImpl
    from infra.db.tables import EmployeeRow, OwnershipLockRow, ProspectAccountRow
    from shared.schemas.identifiers import ProspectAccountId, new_id
    from tests.integration.test_employees_repositories import OwnershipLock

    service, catalog, factory, tenant, _ = inventory
    employees = await seed_assignment_employees(factory, tenant)
    await catalog.save_public((candidate(),))
    allocation = (
        await service.request(
            tenant,
            employees["boss"],
            InventoryQuery(product_tags=("hinges",)),
            1,
            "try-lock-race",
        )
    ).allocations[0]
    account_id = ProspectAccountId(new_id("acc"))
    async with factory() as session, session.begin():
        session.add(
            ProspectAccountRow(
                tenant_id=tenant,
                account_id=account_id,
                name="Private company",
                country="US",
                website_domain="acme.com",
                source_signal_refs=[],
                field_provenance={},
                created_at=NOW,
            )
        )
    employee_locked, resume_assignment = asyncio.Event(), asyncio.Event()

    class PausedEmployeeSession(AsyncSession):
        async def scalars(self, statement, *args, **kwargs):
            result = await super().scalars(statement, *args, **kwargs)
            if any(
                description.get("entity") is EmployeeRow
                for description in statement.column_descriptions
            ):
                employee_locked.set()
                await resume_assignment.wait()
            return result

    class Bridge:
        async def bind(
            self, session, tenant_id, value, employee_id, acting_employee_id
        ):
            repository = OwnershipRepositoryImpl(session, tenant_id)
            await repository.try_lock(
                OwnershipLock(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    owner=employee_id,
                    locked_at=NOW,
                    locked_by_rule="assignment",
                )
            )
            owner = await repository.get(tenant_id, account_id)
            if owner is None or owner.owner != employee_id:
                raise IdempotencyConflict("客户已有其他负责人")
            return account_id

    async def take_ownership():
        async with factory() as session, session.begin():
            return await OwnershipRepositoryImpl(session, tenant).try_lock(
                OwnershipLock(
                    tenant_id=tenant,
                    account_id=account_id,
                    owner=employees[lock_owner],
                    locked_at=NOW,
                    locked_by_rule="existing-try-lock",
                )
            )

    paused_factory = async_sessionmaker(
        factory.kw["bind"], class_=PausedEmployeeSession, expire_on_commit=False
    )
    assignment = module().PostgresInventoryService(
        paused_factory, tenant, catalog, now=lambda: NOW, assignment_bridge=Bridge()
    )
    assignment_task = asyncio.create_task(
        assignment.assign(
            tenant,
            (allocation.id,),
            employees["sales"],
            acting_employee_id=employees["boss"],
        )
    )
    try:
        await asyncio.wait_for(employee_locked.wait(), timeout=5)
        # assignment仍持有员工锁；外键写入必须独立完成，不能靠释放员工锁才能推进。
        assert await asyncio.wait_for(take_ownership(), timeout=3)
        resume_assignment.set()
        if lock_owner == "sales":
            assigned = await asyncio.wait_for(assignment_task, timeout=5)
            assert (
                assigned[0].employee_id == employees["sales"]
                and assigned[0].account_id == account_id
            )
        else:
            with pytest.raises(IdempotencyConflict):
                await asyncio.wait_for(assignment_task, timeout=5)
            assert (
                await service.get_allocation(tenant, allocation.id)
            ).employee_id is None
        async with factory() as session:
            owners = (
                await session.scalars(
                    select(OwnershipLockRow).where(
                        OwnershipLockRow.tenant_id == tenant,
                        OwnershipLockRow.account_id == account_id,
                    )
                )
            ).all()
            assert len(owners) == 1 and owners[0].owner == employees[lock_owner]
        assert (await service.status(tenant)).used_today == 1
    finally:
        resume_assignment.set()
        if not assignment_task.done():
            assignment_task.cancel()
        await asyncio.gather(assignment_task, return_exceptions=True)
