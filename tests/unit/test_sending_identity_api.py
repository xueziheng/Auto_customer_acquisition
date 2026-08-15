"""Internal Sending Identity API：仅 boss 可访问，严格请求体与域调用。"""

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
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    Phase1SendingIdentityAuthorizer,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.schemas import (
    AuthStatusView,
    DomainRole,
    IdentityState,
    IdentityView,
)
from domains.sending_identity.service import (
    AuthenticationCheckRequestStatus,
    AuthenticationCheckRequestView,
)
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    EmployeeId,
    SendingIdentityId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
BOSS = EmployeeId(new_id("emp"))
SALES = EmployeeId(new_id("emp"))
IDENTITY = SendingIdentityId(new_id("sid"))
OTHER_IDENTITY = SendingIdentityId(new_id("sid"))
REQUEST_KEY = "request-key-2026-v1"


def _employee(employee_id: EmployeeId, role: str) -> EmployeeView:
    return EmployeeView(
        employee_id=employee_id,
        tenant_id=TENANT,
        name="测试员工",
        role=role,
        manager_id=None,
        is_active=True,
    )


class _EmployeeService:
    def __init__(self, employee: EmployeeView) -> None:
        self.employee = employee

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: EmployeeActor
    ) -> EmployeeView:
        del tenant_id, actor
        assert employee_id == self.employee.employee_id
        return self.employee

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        del tenant_id, actor
        return [self.employee]


class _EmployeeScope:
    def __init__(self, service: _EmployeeService) -> None:
        self.service = service

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId) -> AsyncIterator[_EmployeeService]:
        assert tenant_id == TENANT
        yield self.service


class _SendingIdentities:
    def __init__(self) -> None:
        self.list_calls: list[tuple[TenantId, int, SendingIdentityActor]] = []
        self.get_calls: list[tuple[TenantId, SendingIdentityId, SendingIdentityActor]] = []
        self.check_calls: list[
            tuple[TenantId, SendingIdentityId, str, SendingIdentityActor]
        ] = []

    async def list_available_for_campaign(
        self, tenant_id: TenantId, *, limit: int, actor: SendingIdentityActor
    ) -> list[IdentityView]:
        self.list_calls.append((tenant_id, limit, actor))
        return [
            IdentityView(
                identity_id=IDENTITY,
                address="sender-marker@example.test",
                domain="cold.example.test",
                role=DomainRole.COLD_OUTREACH,
                state=IdentityState.ACTIVE,
                created_at=NOW,
                auth=AuthStatusView(
                    checked_at=NOW,
                    spf_passed=True,
                    dkim_passed=True,
                    dmarc_passed=True,
                    failures=(),
                ),
                reputation=None,
                warmup_day=10,
                warmup_complete=False,
            )
        ]

    async def get(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, *, actor: SendingIdentityActor
    ) -> IdentityView:
        self.get_calls.append((tenant_id, identity_id, actor))
        if identity_id != IDENTITY:
            from domains.sending_identity.errors import SendingIdentityNotFoundError

            raise SendingIdentityNotFoundError("发件身份不存在或不属于当前租户")
        return (await self.list_available_for_campaign(tenant_id, limit=1, actor=actor))[0]

    async def request_authentication_check(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        request_key: str,
        *,
        actor: SendingIdentityActor,
    ) -> AuthenticationCheckRequestView:
        self.check_calls.append((tenant_id, identity_id, request_key, actor))
        return AuthenticationCheckRequestView(
            AuthenticationCheckRequestId(new_id("acr")),
            tenant_id,
            identity_id,
            request_key,
            AuthenticationCheckRequestStatus.REQUESTED,
            NOW,
            None,
        )


def _app(employee: EmployeeView, *, authorizer: Any | None = None):
    from apps.api.main import ApiSettings, create_app

    sending = _SendingIdentities()
    dependencies = ConfiguredApiDependencies(
        opportunities=object(),
        outreach=object(),
        sending_identities=sending,
        tool_gateway=_ToolGateway(),
        delivery_materials=_DeliveryMaterials(),
        unsubscribe_links=_UnsubscribeLinks(),
        unsubscribe_service=_UnsubscribeService(),
        employees=_EmployeeScope(_EmployeeService(employee)),
        opportunity_authorizer=object(),
        employee_authorizer=object(),
        workflow_engine=object(),
        outbox_deliverer=object(),
        notification_router=object(),
        notification_dedup_store=object(),
        employee_lookup_actor=EmployeeActor(
            actor_id="system:api-identity", scope=EmployeeScope.SYSTEM, role="system"
        ),
        outreach_authorizer=object(),
        sending_identity_authorizer=(
            authorizer
            if authorizer is not None
            else Phase1SendingIdentityAuthorizer(TENANT)
        ),
        campaign_scope_resolver=object(),
        in_app_notifications=object(),
    )
    app = create_app(
        settings=ApiSettings(tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17),
        dependencies=dependencies,
    )
    return app, sending


class _ToolGateway:
    async def invoke(self, ctx: object) -> object:
        del ctx
        raise AssertionError("本测试不应调用手工发送 Gateway")


class _DeliveryMaterials:
    async def resolve(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不应解析发送材料")


class _UnsubscribeLinks:
    async def build(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("本测试不应生成退订链接")


class _UnsubscribeService:
    async def issue(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不应签发退订 token")

    async def consume(self, opaque_token: str) -> bool:
        del opaque_token
        return False



class _Client:
    def __init__(self, app: Any) -> None:
        self._app = app

    def get(self, path: str, *, headers: dict[str, str]) -> Response:
        return self._request("GET", path, headers=headers)

    def post(self, path: str, *, headers: dict[str, str], json: dict[str, object]) -> Response:
        return self._request("POST", path, headers=headers, json=json)

    def _request(
        self, method: str, path: str, *, headers: dict[str, str], json: dict[str, object] | None = None
    ) -> Response:
        async def run() -> Response:
            transport = ASGITransport(app=self._app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, headers=headers, json=json)

        return asyncio.run(run())


def _headers(employee_id: EmployeeId) -> dict[str, str]:
    return {"X-Tenant-Id": str(TENANT), "X-Employee-Id": str(employee_id)}


def test_boss_lists_sending_identities_with_tenant_scope() -> None:
    app, sending = _app(_employee(BOSS, "boss"))
    response = _Client(app).get("/crm/sending-identities?limit=10", headers=_headers(BOSS))
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["identity_id"] == str(IDENTITY)
    assert body[0]["domain"] == "cold.example.test"
    tenant, limit, actor = sending.list_calls[0]
    assert (tenant, limit) == (TENANT, 10)
    assert actor.role == "boss"
    assert actor.scope.level is SendingIdentityScopeLevel.TENANT


def test_boss_reads_single_identity_and_requests_auth_check() -> None:
    app, sending = _app(_employee(BOSS, "boss"))
    client = _Client(app)
    read = client.get(
        f"/crm/sending-identities/{IDENTITY}", headers=_headers(BOSS)
    )
    assert read.status_code == 200
    assert read.json()["identity_id"] == str(IDENTITY)
    checked = client.post(
        f"/crm/sending-identities/{IDENTITY}/authentication-checks",
        headers=_headers(BOSS),
        json={"request_key": REQUEST_KEY},
    )
    assert checked.status_code == 200
    assert checked.json()["request_key"] == REQUEST_KEY
    assert checked.json()["sending_identity_id"] == str(IDENTITY)
    tenant, identity_id, request_key, actor = sending.check_calls[0]
    assert (tenant, identity_id, request_key) == (TENANT, IDENTITY, REQUEST_KEY)
    assert actor.role == "boss"


def test_auth_check_accepts_only_idempotency_key() -> None:
    app, sending = _app(_employee(BOSS, "boss"))
    client = _Client(app)
    for payload in (
        {"request_key": REQUEST_KEY, "spf": "pass", "dkim": "pass", "dmarc": "pass"},
        {"request_key": REQUEST_KEY, "tenant_id": str(TENANT)},
        {},
    ):
        response = client.post(
            f"/crm/sending-identities/{IDENTITY}/authentication-checks",
            headers=_headers(BOSS),
            json=payload,
        )
        assert response.status_code == 400
        assert "422" not in response.text
    assert sending.check_calls == []


def test_non_boss_roles_are_denied_without_domain_calls() -> None:
    app, sending = _app(_employee(SALES, "sales"))
    client = _Client(app)
    listed = client.get("/crm/sending-identities?limit=10", headers=_headers(SALES))
    assert listed.status_code == 403
    read = client.get(f"/crm/sending-identities/{IDENTITY}", headers=_headers(SALES))
    assert read.status_code == 403
    checked = client.post(
        f"/crm/sending-identities/{IDENTITY}/authentication-checks",
        headers=_headers(SALES),
        json={"request_key": REQUEST_KEY},
    )
    assert checked.status_code == 403
    assert sending.list_calls == [] and sending.get_calls == [] and sending.check_calls == []


def test_invalid_identity_id_is_fixed_400_and_unknown_identity_is_not_found() -> None:
    app, _sending = _app(_employee(BOSS, "boss"))
    client = _Client(app)
    bad = client.get("/crm/sending-identities/not-an-id", headers=_headers(BOSS))
    assert bad.status_code == 400
    missing = client.get(
        f"/crm/sending-identities/{OTHER_IDENTITY}", headers=_headers(BOSS)
    )
    assert missing.status_code == 400
    assert missing.json() == {
        "code": "request_rejected",
        "message": "请求被安全策略拒绝",
    }
