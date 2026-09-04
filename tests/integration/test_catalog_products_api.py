"""Catalog Products HTTP 的真实 PostgreSQL 幂等与未知提交恢复契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.tables import CatalogProposalPolicyVersionRow
from shared.schemas.identifiers import EmployeeId, RunId, TenantId, new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 每例独立真实 PostgreSQL
)
from workflows.catalog_product_proposal import CatalogProductApplication

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


class _Engine:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.calls = 0

    async def start(self, *args, **kwargs):
        del kwargs
        self.calls += 1
        del args
        if self.fail_first and self.calls == 1:
            raise RuntimeError("workflow database detail")
        return RunId(new_id("run"))


class _NoApprovals:
    async def get_catalog_link_state_for_reader(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("未绑定审批的策略不得读取审批")


def _identity(tenant: TenantId, employee: EmployeeId) -> RequestIdentity:
    view = EmployeeView(
        employee_id=employee,
        tenant_id=tenant,
        name="Catalog product owner",
        role="product",
    )
    return RequestIdentity(
        tenant_id=tenant,
        employee=view,
        employee_actor=EmployeeActor(
            str(employee), EmployeeScope.SELF, "product"
        ),
        opportunity_actor=OpportunityActor(
            str(employee), OpportunityScope(), "product"
        ),
    )


def _app(engine: AsyncEngine, workflow: _Engine):
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    products = CatalogProposalServiceImpl(
        lambda scoped: SqlAlchemyCatalogProductsUnitOfWork(sessions, scoped),
        Phase2ProductAuthorizer(tenant),
        now=lambda: NOW,
    )
    system_actor = ProductActor(
        "system:catalog-products-api-test", ProductRole.SYSTEM, tenant
    )
    application = CatalogProductApplication(
        SimpleNamespace(), products, workflow, system_actor  # type: ignore[arg-type]
    )
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(tenant), dev_mode=True, retry_after_seconds=11
        )
    )
    app.state.catalog_test_tenant = tenant
    app.state.catalog_test_employee = employee
    app.dependency_overrides[get_request_identity] = lambda: _identity(
        tenant, employee
    )
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        catalog_products=products,
        catalog_product_application=application,
        approvals=_NoApprovals(),
    )
    return app, tenant, sessions


async def _post(app, body: dict[str, object], key: str):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/products/catalog-policies",
            json=body,
            headers={
                "Idempotency-Key": key,
                "X-Employee-Id": str(app.state.catalog_test_employee),
                "X-Tenant-Id": str(app.state.catalog_test_tenant),
            },
        )


async def _count(sessions, tenant: TenantId) -> int:
    async with sessions() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CatalogProposalPolicyVersionRow)
                .where(CatalogProposalPolicyVersionRow.tenant_id == str(tenant))
            )
            or 0
        )


async def _seed_actor(engine: AsyncEngine, app) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (employee_id,tenant_id,name,role) "
                "VALUES (:employee,:tenant,'Catalog API product','product')"
            ),
            {
                "employee": str(app.state.catalog_test_employee),
                "tenant": str(app.state.catalog_test_tenant),
            },
        )


async def test_catalog_policy_http_replay_converges_and_mismatch_conflicts(
    unit_engine: AsyncEngine,
) -> None:
    workflow = _Engine()
    app, tenant, sessions = _app(unit_engine, workflow)
    await _seed_actor(unit_engine, app)
    body = {
        "minimum_distinct_accounts": 3,
        "minimum_recurring_accounts": None,
        "minimum_distinct_countries": None,
        "minimum_quantity_unit_accounts": None,
        "require_unified_unit": False,
    }

    first = await _post(app, body, "catalog-api-stable-key")
    replay = await _post(app, body, "catalog-api-stable-key")
    mismatch = await _post(
        app,
        {**body, "minimum_distinct_accounts": 4},
        "catalog-api-stable-key",
    )

    assert (first.status_code, replay.status_code, mismatch.status_code) == (
        202,
        202,
        409,
    )
    assert first.json()["policy"]["policy_version_id"] == replay.json()["policy"][
        "policy_version_id"
    ]
    assert await _count(sessions, tenant) == 1
    assert workflow.calls == 2


async def test_catalog_policy_http_unknown_workflow_start_recovers_with_same_key(
    unit_engine: AsyncEngine,
) -> None:
    workflow = _Engine(fail_first=True)
    app, tenant, sessions = _app(unit_engine, workflow)
    await _seed_actor(unit_engine, app)
    body = {
        "minimum_distinct_accounts": 3,
        "minimum_recurring_accounts": None,
        "minimum_distinct_countries": None,
        "minimum_quantity_unit_accounts": None,
        "require_unified_unit": False,
    }

    uncertain = await _post(app, body, "catalog-api-uncertain-key")
    recovered = await _post(app, body, "catalog-api-uncertain-key")

    assert uncertain.status_code == 503
    assert "workflow database detail" not in uncertain.text
    assert recovered.status_code == 202
    assert await _count(sessions, tenant) == 1
