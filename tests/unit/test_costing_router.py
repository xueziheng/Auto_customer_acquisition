"""人工成本 API 的身份绑定、Decimal 边界与审批旁路测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import ConfiguredApiDependencies
from domains.costing.permissions import CostingActor
from domains.costing.schemas import CostItemCreate, CostSheetCreate, QuoteReadiness
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    TenantId,
    new_id,
)
from tests.provider_readiness_fakes import provider_readiness_dependencies

TENANT = TenantId("tenant-costing-api")
EMPLOYEE = EmployeeId("employee-costing-api")
OPPORTUNITY = OpportunityId(new_id("opp"))
COST_SHEET = CostSheetId(new_id("cost"))


class _EmployeeService:
    def __init__(self, role: str) -> None:
        self._role = role

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: EmployeeActor
    ) -> EmployeeView:
        del actor
        return EmployeeView(
            employee_id=employee_id,
            tenant_id=tenant_id,
            name="成本员工",
            role=self._role,
            manager_id=None,
            is_active=True,
        )


class _EmployeeScope:
    def __init__(self, role: str) -> None:
        self._role = role

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId) -> AsyncIterator[_EmployeeService]:
        assert tenant_id == TENANT
        yield _EmployeeService(self._role)


class _Costing:
    def __init__(self) -> None:
        self.created: list[tuple[TenantId, OpportunityId, CostSheetCreate, CostingActor]] = []
        self.added: list[tuple[TenantId, CostSheetId, CostItemCreate, CostingActor]] = []
        self.assessed: list[tuple[TenantId, CostSheetId, tuple[str, ...], CostingActor]] = []

    async def create_sheet(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        command: CostSheetCreate,
        *,
        actor: CostingActor,
    ) -> CostSheetId:
        self.created.append((tenant_id, opportunity_id, command, actor))
        return COST_SHEET

    async def add_item(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostItemCreate,
        *,
        actor: CostingActor,
    ) -> None:
        self.added.append((tenant_id, cost_sheet_id, command, actor))

    async def assess_for_quote(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        expected_item_types: tuple[str, ...],
        *,
        actor: CostingActor,
    ) -> QuoteReadiness:
        self.assessed.append(
            (tenant_id, cost_sheet_id, expected_item_types, actor)
        )
        return QuoteReadiness(ready=False, missing_items=["packaging"])

    async def get_sheet(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("本测试不读取成本表")

    async def list_versions(self, *args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        return []


class _ToolGateway:
    async def invoke(self, ctx: object) -> object:
        del ctx
        raise AssertionError("本测试不调用工具网关")


class _DeliveryMaterials:
    async def resolve(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("本测试不解析发送材料")


class _UnsubscribeLinks:
    async def build(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("本测试不生成退订链接")


class _UnsubscribeService:
    async def issue(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("本测试不签发退订 token")

    async def consume(self, opaque_token: str) -> bool:
        del opaque_token
        return False


class _Unavailable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本测试不调用 {name}")


def _app(role: str = "finance") -> tuple[Any, _Costing]:
    from apps.api.main import ApiSettings, create_app

    costing = _Costing()
    dependencies = ConfiguredApiDependencies(
        opportunities=_Unavailable(),
        outreach=_Unavailable(),
        sending_identities=_Unavailable(),
        tool_gateway=_ToolGateway(),
        delivery_materials=_DeliveryMaterials(),
        unsubscribe_links=_UnsubscribeLinks(),
        unsubscribe_service=_UnsubscribeService(),
        employees=_EmployeeScope(role),
        opportunity_authorizer=_Unavailable(),
        employee_authorizer=_Unavailable(),
        workflow_engine=_Unavailable(),
        outbox_deliverer=_Unavailable(),
        notification_router=_Unavailable(),
        notification_dedup_store=_Unavailable(),
        outreach_authorizer=_Unavailable(),
        sending_identity_authorizer=_Unavailable(),
        campaign_scope_resolver=_Unavailable(),
        in_app_notifications=_Unavailable(),
        employee_lookup_actor=EmployeeActor(
            "system:api-identity", EmployeeScope.SYSTEM, "system"
        ),
        **provider_readiness_dependencies(TENANT),
    )
    object.__setattr__(dependencies, "costing", costing)
    return (
        create_app(
            settings=ApiSettings(
                tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
            ),
            dependencies=dependencies,
        ),
        costing,
    )


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
) -> Response:
    async def run() -> Response:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request(
                method,
                path,
                headers={
                    "X-Tenant-Id": str(TENANT),
                    "X-Employee-Id": str(EMPLOYEE),
                },
                json=json,
            )

    return asyncio.run(run())


def test_costing_openapi_exposes_manual_writes_without_lock_or_risk_bypass() -> None:
    app, _costing = _app()
    paths = app.openapi()["paths"]

    assert "/costing-quotes/opportunities/{opportunity_id}/cost-sheets" in paths
    assert "/costing-quotes/cost-sheets/{cost_sheet_id}" in paths
    assert "/costing-quotes/cost-sheets/{cost_sheet_id}/items" in paths
    assert "/costing-quotes/cost-sheets/{cost_sheet_id}/readiness" in paths
    assert "/costing-quotes/cost-sheets/{cost_sheet_id}/lock" not in paths
    assert not any("accept-indicative-risk" in path for path in paths)


def test_create_sheet_binds_opportunity_and_employee_from_server_context() -> None:
    app, costing = _app()

    response = _request(
        app,
        "POST",
        f"/costing-quotes/opportunities/{OPPORTUNITY}/cost-sheets",
        json={
            "version_type": "quoted",
            "quantity": 100,
            "base_currency": "USD",
            "quote_currency": "EUR",
            "fx_snapshot_id": "fx-api-one",
            "fx_rates": [],
        },
    )

    assert response.status_code == 201
    assert response.json() == {"cost_sheet_id": str(COST_SHEET)}
    tenant_id, opportunity_id, command, actor = costing.created[0]
    assert tenant_id == TENANT
    assert opportunity_id == OPPORTUNITY
    assert command.quantity == 100
    assert actor.actor_id == EMPLOYEE
    assert actor.role == "finance"


def test_add_item_accepts_decimal_string_and_rejects_json_number() -> None:
    app, costing = _app()
    body: dict[str, object] = {
        "item_type": "product_purchase",
        "amount": "12.50",
        "currency": "USD",
        "price_basis": "quoted",
        "is_per_unit": True,
        "source_ref": "supplier-quote-artifact-1",
    }

    accepted = _request(
        app,
        "POST",
        f"/costing-quotes/cost-sheets/{COST_SHEET}/items",
        json=body,
    )
    rejected = _request(
        app,
        "POST",
        f"/costing-quotes/cost-sheets/{COST_SHEET}/items",
        json={**body, "amount": 12.5},
    )

    assert accepted.status_code == 204
    assert costing.added[0][2].amount == Decimal("12.50")
    assert costing.added[0][3].actor_id == EMPLOYEE
    assert rejected.status_code == 400
    assert len(costing.added) == 1


def test_readiness_is_read_only_and_uses_explicit_scenario_items() -> None:
    app, costing = _app()

    response = _request(
        app,
        "POST",
        f"/costing-quotes/cost-sheets/{COST_SHEET}/readiness",
        json={"expected_item_types": ["product_purchase", "packaging"]},
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert response.json()["missing_items"] == ["packaging"]
    assert costing.assessed[0][2] == ("product_purchase", "packaging")


def test_first_api_gate_rejects_role_before_costing_service() -> None:
    app, costing = _app("sales")

    response = _request(
        app,
        "GET",
        f"/costing-quotes/opportunities/{OPPORTUNITY}/cost-sheets",
    )

    assert response.status_code == 403
    assert costing.created == []
