"""Team & Territory 查询只允许 boss，并复用员工域的双重授权。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    EmployeeScope,
    Phase1EmployeeAuthorizer,
)
from domains.employees.schemas import EmployeeView, TerritoryAssignmentView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from shared.schemas.identifiers import EmployeeId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
BOSS = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
SALES = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0Y")
NOW = datetime(2026, 8, 23, 9, tzinfo=UTC)


class _Employees:
    def __init__(self) -> None:
        self.calls: list[tuple[str, EmployeeActor]] = []

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        assert tenant_id == TENANT
        self.calls.append(("employees", actor))
        return [
            EmployeeView(
                employee_id=SALES,
                tenant_id=TENANT,
                name="张三",
                role="sales",
                languages=["zh", "en"],
                timezone="Asia/Shanghai",
            )
        ]

    async def list_territory_matrix(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[TerritoryAssignmentView]:
        assert tenant_id == TENANT
        self.calls.append(("territory", actor))
        return [
            TerritoryAssignmentView(
                tenant_id=TENANT,
                employee_id=SALES,
                priority=1,
                effective_from=NOW,
                countries=["US"],
                need_categories=["hinges"],
            )
        ]


class _Scope:
    def __init__(self, service: _Employees) -> None:
        self._service = service

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId) -> AsyncIterator[_Employees]:
        assert tenant_id == TENANT
        yield self._service


def _identity(role: str = "boss") -> RequestIdentity:
    employee_id = BOSS if role == "boss" else SALES
    scope = EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF
    employee = EmployeeView(
        employee_id=employee_id,
        tenant_id=TENANT,
        name="老板" if role == "boss" else "张三",
        role=role,
    )
    return RequestIdentity(
        tenant_id=TENANT,
        employee=employee,
        employee_actor=EmployeeActor(str(employee_id), scope, role),
        opportunity_actor=OpportunityActor(str(employee_id), OpportunityScope(), role),
    )


def _app(role: str = "boss") -> tuple[object, _Employees]:
    service = _Employees()
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        employee_authorizer=Phase1EmployeeAuthorizer(TENANT),
        employees=_Scope(service),
    )
    return app, service


def _get(app: object, path: str) -> Response:
    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.get(
                path,
                headers={
                    "X-Employee-Id": str(BOSS),
                    "X-Tenant-Id": str(TENANT),
                },
            )

    return asyncio.run(run())


def test_boss_reads_active_employees_and_stable_territory_matrix() -> None:
    app, service = _app()

    employees = _get(app, "/team/employees")
    territory = _get(app, "/team/territory")

    assert employees.status_code == 200
    assert employees.json()[0]["name"] == "张三"
    assert territory.status_code == 200
    assert territory.json()[0]["countries"] == ["US"]
    assert service.calls == [
        ("employees", _identity().employee_actor),
        ("territory", _identity().employee_actor),
    ]


def test_non_boss_is_rejected_before_team_service_call() -> None:
    app, service = _app("sales")

    response = _get(app, "/team/employees")

    assert response.status_code == 403
    assert service.calls == []
