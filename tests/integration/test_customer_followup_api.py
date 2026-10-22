"""真实Postgres归属与API读取；无真实采集、验证或发送。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from apps.api.composition.customer_assignment import _NoContactHasher
from apps.api.composition.customer_followup import CustomerFollowupApplication
from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.prospecting.inventory import InventoryQuery
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.customer_followup import PostgresCustomerContextReader
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.tables import EmployeeRow
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_customer_assignment_bridge import (  # noqa: F401
    NOW,
)
from tests.integration.test_customer_assignment_bridge import (
    bridge_inventory as bridge_inventory,  # noqa: PLC0414 -- 共享真实PG夹具
)
from tests.unit.test_prospect_inventory import candidate


async def app_fixture(bridge_inventory, role="sales", employee=None):
    inv, catalog, factory, tenant, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await inv.request(
            tenant, boss, InventoryQuery(product_tags=("hinges",)), 1, "api"
        )
    ).allocations[0]
    allocation = (
        await inv.assign(tenant, (allocation.id,), sales, acting_employee_id=boss)
    )[0]
    employee = employee or (boss if role == "boss" else sales)
    identity = RequestIdentity(
        tenant,
        EmployeeView(employee_id=employee, tenant_id=tenant, name="测试", role=role),
        EmployeeActor(
            str(employee),
            EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF,
            role,
        ),
        Actor(
            str(employee),
            OpportunityScope(
                level=ScopeLevel.TENANT if role == "boss" else ScopeLevel.SELF,
                allowed_owners=frozenset({employee}),
            ),
            role,
        ),
    )
    prospecting = ProspectingServiceImpl(
        lambda t: SqlAlchemyProspectingUnitOfWork(factory, t), _NoContactHasher()
    )
    demand = SimpleNamespace(
        list_account_outreach_hypotheses=AsyncMock(return_value=[])
    )
    outreach = SimpleNamespace(list_enrollments=AsyncMock(return_value=[]))
    application = CustomerFollowupApplication(
        PostgresCustomerContextReader(factory, tenant),
        prospecting,
        demand,
        outreach,
        automatic_enabled=False,
    )
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=17)
    )
    app.dependency_overrides[get_request_identity] = lambda: identity
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        customer_followup=application
    )
    app.state.prospect_inventory_factory = lambda _: inv
    return app, allocation, employee, outreach


async def test_own_customer_detail_options_and_access_are_truthful(bridge_inventory):
    app, allocation, _, outreach = await app_fixture(bridge_inventory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": app.state.settings.tenant_id},
    ) as client:
        access = await client.get("/prospects/customers/access")
        assert access.status_code == 200 and access.json() == {
            "role": "sales",
            "can_search": False,
            "can_assign": False,
        }
        response = await client.get(f"/prospects/customers/{allocation.id}")
        assert response.status_code == 200
        detail = response.json()
        assert detail["account"]["account_id"] == allocation.account_id
        assert set(detail["blocking_reasons"]) == {
            "contact_unverified",
            "customer_type_missing",
            "product_fit_missing",
            "automatic_followup_disabled",
        }
        assert detail["contacts"] == [] and "request_key" not in detail["allocation"]
        options = await client.get(f"/prospects/customers/{allocation.id}/follow-up")
        assert options.status_code == 200 and options.json()["contacts"] == []
        listed = await client.get("/prospects/customers")
        assert listed.status_code == 200 and listed.json()[0]["id"] == allocation.id
        bad = await client.post(
            f"/prospects/customers/{allocation.id}/follow-up",
            json={"campaign_id": "cmp_bad"},
        )
        assert bad.status_code == 400
    assert not outreach.list_enrollments.call_args.kwargs[
        "actor"
    ].scope.allowed_campaign_ids


async def test_transfer_revokes_old_employee_private_customer_access(bridge_inventory):
    from domains.prospecting.inventory import InventoryTransferRequest

    inv, _, _, tenant, boss, sales, other = bridge_inventory
    app, allocation, _, _ = await app_fixture(bridge_inventory)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": tenant},
    ) as client:
        assert (
            await client.get(f"/prospects/customers/{allocation.id}")
        ).status_code == 200
        body = InventoryTransferRequest(
            employee_id=other,
            expected_employee_id=sales,
            expected_assigned_at=allocation.assigned_at,
            reason="管理员安排转交",
        )
        assert (
            await client.post(
                f"/prospects/inventory/allocations/{allocation.id}/transfer",
                json=body.model_dump(mode="json"),
            )
        ).status_code == 403
        await inv.transfer(tenant, allocation.id, body, acting_employee_id=boss)
        assert (
            await client.get(f"/prospects/customers/{allocation.id}")
        ).status_code == 403
        assert (await client.get("/prospects/customers")).json() == []


async def test_other_owner_unknown_customer_and_current_disabled_role_are_rejected(
    bridge_inventory,
):
    _, _, factory, t, _, _, other = bridge_inventory
    app, allocation, _, _ = await app_fixture(bridge_inventory, employee=other)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": app.state.settings.tenant_id},
    ) as client:
        assert (
            await client.get(f"/prospects/customers/{allocation.id}")
        ).status_code == 403
        assert (await client.get("/prospects/customers/cal_foreign")).status_code == 403
        async with factory() as s, s.begin():
            await s.execute(
                update(EmployeeRow)
                .where(EmployeeRow.tenant_id == t, EmployeeRow.employee_id == other)
                .values(is_active=False)
            )
        assert (await client.get("/prospects/customers/access")).status_code == 403


async def test_boss_can_read_company_detail_but_not_start_for_sales(bridge_inventory):
    app, allocation, _, _ = await app_fixture(bridge_inventory, role="boss")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": app.state.settings.tenant_id},
    ) as client:
        assert (
            await client.get(f"/prospects/customers/{allocation.id}")
        ).status_code == 200
        response = await client.post(
            f"/prospects/customers/{allocation.id}/follow-up",
            headers={"Idempotency-Key": "original"},
            json={
                "campaign_id": new_id("cmp"),
                "contact_point_id": new_id("cp"),
                "source_hypothesis_id": new_id("hyp"),
                "campaign_version": 1,
            },
        )
        assert response.status_code == 403


async def test_cross_tenant_context_reader_rejects_before_lookup(bridge_inventory):
    _, _, factory, tenant, _, sales, _ = bridge_inventory
    reader = PostgresCustomerContextReader(factory, tenant)
    from shared.errors import PermissionDenied

    with pytest.raises(PermissionDenied):
        async with reader.guard(TenantId(new_id("tn")), sales, None):
            pass
