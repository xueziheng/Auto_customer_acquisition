"""原30天额度与唯一库存任务共用回执；所有来源累计上限不随恢复扩张。"""
from datetime import timedelta

import pytest

from domains.prospecting.service import InventoryQuery
from infra.db.inventory_service_plan import InventoryServicePlanBudget
from infra.db.prospecting_service_plans import PostgresProspectingServicePlanStore
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId
from tests.integration.test_prospect_inventory import NOW
from tests.integration.test_prospect_inventory import inventory as inventory_fixture

inventory = inventory_fixture

async def test_basic_candidate_page_budget_and_unknown_are_cross_source_durable(inventory):
    service, _, factory, tenant, _ = inventory
    store = PostgresProspectingServicePlanStore(factory)
    state = await store.assign(tenant, 'basic', expected_version=None, actor_id='operator', now=NOW)
    query = InventoryQuery(product_tags=('steel',))
    result = await service.request(tenant, EmployeeId('emp'), query, 10, 'cycle-one')
    assert result.job is not None
    with pytest.raises(PermissionDenied):
        await service.request(tenant, EmployeeId('other'), query, 10, 'same-target')
    assert (await store.get(tenant)).candidates_reserved == 10
    job = await service.claim_job(tenant, timedelta(minutes=10))
    budget = InventoryServicePlanBudget(factory, tenant, now=lambda:NOW)
    assert (await budget.limits(job))['candidates'] == 10
    await budget.reserve_execution(job, kind='candidates', key='source-one', amount=7)
    restarted = InventoryServicePlanBudget(factory, tenant, now=lambda:NOW)
    with pytest.raises(PermissionDenied):
        await restarted.reserve_execution(job, kind='candidates', key='source-two', amount=4)
    with pytest.raises(PermissionDenied):
        await restarted.reserve_execution(job, kind='candidates', key='source-one', amount=7)
    await restarted.reserve_execution(job, kind='pages', key='page-one', amount=1, host='example.com')
    await restarted.reserve_execution(job, kind='pages', key='page-two', amount=1, host='example.com')
    with pytest.raises(PermissionDenied):
        await restarted.reserve_execution(job, kind='pages', key='page-three', amount=1, host='example.com')
    await store.assign(tenant,'advanced',expected_version=state.version,actor_id='operator',now=NOW)
    assert (await restarted.limits(job)) == {'candidates': 10,'searches': 10,'pages': 10,'pages_per_company': 2}
    assert await restarted.find_request('cycle-one') is not None

pytest_plugins = ('tests.integration.test_tenant_row_security',)

async def test_service_plan_tables_use_current_tenant_roles_and_forced_rls(plan_role_database):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from infra.db.prospecting_service_tables import SERVICE_PLAN_TABLES
    db = plan_role_database
    stores = [PostgresProspectingServicePlanStore(async_sessionmaker(app)) for app in db.apps]
    for tenant, store in zip(db.tenants, stores, strict=True):
        await store.assign(tenant,'basic',expected_version=None,actor_id='operator',now=NOW)
        await store.reserve(tenant,key='same-key',candidates=1,searches=1,contacts=1,now=NOW)
    for index, app in enumerate(db.apps):
        own, other = db.tenants[index], db.tenants[1-index]
        async with app.connect() as connection:
            for table in SERVICE_PLAN_TABLES:
                rows = (await connection.execute(text(f'SELECT tenant_id FROM {table.name}'))).scalars().all()
                assert set(rows) <= {own}
            assert await connection.scalar(text('SELECT count(*) FROM prospecting_service_plans WHERE tenant_id=:other'),{'other':other}) == 0
        with pytest.raises(DBAPIError):
            async with app.begin() as connection:
                await connection.execute(text('UPDATE prospecting_service_plans SET tenant_id=:other WHERE tenant_id=:own'),{'own':own,'other':other})
    async with db.admin.connect() as connection:
        for table in SERVICE_PLAN_TABLES:
            assert await connection.scalar(text('SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname=:name'),{'name':table.name})


@pytest.fixture
async def plan_role_database(isolated_database):
    from sqlalchemy import text

    from infra.db.prospecting_service_tables import SERVICE_PLAN_TABLES
    try:
        yield isolated_database
    finally:
        async with isolated_database.admin.begin() as connection:
            for table in reversed(SERVICE_PLAN_TABLES):
                await connection.execute(text(f'DELETE FROM {table.name} WHERE tenant_id IN (:first,:second)'),
                    {'first':isolated_database.tenants[0],'second':isolated_database.tenants[1]})


@pytest.mark.parametrize('backend_id', ['deepseek', 'tavily'])
@pytest.mark.parametrize('platform_cap', [24, 7])
async def test_actual_search_assembly_intersects_advanced_and_platform_caps(
    inventory, monkeypatch, tmp_path, backend_id, platform_cap
):
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import select

    from apps.scheduler_worker import standalone_inventory as assembly
    from infra.db.prospect_inventory_tables import AcquisitionJobRow
    from shared.schemas.search_backend import InventorySearchPolicy

    service, _, factory, tenant, control = inventory
    store = PostgresProspectingServicePlanStore(factory)
    state = await store.assign(tenant, 'advanced', expected_version=None, actor_id='operator', now=NOW)
    await service.request(tenant, EmployeeId('emp'), InventoryQuery(product_tags=('steel',)), 30, 'advanced-assembly')
    job = await service.claim_job(tenant, timedelta(minutes=10))
    budget = InventoryServicePlanBudget(factory, tenant, now=lambda: NOW)
    assert (await budget.limits(job))['searches'] == 30

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(assembly, 'datetime', Clock)
    monkeypatch.setattr(assembly, 'deployment_verification', lambda _: None)
    # 只替换当前模型身份读取；装配、两种后端预算与持久行锁均走正式代码。
    monkeypatch.setattr(assembly, 'InventoryModelAuthority', lambda **_: SimpleNamespace(identity=AsyncMock(), check=AsyncMock()))
    monkeypatch.setattr(assembly, 'resolve_task_model', lambda *_: SimpleNamespace(
        provider='deepseek', api_base_url='https://api.deepseek.com',
        api_key_file=tmp_path / 'never-read', api_connection_version='cv1', api_control_tenant=control))
    settings = SimpleNamespace(provider='deepseek', configuration_version='cv1', inventory_search=InventorySearchPolicy(
        version='p1', backend_id=backend_id, paid_search_enabled=True, maximum_requests=platform_cap))
    research = SimpleNamespace(object_transport=object(), maximum_artifact_bytes=1024, exclusive_account_confirmed=True)

    def build():
        return assembly.build_inventory_driver(core=SimpleNamespace(employee_scope=object()),
            sessions=factory, control_sessions=factory, control_tenant=control, tenant_id=tenant,
            settings=settings, resolver=object(), fingerprints=object(), research=research, owner='test')

    async def cap():
        async with factory() as session:
            row = await session.scalar(select(AcquisitionJobRow).where(
                AcquisitionJobRow.tenant_id == tenant, AcquisitionJobRow.job_id == job.id))
            return row.search_binding['effective_request_cap']

    driver = build()
    await driver.inventory.sources.validate_binding(job, AsyncMock())
    assert await cap() == platform_cap
    state = await store.assign(tenant, 'standard', expected_version=state.version, actor_id='operator', now=NOW)
    await driver.inventory.sources.validate_binding(job, AsyncMock())
    assert await cap() == min(platform_cap, 20)
    state = await store.assign(tenant, 'advanced', expected_version=state.version, actor_id='operator', now=NOW)
    await build().inventory.sources.validate_binding(job, AsyncMock())
    assert await cap() == min(platform_cap, 20)
    await store.assign(tenant, 'basic', expected_version=state.version, actor_id='operator', now=NOW)
    await build().inventory.sources.validate_binding(job, AsyncMock())
    assert await cap() == min(platform_cap, 10)
    assert (await store.get(tenant)).searches_reserved == 30


@pytest.mark.parametrize('transition', ['same', 'upgrade', 'downgrade', 'pause', 'expiry', 'renew', 'authority', 'context'])
async def test_pending_cycle_recovers_independent_reservation_after_real_request_rollback(
    inventory, monkeypatch, transition
):
    from hashlib import sha256
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import func, select

    from apps.scheduler_worker.scheduled_inventory import ScheduledInventoryDriver
    from domains.prospecting.service import tier_policy
    from infra.db.prospect_inventory import PostgresInventoryService
    from infra.db.prospect_inventory_tables import (
        AcquisitionJobRow,
        InventoryRequestRow,
    )
    from shared.errors import InvalidStateTransition, TransientError

    service, catalog, factory, tenant, _ = inventory
    store = PostgresProspectingServicePlanStore(factory)
    initial_tier = 'advanced' if transition == 'downgrade' else 'basic'
    policy = tier_policy(initial_tier)
    state = await store.assign(tenant, initial_tier, expected_version=None, actor_id='operator', now=NOW)
    query = InventoryQuery(product_tags=('steel',), confirmed_refs=({'document_id': 'doc', 'revision_id': 'rev'},))
    state = await store.configure_inventory_schedule(tenant, query=query, employee_id='emp', acting_user_id='usr',
        enabled=True, expected_version=state.version, now=NOW)
    await store.reserve(tenant, key='prior-cost', candidates=policy.candidate_limit-policy.per_run_signal_limit,
        searches=policy.search_limit-policy.per_run_search_limit, contacts=0, now=NOW)
    clock = [NOW]
    authority, context = SimpleNamespace(authorize_schedule=AsyncMock()), SimpleNamespace(read=AsyncMock())

    def driver(requests):
        return ScheduledInventoryDriver(SimpleNamespace(scan=AsyncMock(return_value=0)), tenant_id=tenant,
            store=PostgresProspectingServicePlanStore(factory), requests=requests,
            receipts=InventoryServicePlanBudget(factory, tenant, now=lambda: clock[0]),
            authority=authority, context=context, now=lambda: clock[0])

    # 已 flush InventoryRequest 后故障，真实事务回滚；独立额度事务必须保留。
    monkeypatch.setattr(service, '_allocate', AsyncMock(side_effect=TransientError('合成事务中断')))
    with pytest.raises(TransientError):
        await driver(service).submit_due()
    cycle = await store.begin_cycle(tenant, NOW)
    digest = sha256(cycle.key.encode()).hexdigest()
    receipt = await store.reservation(tenant, key='inventory:' + digest, now=NOW)
    assert receipt.candidates == policy.per_run_signal_limit
    assert receipt.searches == policy.per_run_search_limit
    async with factory() as session:
        for table in (InventoryRequestRow, AcquisitionJobRow):
            assert await session.scalar(select(func.count()).select_from(table).where(table.tenant_id == tenant)) == 0
    assert (await store.get(tenant)).candidates_reserved == policy.candidate_limit

    if transition in {'upgrade', 'downgrade'}:
        await store.assign(tenant, 'advanced' if transition == 'upgrade' else 'basic',
            expected_version=state.version, actor_id='operator', now=NOW)
    elif transition == 'pause':
        await store.configure_inventory_schedule(tenant, query=query, employee_id='emp', acting_user_id='usr',
            enabled=False, expected_version=state.version, now=NOW)
    elif transition in {'expiry', 'renew'}:
        clock[0] = NOW + timedelta(days=30)
        if transition == 'renew':
            await store.assign(tenant, 'basic', expected_version=state.version, actor_id='operator', now=clock[0])
    elif transition == 'authority':
        authority.authorize_schedule.side_effect = PermissionDenied('当前身份已撤销')
    elif transition == 'context':
        context.read.side_effect = PermissionDenied('当前产品已撤回')
    restarted = PostgresInventoryService(factory, tenant, catalog, now=lambda: clock[0])
    resumed = driver(restarted)
    if transition in {'same', 'upgrade', 'downgrade'}:
        assert await resumed.submit_due() == 1
        assert await resumed.submit_due() == 0
        state = await store.get(tenant)
        assert (state.candidates_reserved, state.searches_reserved) == (policy.candidate_limit, policy.search_limit)
        original = await restarted.request(tenant, EmployeeId('emp'), query, policy.per_run_signal_limit, cycle.key)
        assert original.job.requested_count == policy.per_run_signal_limit
        assert (await InventoryServicePlanBudget(factory, tenant, now=lambda: NOW).limits(original.job))['candidates'] == (
            10 if transition == 'downgrade' else policy.per_run_signal_limit)
        authority.authorize_schedule.assert_awaited_with(tenant, EmployeeId('emp'), 'usr')
        context.read.assert_awaited_with(tenant, EmployeeId('emp'), query.confirmed_refs)
    else:
        if transition in {'authority', 'context', 'renew'}:
            with pytest.raises((PermissionDenied, InvalidStateTransition)):
                await resumed.submit_due()
        else:
            assert await resumed.submit_due() == 0
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(InventoryRequestRow).where(InventoryRequestRow.tenant_id == tenant)) == 0
        state = await store.get(tenant)
        assert state.candidates_reserved == (0 if transition == 'renew' else policy.candidate_limit)
        assert await store.begin_cycle(tenant, clock[0]) == cycle
