"""Commitment Center API 只操作当前员工自己的承诺。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import ConfiguredApiDependencies
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from tests.provider_readiness_fakes import provider_readiness_dependencies

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
EMPLOYEE = EmployeeId(new_id("emp"))
COMMITMENT = new_id("com")


class _EmployeeService:
    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: EmployeeActor
    ) -> EmployeeView:
        del actor
        return EmployeeView(
            employee_id=employee_id,
            tenant_id=tenant_id,
            name="测试员工",
            role="sales",
            manager_id=None,
            is_active=True,
        )


class _EmployeeScope:
    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId) -> AsyncIterator[_EmployeeService]:
        assert tenant_id == TENANT
        yield _EmployeeService()


class _Commitments:
    def __init__(self) -> None:
        self.list_calls: list[tuple[TenantId, EmployeeId, bool]] = []
        self.confirm_calls: list[tuple[TenantId, str, EmployeeId, str | None]] = []
        self.fulfill_calls: list[tuple[TenantId, str, EmployeeId]] = []

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        include_fulfilled: bool,
    ) -> list[dict[str, object]]:
        self.list_calls.append((tenant_id, employee_id, include_fulfilled))
        return [
            {
                "commitment_id": COMMITMENT,
                "commitment_type": "employee",
                "owner": str(employee_id),
                "action": "Send the revised quotation",
                "due_at": NOW,
                "due_at_uncertain": False,
                "source_message_id": new_id("msg"),
                "verbatim": "I will send the revised quotation tomorrow.",
                "status": "pending",
                "account_id": None,
                "opportunity_id": None,
                "extracted_by": "team-operations-v1",
                "confirmed_by": None,
                "confirmed_at": None,
                "created_at": NOW,
                "fulfilled_at": None,
                "escalated_at": None,
            }
        ]

    async def confirm(
        self,
        tenant_id: TenantId,
        commitment_id: str,
        confirmed_by: EmployeeId,
        corrected_due_at: str | None = None,
    ) -> None:
        self.confirm_calls.append(
            (tenant_id, commitment_id, confirmed_by, corrected_due_at)
        )

    async def fulfill(
        self,
        tenant_id: TenantId,
        commitment_id: str,
        fulfilled_by: EmployeeId,
    ) -> None:
        self.fulfill_calls.append((tenant_id, commitment_id, fulfilled_by))


class _ToolGateway:
    async def invoke(self, ctx: object) -> object:
        del ctx
        raise AssertionError("本测试不调用工具网关")


class _DeliveryMaterials:
    async def resolve(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不解析发送材料")


class _UnsubscribeLinks:
    async def build(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("本测试不生成退订链接")


class _UnsubscribeService:
    async def issue(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不签发退订 token")

    async def consume(self, opaque_token: str) -> bool:
        del opaque_token
        return False


class _Unavailable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"本测试不调用 {name}")


def _app() -> tuple[Any, _Commitments]:
    from apps.api.main import ApiSettings, create_app

    commitments = _Commitments()
    dependencies = ConfiguredApiDependencies(
        opportunities=_Unavailable(),
        outreach=_Unavailable(),
        sending_identities=_Unavailable(),
        tool_gateway=_ToolGateway(),
        delivery_materials=_DeliveryMaterials(),
        unsubscribe_links=_UnsubscribeLinks(),
        unsubscribe_service=_UnsubscribeService(),
        employees=_EmployeeScope(),
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
    object.__setattr__(dependencies, "commitments", commitments)
    return (
        create_app(
            settings=ApiSettings(
                tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
            ),
            dependencies=dependencies,
        ),
        commitments,
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


def test_list_commitments_uses_request_employee_identity() -> None:
    app, commitments = _app()

    response = _request(app, "GET", "/commitments?include_fulfilled=false")

    assert response.status_code == 200
    assert response.json()[0]["commitment_id"] == COMMITMENT
    assert commitments.list_calls == [(TENANT, EMPLOYEE, False)]


def test_confirm_and_fulfill_bind_actor_to_request_identity() -> None:
    app, commitments = _app()

    confirmed = _request(
        app,
        "POST",
        f"/commitments/{COMMITMENT}/confirm",
        json={"corrected_due_at": "2026-08-25T09:30:00+08:00"},
    )
    fulfilled = _request(
        app,
        "POST",
        f"/commitments/{COMMITMENT}/fulfill",
    )

    assert confirmed.status_code == 204
    assert fulfilled.status_code == 204
    assert commitments.confirm_calls == [
        (TENANT, COMMITMENT, EMPLOYEE, "2026-08-25T09:30:00+08:00")
    ]
    assert commitments.fulfill_calls == [(TENANT, COMMITMENT, EMPLOYEE)]


def test_commitment_routes_reject_unknown_body_fields_and_invalid_ids() -> None:
    app, commitments = _app()

    unknown = _request(
        app,
        "POST",
        f"/commitments/{COMMITMENT}/confirm",
        json={"corrected_due_at": None, "confirmed_by": str(EMPLOYEE)},
    )
    invalid = _request(app, "POST", "/commitments/not-an-id/fulfill")

    assert unknown.status_code == 400
    assert invalid.status_code == 400
    assert commitments.confirm_calls == []
    assert commitments.fulfill_calls == []
