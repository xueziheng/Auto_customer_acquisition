"""Internal outreach API：角色门、作用域推导、域调用与零写入拒绝。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import ConfiguredApiDependencies
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope, Phase1OutreachAuthorizer
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    EnrollmentState,
    EnrollmentView,
    MessageAttemptState,
    MessageAttemptView,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    CampaignId,
    EmployeeId,
    EnrollmentId,
    MessageAttemptId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.provider_readiness_fakes import provider_readiness_dependencies

NOW = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
BOSS = EmployeeId(new_id("emp"))
SALES = EmployeeId(new_id("emp"))
MANAGER = EmployeeId(new_id("emp"))
REPORT = EmployeeId(new_id("emp"))
CAMPAIGN_A = CampaignId(new_id("cmp"))
CAMPAIGN_B = CampaignId(new_id("cmp"))
ENROLLMENT_A = EnrollmentId(new_id("enr"))
ENROLLMENT_B = EnrollmentId(new_id("enr"))
ATTEMPT = MessageAttemptId(new_id("mat"))


def _employee(employee_id: EmployeeId, role: str, manager_id: EmployeeId | None = None) -> EmployeeView:
    return EmployeeView(
        employee_id=employee_id,
        tenant_id=TENANT,
        name="测试员工",
        role=role,
        manager_id=manager_id,
        is_active=True,
    )


class _EmployeeService:
    def __init__(self, employee: EmployeeView, *, active: list[EmployeeView]) -> None:
        self.employee = employee
        self.active = active

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
        return list(self.active)


class _EmployeeScope:
    def __init__(self, service: _EmployeeService) -> None:
        self.service = service

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId) -> AsyncIterator[_EmployeeService]:
        assert tenant_id == TENANT
        yield self.service


@dataclass
class _Outreach:
    list_calls: list[tuple[Any, Any, int, Any]] | None = None
    get_calls: list[tuple[Any, Any, Any]] | None = None
    prepare_calls: list[tuple[Any, Any, Any]] | None = None

    async def list_enrollments(
        self, tenant_id: TenantId, scope: OutreachScope, *, limit: int, actor: OutreachActor
    ) -> list[EnrollmentView]:
        self.list_calls.append((tenant_id, scope, limit, actor))
        return [
            EnrollmentView(
                tenant_id=TENANT,
                enrollment_id=ENROLLMENT_A,
                campaign_id=CAMPAIGN_A,
                campaign_version=1,
                account_id=new_id("acc"),
                contact_point_id=new_id("cp"),
                sending_identity_id=SendingIdentityId(new_id("sid")),
                state=EnrollmentState.IN_SEQUENCE,
                current_step=1,
                next_send_at=NOW,
                enrolled_at=NOW,
                stopped_at=None,
                stop_reason=None,
            )
        ]

    async def get_enrollment(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId, *, actor: OutreachActor
    ) -> EnrollmentView:
        self.get_calls.append((tenant_id, enrollment_id, actor))
        if enrollment_id != ENROLLMENT_A:
            raise PermissionDenied("Phase 1 触达授权拒绝")
        return (await self.list_enrollments(tenant_id, actor.scope, limit=1, actor=actor))[0]

    async def prepare_message_attempt(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId, *, actor: OutreachActor
    ) -> MessageAttemptView:
        self.prepare_calls.append((tenant_id, enrollment_id, actor))
        return MessageAttemptView(
            tenant_id=TENANT,
            attempt_id=ATTEMPT,
            message_id=new_id("msg"),
            campaign_id=CAMPAIGN_A,
            enrollment_id=enrollment_id,
            campaign_version=1,
            step_number=1,
            sending_identity_id=SendingIdentityId(new_id("sid")),
            idempotency_key="api-prepare-key",
            state=MessageAttemptState.RESERVED,
            provider_ref=None,
            failure_category=None,
            created_at=NOW,
            updated_at=NOW,
        )


class _Resolver:
    def __init__(self, campaign_ids: frozenset[CampaignId]) -> None:
        self.campaign_ids = campaign_ids
        self.calls: list[frozenset[str]] = []

    async def campaign_ids_for(self, *, created_by: frozenset[str]) -> frozenset[CampaignId]:
        self.calls.append(created_by)
        return self.campaign_ids


class _DenyOutreachAuthorizer:
    def preauthorize(self, *args: object, **kwargs: object) -> str:
        raise PermissionDenied("Phase 1 触达授权拒绝")


def _app(
    employee: EmployeeView,
    *,
    active: list[EmployeeView],
    resolver_campaigns: frozenset[CampaignId] = frozenset({CAMPAIGN_A}),
    authorizer: Any | None = None,
):
    from apps.api.main import ApiSettings, create_app

    outreach = _Outreach(list_calls=[], get_calls=[], prepare_calls=[])
    resolver = _Resolver(resolver_campaigns)
    employee_service = _EmployeeService(employee, active=active)
    dependencies = ConfiguredApiDependencies(
        opportunities=object(),
        outreach=outreach,
        sending_identities=object(),
        tool_gateway=_ToolGateway(),
        delivery_materials=_DeliveryMaterials(),
        unsubscribe_links=_UnsubscribeLinks(),
        unsubscribe_service=_UnsubscribeService(),
        employees=_EmployeeScope(employee_service),
        opportunity_authorizer=object(),
        employee_authorizer=object(),
        workflow_engine=object(),
        outbox_deliverer=object(),
        notification_router=object(),
        notification_dedup_store=object(),
        employee_lookup_actor=EmployeeActor(
            actor_id="system:api-identity", scope=EmployeeScope.SYSTEM, role="system"
        ),
        outreach_authorizer=(
            authorizer
            if authorizer is not None
            else Phase1OutreachAuthorizer(TENANT)
        ),
        sending_identity_authorizer=object(),
        campaign_scope_resolver=resolver,
        in_app_notifications=object(),
        **provider_readiness_dependencies(TENANT),
    )
    app = create_app(
        settings=ApiSettings(tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17),
        dependencies=dependencies,
    )
    return app, outreach, resolver


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

    def post(self, path: str, *, headers: dict[str, str], json: dict[str, object] | None = None) -> Response:
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


def test_sales_list_sees_only_own_campaign_enrollments() -> None:
    app, outreach, resolver = _app(
        _employee(SALES, "sales"), active=[_employee(SALES, "sales")]
    )
    client = _Client(app)
    response = client.get("/crm/enrollments?limit=10", headers=_headers(SALES))
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["enrollment_id"] == str(ENROLLMENT_A)
    assert body[0]["campaign_id"] == str(CAMPAIGN_A)
    assert set(body[0]) == {
        "tenant_id", "enrollment_id", "campaign_id", "campaign_version",
        "account_id", "contact_point_id", "sending_identity_id", "state",
        "current_step", "next_send_at", "enrolled_at", "stopped_at", "stop_reason",
    }
    assert resolver.calls == [frozenset({str(SALES)})]
    tenant, scope, limit, actor = outreach.list_calls[0]
    assert tenant == TENANT and limit == 10
    assert scope.level is OutreachScopeLevel.SELF
    assert scope.allowed_campaign_ids == frozenset({CAMPAIGN_A})
    assert actor.role == "sales"
    assert actor.actor_id == str(SALES)


def test_manager_list_scope_covers_self_and_direct_reports() -> None:
    app, outreach, resolver = _app(
        _employee(MANAGER, "manager"),
        active=[_employee(MANAGER, "manager"), _employee(REPORT, "sales", manager_id=MANAGER)],
    )
    response = _Client(app).get("/crm/enrollments?limit=10", headers=_headers(MANAGER))
    assert response.status_code == 200
    assert resolver.calls == [frozenset({str(MANAGER), str(REPORT)})]
    _, scope, _, actor = outreach.list_calls[0]
    assert scope.level is OutreachScopeLevel.MANAGER
    assert scope.allowed_campaign_ids == frozenset({CAMPAIGN_A})
    assert actor.role == "manager"


def test_boss_list_uses_tenant_scope_without_resolver() -> None:
    app, outreach, resolver = _app(
        _employee(BOSS, "boss"), active=[_employee(BOSS, "boss")]
    )
    response = _Client(app).get("/crm/enrollments?limit=10", headers=_headers(BOSS))
    assert response.status_code == 200
    assert resolver.calls == []
    _, scope, _, actor = outreach.list_calls[0]
    assert scope.level is OutreachScopeLevel.TENANT
    assert actor.role == "boss"


def test_non_crm_role_is_denied_without_domain_writes() -> None:
    viewer = EmployeeId(new_id("emp"))
    app, outreach, _resolver = _app(
        _employee(viewer, "viewer"), active=[_employee(viewer, "viewer")]
    )
    response = _Client(app).get("/crm/enrollments?limit=10", headers=_headers(viewer))
    assert response.status_code == 403
    assert outreach.list_calls == []


def test_prepare_requires_ownership_then_uses_exact_system_scope() -> None:
    app, outreach, _resolver = _app(
        _employee(SALES, "sales"), active=[_employee(SALES, "sales")]
    )
    client = _Client(app)
    ok = client.post(
        f"/crm/enrollments/{ENROLLMENT_A}/attempts/prepare", headers=_headers(SALES)
    )
    assert ok.status_code == 200
    assert ok.json()["attempt_id"] == str(ATTEMPT)
    assert ok.json()["enrollment_id"] == str(ENROLLMENT_A)
    tenant, enrollment_id, actor = outreach.prepare_calls[0]
    assert (tenant, enrollment_id) == (TENANT, ENROLLMENT_A)
    assert actor.role == "system"
    assert actor.scope.level is OutreachScopeLevel.SYSTEM
    assert actor.scope.allowed_enrollment_ids == frozenset({ENROLLMENT_A})

    denied = client.post(
        f"/crm/enrollments/{ENROLLMENT_B}/attempts/prepare", headers=_headers(SALES)
    )
    assert denied.status_code == 403
    assert len(outreach.prepare_calls) == 1


def test_prepare_rejects_invalid_enrollment_id() -> None:
    app, outreach, _resolver = _app(
        _employee(BOSS, "boss"), active=[_employee(BOSS, "boss")]
    )
    response = _Client(app).post(
        "/crm/enrollments/not-an-id/attempts/prepare", headers=_headers(BOSS)
    )
    assert response.status_code == 400
    assert outreach.prepare_calls == []


def test_list_and_prepare_never_accept_scope_role_or_tenant_body_fields() -> None:
    app, outreach, _resolver = _app(
        _employee(BOSS, "boss"), active=[_employee(BOSS, "boss")]
    )
    client = _Client(app)
    listed = client.get(
        "/crm/enrollments?limit=10&role=spoof&scope=tenant&tenant=other",
        headers=_headers(BOSS),
    )
    assert listed.status_code == 200
    prepared = client.post(
        f"/crm/enrollments/{ENROLLMENT_A}/attempts/prepare",
        headers=_headers(BOSS),
        json={"role": "system", "tenant_id": "other", "scope": "tenant"},
    )
    # 无 body 参数的端点不读取任何请求体：伪造的 role/scope/tenant 一律忽略。
    assert prepared.status_code == 200
    assert len(outreach.prepare_calls) == 1
    _, _, actor = outreach.prepare_calls[0]
    assert actor.role == "system"
    assert actor.scope.allowed_enrollment_ids == frozenset({ENROLLMENT_A})
