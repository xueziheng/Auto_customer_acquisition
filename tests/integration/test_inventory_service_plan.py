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
