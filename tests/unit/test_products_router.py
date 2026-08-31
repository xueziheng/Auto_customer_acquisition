"""Product & Supply Center HTTP 的安全列表和视图隔离。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.products.errors import ProductNotFoundError
from shared.schemas.identifiers import EmployeeId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")


class _Products:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_supply_cards(self, tenant_id, *, actor, source_only, limit):
        self.calls.append((tenant_id, actor, source_only, limit))
        return []

    async def get_internal_view(self, tenant_id, product_id, *, actor):
        raise ProductNotFoundError("产品不存在或租户不匹配")

    async def get_sales_view(self, tenant_id, product_id, *, actor):
        raise ProductNotFoundError("产品不存在或租户不匹配")

    async def get_customer_view(self, tenant_id, product_id, *, actor):
        raise ProductNotFoundError("产品不存在或租户不匹配")


def _identity(role: str) -> RequestIdentity:
    employee = EmployeeView(
        employee_id=EMPLOYEE,
        tenant_id=TENANT,
        name="测试员工",
        role=role,
    )
    return RequestIdentity(
        tenant_id=TENANT,
        employee=employee,
        employee_actor=EmployeeActor(
            str(EMPLOYEE),
            EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF,
            role,
        ),
        opportunity_actor=OpportunityActor(str(EMPLOYEE), OpportunityScope(), role),
    )


def _app(role: str) -> tuple[object, _Products]:
    products = _Products()
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        products=products,
    )
    return app, products


def _get(app: object, path: str) -> Response:
    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.get(
                path,
                headers={
                    "X-Employee-Id": str(EMPLOYEE),
                    "X-Tenant-Id": str(TENANT),
                },
            )

    return asyncio.run(run())


def test_product_supply_list_is_internal_and_preserves_source_only_filter() -> None:
    allowed_app, products = _app("product")
    denied_app, denied_products = _app("sales")

    allowed = _get(allowed_app, "/products?source_only=true&limit=20")
    denied = _get(denied_app, "/products?source_only=true&limit=20")

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert products.calls[0][2:] == (True, 20)
    assert denied_products.calls == []


def test_product_views_hide_missing_or_other_tenant_product_as_not_found() -> None:
    app, _ = _app("boss")
    product_id = "prd_01K39P9M5D6K4A91YEQ80EJZ0X"

    responses = [
        _get(app, f"/products/{product_id}/internal"),
        _get(app, f"/products/{product_id}/sales"),
        _get(app, f"/products/{product_id}/customer"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]
