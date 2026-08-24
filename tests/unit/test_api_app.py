"""S3-12 API 工厂、租户断言、身份推导、错误转换与 DI 行为测试。"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from typing import Annotated, Any

import pytest
from fastapi import Depends, HTTPException
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError as PydanticValidationError

from apps.api.dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
    require_opportunity_action,
)
from apps.api.identity import RequestIdentity
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    EmployeeScope,
)
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import (
    OpportunityAction,
    ScopeLevel,
)
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    PolicyViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import EmployeeId, TenantId

_IDENTITY_DEPENDENCY = Depends(get_request_identity)
_CONFIGURED_DEPENDENCY = Depends(get_api_dependencies)
_CRM_READ_GATE = Depends(
    require_opportunity_action(
        OpportunityAction.OPPORTUNITY_READ,
        allowed_roles=frozenset({"sales", "manager", "boss"}),
    )
)
_SALES_READ_GATE = Depends(
    require_opportunity_action(
        OpportunityAction.OPPORTUNITY_READ,
        allowed_roles=frozenset({"sales"}),
    )
)
_EXPECTED_API_PATHS = {
    "/approvals/pending",
    "/approvals/{approval_id}",
    "/approvals/{approval_id}/decide",
    "/commands/discovery-proposals",
    "/commands/discovery-proposals/{proposal_id}",
    "/commands/discovery-proposals/{proposal_id}/confirm",
    "/commands/discovery-proposals/{proposal_id}/reject",
    "/commitments",
    "/commitments/overdue",
    "/commitments/{commitment_id}/confirm",
    "/commitments/{commitment_id}/fulfill",
    "/costing-quotes/cost-sheets/{cost_sheet_id}",
    "/costing-quotes/cost-sheets/{cost_sheet_id}/items",
    "/costing-quotes/cost-sheets/{cost_sheet_id}/readiness",
    "/costing-quotes/opportunities/{opportunity_id}/cost-sheets",
    "/crm/analytics/loss-reasons",
    "/crm/campaigns",
    "/crm/campaigns/{campaign_id}",
    "/crm/campaigns/{campaign_id}/activate",
    "/crm/campaigns/{campaign_id}/cancel",
    "/crm/campaigns/{campaign_id}/enrollments",
    "/crm/campaigns/{campaign_id}/pause",
    "/crm/campaigns/{campaign_id}/revise",
    "/crm/campaigns/{campaign_id}/submit",
    "/crm/enrollments",
    "/crm/enrollments/{enrollment_id}/attempts/prepare",
    "/crm/handoffs",
    "/crm/handoffs/{handoff_id}",
    "/crm/handoffs/{handoff_id}/accept",
    "/crm/message-attempts/{attempt_id}/send",
    "/crm/opportunities",
    "/crm/opportunities/{opportunity_id}",
    "/crm/opportunities/{opportunity_id}/transition",
    "/crm/opportunities/{opportunity_id}/mark-lost",
    "/crm/sending-identities",
    "/crm/sending-identities/{identity_id}",
    "/crm/sending-identities/{identity_id}/authentication-checks",
    "/demand/clusters",
    "/demand/clusters/{cluster_id}",
    "/demand/hypotheses",
    "/demand/hypotheses/{hypothesis_id}",
    "/demand/needs",
    "/demand/needs/{need_id}",
    "/demand/signals",
    "/inbox/conversations",
    "/inbox/conversations/{conversation_id}",
    "/inbox/messages/{message_id}/correct-classification",
    "/notifications",
    "/notifications/{notification_id}/read",
    "/products/status",
    "/prospects/accounts",
    "/prospects/accounts/{account_id}",
    "/prospects/accounts/{account_id}/contacts",
    "/prospects/discoveries",
    "/runs",
    "/runs/{run_id}",
    "/settings/playbook",
    "/settings/playbook/versions",
    "/settings/playbook/proposals",
    "/settings/country-policies",
    "/settings/country-policies/versions",
    "/settings/country-policies/proposals",
    "/sourcing/status",
    "/team/employees",
    "/team/territory",
    "/work-uploads",
    "/work-uploads/{upload_id}/artifact",
    "/work-uploads/{upload_id}/extraction",
    "/work-uploads/{upload_id}/confirm",
}


class _ApiClient:
    """避免 Starlette TestClient 的废弃告警，直接经 httpx ASGI transport 请求。"""

    def __init__(self, app: Any, *, raise_server_exceptions: bool = True) -> None:
        self._app = app
        self._raise_server_exceptions = raise_server_exceptions

    def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> Response:
        return self.request("GET", path, headers=headers)

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
    ) -> Response:
        async def request() -> Response:
            transport = ASGITransport(
                app=self._app,
                raise_app_exceptions=self._raise_server_exceptions,
            )
            async with AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, path, headers=headers)

        return asyncio.run(request())


class _AllowAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object, TenantId]] = []

    def require(
        self, actor: object, action: object, scope: object, tenant_id: TenantId
    ) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        return "test:allow"


class _DenyAuthorizer:
    def require(
        self, actor: object, action: object, scope: object, tenant_id: TenantId
    ) -> str:
        raise PermissionDenied("含敏感上下文的拒绝原因")


class _EmployeeService:
    def __init__(
        self,
        employee: EmployeeView,
        *,
        active: list[EmployeeView] | None = None,
    ) -> None:
        self.employee = employee
        self.active = list(active or [employee])
        self.calls: list[tuple[str, TenantId, EmployeeId, EmployeeActor]] = []

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView:
        self.calls.append(("get", tenant_id, employee_id, actor))
        return self.employee

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        self.calls.append(("list", tenant_id, EmployeeId(""), actor))
        return list(self.active)


class _EmployeeServiceScope:
    def __init__(self, service: _EmployeeService) -> None:
        self.service = service
        self.tenants: list[TenantId] = []
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId):
        self.tenants.append(tenant_id)
        self.entered += 1
        try:
            yield self.service
        finally:
            self.exited += 1


class _ToolGateway:
    async def invoke(self, ctx: object) -> object:
        del ctx
        raise AssertionError("本测试不应调用手工发送 Gateway")


class _DeliveryMaterials:
    async def resolve(self, tenant_id: object, preflight: object) -> object:
        del tenant_id, preflight
        raise AssertionError("本测试不应解析发送材料")


class _UnsubscribeLinks:
    async def build(self, tenant_id: object, preflight: object) -> str:
        del tenant_id, preflight
        raise AssertionError("本测试不应生成退订链接")


class _UnsubscribeService:
    async def issue(self, tenant_id: object, preflight: object) -> object:
        del tenant_id, preflight
        raise AssertionError("本测试不应签发退订 token")

    async def consume(self, opaque_token: str) -> bool:
        del opaque_token
        return False


def _employee(
    *,
    employee_id: str = "emp-sales",
    tenant_id: str = "tenant-a",
    role: str = "sales",
    manager_id: str | None = None,
    active: bool = True,
) -> EmployeeView:
    return EmployeeView(
        employee_id=EmployeeId(employee_id),
        tenant_id=TenantId(tenant_id),
        name="测试员工",
        role=role,
        manager_id=EmployeeId(manager_id) if manager_id is not None else None,
        is_active=active,
    )


def _configured_dependencies(
    service: _EmployeeService,
    *,
    opportunity_authorizer: object | None = None,
    employee_authorizer: object | None = None,
):
    from apps.api.dependencies import ConfiguredApiDependencies

    opportunity_auth = opportunity_authorizer or _AllowAuthorizer()
    employee_auth = employee_authorizer or _AllowAuthorizer()
    scope = _EmployeeServiceScope(service)
    markers = {
        "opportunities": object(),
        "outreach": object(),
        "sending_identities": object(),
        "tool_gateway": _ToolGateway(),
        "delivery_materials": _DeliveryMaterials(),
        "unsubscribe_links": _UnsubscribeLinks(),
        "unsubscribe_service": _UnsubscribeService(),
        "workflow_engine": object(),
        "outbox_deliverer": object(),
        "notification_router": object(),
        "notification_dedup_store": object(),
        "outreach_authorizer": object(),
        "sending_identity_authorizer": object(),
        "campaign_scope_resolver": object(),
        "in_app_notifications": object(),
    }
    dependencies = ConfiguredApiDependencies(
        opportunities=markers["opportunities"],
        outreach=markers["outreach"],
        sending_identities=markers["sending_identities"],
        tool_gateway=markers["tool_gateway"],
        delivery_materials=markers["delivery_materials"],
        unsubscribe_links=markers["unsubscribe_links"],
        unsubscribe_service=markers["unsubscribe_service"],
        employees=scope,
        opportunity_authorizer=opportunity_auth,
        employee_authorizer=employee_auth,
        workflow_engine=markers["workflow_engine"],
        outbox_deliverer=markers["outbox_deliverer"],
        notification_router=markers["notification_router"],
        notification_dedup_store=markers["notification_dedup_store"],
        employee_lookup_actor=EmployeeActor(
            actor_id="system:api-identity",
            scope=EmployeeScope.SYSTEM,
            role="system",
        ),
        outreach_authorizer=markers["outreach_authorizer"],
        sending_identity_authorizer=markers["sending_identity_authorizer"],
        campaign_scope_resolver=markers["campaign_scope_resolver"],
        in_app_notifications=markers["in_app_notifications"],
    )
    return dependencies, scope, markers


def _app(
    employee: EmployeeView | None = None,
    *,
    active: list[EmployeeView] | None = None,
    tenant_id: str = "tenant-a",
    dev_mode: bool = True,
    retry_after_seconds: int = 17,
    opportunity_authorizer: object | None = None,
):
    from apps.api.main import ApiSettings, create_app

    service = _EmployeeService(
        employee or _employee(tenant_id=tenant_id), active=active
    )
    dependencies, scope, markers = _configured_dependencies(
        service, opportunity_authorizer=opportunity_authorizer
    )
    app = create_app(
        settings=ApiSettings(
            tenant_id=tenant_id,
            dev_mode=dev_mode,
            retry_after_seconds=retry_after_seconds,
        ),
        dependencies=dependencies,
    )
    return app, service, scope, dependencies, markers


def _install_identity_route(app: Any) -> None:
    @app.get("/_test/identity", include_in_schema=False)
    async def identity_view(
        identity: Annotated[RequestIdentity, _IDENTITY_DEPENDENCY],
    ) -> dict[str, object]:
        return {
            "employee_id": str(identity.employee.employee_id),
            "role": identity.employee.role,
            "employee_scope": identity.employee_actor.scope.value,
            "opportunity_scope": identity.opportunity_actor.scope.label,
            "allowed_owners": sorted(
                str(item)
                for item in (identity.opportunity_actor.scope.allowed_owners or [])
            ),
        }


@pytest.mark.parametrize(
    "modules",
    [
        ("apps.api.identity", "apps.api.dependencies"),
        ("apps.api.dependencies", "apps.api.identity"),
    ],
)
def test_identity_and_dependencies_import_in_either_cold_process_order(
    modules: tuple[str, str],
) -> None:
    code = "; ".join(f"import {module}" for module in modules)
    result = subprocess.run(
        [sys.executable, "-W", "error", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_api_settings_and_error_contracts_are_strict_frozen_pydantic_models() -> None:
    from apps.api.main import ApiSettings
    from apps.api.middleware import ApiErrorResponse

    settings = ApiSettings(
        tenant_id="tenant-a", dev_mode=True, retry_after_seconds=9
    )
    error = ApiErrorResponse(code="forbidden", message="没有权限")

    with pytest.raises(PydanticValidationError):
        settings.tenant_id = "tenant-b"  # type: ignore[misc]
    with pytest.raises(PydanticValidationError):
        error.message = "changed"  # type: ignore[misc]

    for invalid in ("", "   "):
        with pytest.raises(PydanticValidationError):
            ApiSettings(
                tenant_id=invalid, dev_mode=True, retry_after_seconds=9
            )
    with pytest.raises(PydanticValidationError):
        ApiSettings(tenant_id="tenant-a", dev_mode=1, retry_after_seconds=9)  # type: ignore[arg-type]
    with pytest.raises(PydanticValidationError):
        ApiSettings(tenant_id="tenant-a", dev_mode=True, retry_after_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(PydanticValidationError):
        ApiSettings(tenant_id="tenant-a", dev_mode=True, retry_after_seconds=0)
    with pytest.raises(PydanticValidationError):
        ApiErrorResponse(code="x", message="y", extra="forbidden")  # type: ignore[call-arg]


def test_import_and_zero_arg_factory_do_not_create_database_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import apps.api.main as main_module
    import infra.db.session as session_module

    def forbidden_engine(*args: object, **kwargs: object) -> None:
        raise AssertionError("API import/factory 不得创建数据库引擎")

    monkeypatch.setattr(session_module, "create_engine_from", forbidden_engine)
    monkeypatch.setattr(session_module, "session_factory", forbidden_engine)
    reloaded = importlib.reload(main_module)

    app = reloaded.create_app()
    assert set(app.openapi()["paths"]) == _EXPECTED_API_PATHS
    assert app.state.dependencies.configured is False


def test_production_entry_disables_access_log_for_dynamic_resource_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认 access log 会把被拒绝的动态 handoff ID 作为原始 URL 写入日志。"""
    import uvicorn

    from apps.api.main import main

    calls: list[tuple[str, dict[str, object]]] = []

    def run(app: str, **kwargs: object) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(uvicorn, "run", run)

    main()

    assert calls == [
        (
            "apps.api.runtime:create_runtime_app",
            {"factory": True, "access_log": False},
        )
    ]


def test_factory_openapi_matches_s3_15_crm_runtime_contracts() -> None:
    from fastapi import APIRouter

    from apps.api.routers.crm import router

    app, *_ = _app()
    schema = app.openapi()

    assert isinstance(router, APIRouter)
    assert {route.path for route in router.routes} == {
        "/analytics/loss-reasons",
        "/handoffs",
        "/handoffs/{handoff_id}",
        "/handoffs/{handoff_id}/accept",
        "/opportunities",
        "/opportunities/{opportunity_id}",
        "/opportunities/{opportunity_id}/transition",
        "/opportunities/{opportunity_id}/mark-lost",
    }
    assert set(schema["paths"]) == _EXPECTED_API_PATHS
    create_responses = schema["paths"]["/crm/opportunities"]["post"]["responses"]
    assert set(schema["paths"]["/crm/opportunities"]) == {"get", "post"}
    assert "201" in create_responses
    assert "204" in create_responses
    assert schema["paths"]["/crm/opportunities"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OpportunityIntakeBody"
    }
    assert "OpportunityCreateRequest" in schema["components"]["schemas"]
    assert "ValidatedNeedEvidence" in schema["components"]["schemas"]
    assert "HandoffQueueItemView" in schema["components"]["schemas"]
    assert "HandoffPacketView" in schema["components"]["schemas"]
    assert create_responses["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiErrorResponse"
    }
    queue_schema = schema["paths"]["/crm/handoffs"]["get"]["responses"]["200"]
    assert queue_schema["content"]["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/HandoffQueueItemView"
    }
    packet_schema = schema["paths"]["/crm/handoffs/{handoff_id}"]["get"][
        "responses"
    ]["200"]
    assert packet_schema["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HandoffPacketView"
    }
    accept_responses = schema["paths"]["/crm/handoffs/{handoff_id}/accept"][
        "post"
    ]["responses"]
    assert "204" in accept_responses
    assert "content" not in accept_responses["204"]
    assert "requestBody" not in schema["paths"][
        "/crm/handoffs/{handoff_id}/accept"
    ]["post"]
    assert accept_responses["409"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiErrorResponse"
    }
    analytics_schema = schema["paths"]["/crm/analytics/loss-reasons"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert analytics_schema["type"] == "object"
    assert analytics_schema["additionalProperties"] == {
        "additionalProperties": {"type": "integer"},
        "type": "object",
    }
    send_operation = schema["paths"]["/crm/message-attempts/{attempt_id}/send"][
        "post"
    ]
    assert send_operation["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ManualEmailSendBody"}
    assert send_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ManualEmailSendResponse"}
    assert {"400", "403", "409", "429", "503"} <= set(
        send_operation["responses"]
    )
    assert "ManualEmailSendBody" in schema["components"]["schemas"]
    assert "ManualEmailSendResponse" in schema["components"]["schemas"]
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if (path, method) == (
                "/settings/country-policies/proposals",
                "post",
            ):
                assert operation["responses"]["422"]["content"][
                    "application/json"
                ]["schema"] == {
                    "$ref": "#/components/schemas/ApiErrorResponse"
                }
            else:
                assert "422" not in operation["responses"]
            assert operation["responses"]["400"]["content"]["application/json"][
                "schema"
            ] == {"$ref": "#/components/schemas/ApiErrorResponse"}
    assert not any(
        getattr(route, "path", "").startswith(
            (
                "/campaigns",
                "/inbox",
                "/products",
                "/runs",
                "/settings",
                "/team",
            )
        )
        for route in app.routes
    )


def test_dependency_container_is_complete_frozen_and_preserves_injections() -> None:
    service = _EmployeeService(_employee())
    dependencies, scope, markers = _configured_dependencies(service)

    assert dependencies.configured is True
    assert dependencies.employees is scope
    assert dependencies.opportunities is markers["opportunities"]
    assert dependencies.outreach is markers["outreach"]
    assert dependencies.sending_identities is markers["sending_identities"]
    assert dependencies.tool_gateway is markers["tool_gateway"]
    assert dependencies.delivery_materials is markers["delivery_materials"]
    assert dependencies.unsubscribe_links is markers["unsubscribe_links"]
    assert dependencies.unsubscribe_service is markers["unsubscribe_service"]
    assert dependencies.workflow_engine is markers["workflow_engine"]
    assert dependencies.outbox_deliverer is markers["outbox_deliverer"]
    assert dependencies.notification_router is markers["notification_router"]
    assert (
        dependencies.notification_dedup_store
        is markers["notification_dedup_store"]
    )
    assert (
        dependencies.outreach_authorizer is markers["outreach_authorizer"]
    )
    assert (
        dependencies.sending_identity_authorizer
        is markers["sending_identity_authorizer"]
    )
    assert (
        dependencies.campaign_scope_resolver
        is markers["campaign_scope_resolver"]
    )
    assert (
        dependencies.in_app_notifications is markers["in_app_notifications"]
    )
    assert dependencies.employee_lookup_actor.scope is EmployeeScope.SYSTEM
    with pytest.raises(FrozenInstanceError):
        dependencies.opportunities = object()  # type: ignore[misc]
    with pytest.raises(TypeError, match="显式 bool"):
        replace(dependencies, contact_enrichment_composed=1)  # type: ignore[arg-type]


def test_dependency_container_rejects_non_system_lookup_actor() -> None:
    from apps.api.dependencies import ConfiguredApiDependencies

    service = _EmployeeService(_employee())
    scope = _EmployeeServiceScope(service)
    with pytest.raises(ValueError, match="SYSTEM"):
        ConfiguredApiDependencies(
            opportunities=object(),
            outreach=object(),
            sending_identities=object(),
            tool_gateway=_ToolGateway(),
            delivery_materials=_DeliveryMaterials(),
            unsubscribe_links=_UnsubscribeLinks(),
            unsubscribe_service=_UnsubscribeService(),
            employees=scope,
            opportunity_authorizer=_AllowAuthorizer(),
            employee_authorizer=_AllowAuthorizer(),
            workflow_engine=object(),
            outbox_deliverer=object(),
            notification_router=object(),
            notification_dedup_store=object(),
            employee_lookup_actor=EmployeeActor(
                actor_id="emp-boss", scope=EmployeeScope.TENANT, role="boss"
            ),
            outreach_authorizer=object(),
            sending_identity_authorizer=object(),
            campaign_scope_resolver=object(),
            in_app_notifications=object(),
        )


@pytest.mark.parametrize(
    ("field_name", "invalid"),
    [
        ("tool_gateway", object()),
        ("delivery_materials", object()),
        ("unsubscribe_links", object()),
        ("unsubscribe_service", object()),
    ],
)
def test_dependency_container_rejects_missing_runtime_provider(
    field_name: str, invalid: object
) -> None:
    service = _EmployeeService(_employee())
    dependencies, scope, markers = _configured_dependencies(service)
    values = {
        "opportunities": markers["opportunities"],
        "outreach": markers["outreach"],
        "sending_identities": markers["sending_identities"],
        "tool_gateway": markers["tool_gateway"],
        "delivery_materials": markers["delivery_materials"],
        "unsubscribe_links": markers["unsubscribe_links"],
        "unsubscribe_service": markers["unsubscribe_service"],
        "employees": scope,
        "opportunity_authorizer": dependencies.opportunity_authorizer,
        "employee_authorizer": dependencies.employee_authorizer,
        "workflow_engine": markers["workflow_engine"],
        "outbox_deliverer": markers["outbox_deliverer"],
        "notification_router": markers["notification_router"],
        "notification_dedup_store": markers["notification_dedup_store"],
        "employee_lookup_actor": dependencies.employee_lookup_actor,
        "outreach_authorizer": dependencies.outreach_authorizer,
        "sending_identity_authorizer": dependencies.sending_identity_authorizer,
        "campaign_scope_resolver": dependencies.campaign_scope_resolver,
        "in_app_notifications": dependencies.in_app_notifications,
    }
    values[field_name] = invalid
    with pytest.raises(TypeError, match="API 手工发送依赖未完整配置"):
        ConfiguredApiDependencies(**values)  # type: ignore[arg-type]


def test_tenant_assertion_is_exact_and_never_echoes_header() -> None:
    app, *_ = _app()
    client = _ApiClient(app)

    missing = client.get("/not-found")
    assert missing.status_code == 401
    assert missing.json() == {
        "code": "tenant_header_required",
        "message": "缺少租户身份",
    }

    for supplied in ("", "tenant-b", " tenant-a", "TENANT-A"):
        response = client.get("/not-found", headers={"X-Tenant-Id": supplied})
        assert response.status_code == 403
        assert response.json() == {
            "code": "tenant_forbidden",
            "message": "租户身份不匹配",
        }
        assert supplied not in response.text or supplied == ""

    matched = client.get("/not-found", headers={"X-Tenant-Id": "tenant-a"})
    assert matched.status_code == 404


def test_tenant_assertion_rejects_duplicate_header_values() -> None:
    app, *_ = _app()
    client = _ApiClient(app)
    response = client.get(
        "/not-found",
        headers=[
            ("X-Tenant-Id", "tenant-a"),
            ("X-Tenant-Id", "tenant-b-secret"),
        ],
    )
    assert response.status_code == 403
    assert "tenant-b-secret" not in response.text


def test_real_not_found_and_method_not_allowed_use_flat_safe_contract() -> None:
    app, *_ = _app()

    @app.get("/_test/get-only", include_in_schema=False)
    async def get_only() -> dict[str, bool]:
        return {"ok": True}

    client = _ApiClient(app)
    headers = {"X-Tenant-Id": "tenant-a"}
    not_found = client.get("/_test/missing", headers=headers)
    method_not_allowed = client.request(
        "POST", "/_test/get-only", headers=headers
    )

    assert not_found.status_code == 404
    assert not_found.json() == {"code": "not_found", "message": "资源不存在"}
    assert not_found.headers.get("Retry-After") is None
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json() == {
        "code": "http_error",
        "message": "请求未完成",
    }
    assert method_not_allowed.headers.get("Retry-After") is None
    assert "Method Not Allowed" not in method_not_allowed.text


def test_sales_identity_comes_from_employee_view_and_ignores_spoof_headers() -> None:
    app, service, scope, dependencies, _ = _app()
    _install_identity_route(app)
    response = _ApiClient(app).get(
        "/_test/identity",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-Employee-Id": "emp-sales",
            "X-Role": "boss",
            "X-Scope": "tenant",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "employee_id": "emp-sales",
        "role": "sales",
        "employee_scope": "self",
        "opportunity_scope": "self",
        "allowed_owners": ["emp-sales"],
    }
    assert scope.entered == scope.exited == 1
    assert scope.tenants == [TenantId("tenant-a")]
    assert [call[0] for call in service.calls] == ["get"]
    assert service.calls[0][3] is dependencies.employee_lookup_actor


def test_manager_scope_uses_only_self_and_active_direct_reports() -> None:
    manager = _employee(employee_id="emp-manager", role="manager")
    active = [
        manager,
        _employee(employee_id="emp-direct", manager_id="emp-manager"),
        _employee(
            employee_id="emp-inactive",
            manager_id="emp-manager",
            active=False,
        ),
        _employee(employee_id="emp-other", manager_id="another-manager"),
        _employee(employee_id="emp-other-tenant", tenant_id="tenant-b"),
    ]
    app, service, scope, dependencies, _ = _app(manager, active=active)
    _install_identity_route(app)

    response = _ApiClient(app).get(
        "/_test/identity",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-Employee-Id": "emp-manager",
        },
    )

    assert response.status_code == 200
    assert response.json()["employee_scope"] == "manager"
    assert response.json()["opportunity_scope"] == "manager"
    assert response.json()["allowed_owners"] == ["emp-direct", "emp-manager"]
    assert [call[0] for call in service.calls] == ["get", "list"]
    assert all(call[3] is dependencies.employee_lookup_actor for call in service.calls)
    assert scope.entered == scope.exited == 1


def test_boss_has_explicit_tenant_scope() -> None:
    app, *_ = _app(_employee(employee_id="emp-boss", role="boss"))
    _install_identity_route(app)
    response = _ApiClient(app).get(
        "/_test/identity",
        headers={"X-Tenant-Id": "tenant-a", "X-Employee-Id": "emp-boss"},
    )
    assert response.status_code == 200
    assert response.json()["employee_scope"] == "tenant"
    assert response.json()["opportunity_scope"] == "tenant"
    assert response.json()["allowed_owners"] == []


@pytest.mark.parametrize(
    ("employee", "employee_header"),
    [
        (_employee(active=False), "emp-sales"),
        (_employee(tenant_id="tenant-b"), "emp-sales"),
        (_employee(role="invented-admin"), "emp-sales"),
        (_employee(employee_id="different-id"), "emp-sales"),
    ],
)
def test_invalid_employee_records_are_fixed_403_without_identifier_echo(
    employee: EmployeeView, employee_header: str
) -> None:
    app, *_ = _app(employee)
    _install_identity_route(app)
    response = _ApiClient(app).get(
        "/_test/identity",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-Employee-Id": employee_header,
        },
    )
    assert response.status_code == 403
    assert response.json() == {"code": "forbidden", "message": "没有权限"}
    assert employee_header not in response.text


def test_known_non_crm_role_is_unprivileged_and_first_gate_denies_it() -> None:
    app, *_ = _app(_employee(employee_id="emp-viewer", role="viewer"))

    @app.get("/_test/gated", include_in_schema=False)
    async def gated(
        identity: Annotated[RequestIdentity, _CRM_READ_GATE],
    ) -> dict[str, bool]:
        return {"ok": identity.employee.is_active}

    response = _ApiClient(app).get(
        "/_test/gated",
        headers={"X-Tenant-Id": "tenant-a", "X-Employee-Id": "emp-viewer"},
    )
    assert response.status_code == 403
    assert response.json() == {"code": "forbidden", "message": "没有权限"}


def test_first_gate_calls_injected_authorizer_with_derived_actor_scope() -> None:
    authorizer = _AllowAuthorizer()
    app, *_ = _app(opportunity_authorizer=authorizer)

    @app.get("/_test/gated", include_in_schema=False)
    async def gated(
        identity: Annotated[RequestIdentity, _SALES_READ_GATE],
    ) -> dict[str, bool]:
        return {"ok": identity.employee.is_active}

    response = _ApiClient(app).get(
        "/_test/gated",
        headers={"X-Tenant-Id": "tenant-a", "X-Employee-Id": "emp-sales"},
    )
    assert response.status_code == 200
    assert len(authorizer.calls) == 1
    actor, action, scope, tenant = authorizer.calls[0]
    assert actor.scope.level is ScopeLevel.SELF  # type: ignore[union-attr]
    assert action is OpportunityAction.OPPORTUNITY_READ
    assert scope is actor.scope  # type: ignore[union-attr]
    assert tenant == TenantId("tenant-a")


def test_dev_identity_requires_employee_header_and_non_dev_fails_closed() -> None:
    dev_app, *_ = _app()
    _install_identity_route(dev_app)
    missing = _ApiClient(dev_app).get(
        "/_test/identity", headers={"X-Tenant-Id": "tenant-a"}
    )
    assert missing.status_code == 401
    assert missing.json() == {
        "code": "authentication_required",
        "message": "需要员工身份",
    }

    prod_app, service, scope, *_ = _app(dev_mode=False)
    _install_identity_route(prod_app)
    rejected = _ApiClient(prod_app).get(
        "/_test/identity",
        headers={
            "X-Tenant-Id": "tenant-a",
            "X-Employee-Id": "emp-sales",
            "X-Role": "boss",
        },
    )
    assert rejected.status_code == 403
    assert rejected.json() == {"code": "forbidden", "message": "没有权限"}
    assert service.calls == []
    assert scope.entered == scope.exited == 0


def test_domain_errors_have_fixed_flat_body_and_retry_header_only_when_retryable() -> None:
    app, *_ = _app(retry_after_seconds=23)

    @app.get("/_test/validation", include_in_schema=False)
    async def validation() -> None:
        raise ValidationError("客户秘密", context={"token": "credential-secret"})

    @app.get("/_test/permission", include_in_schema=False)
    async def permission() -> None:
        raise PermissionDenied("员工和客户秘密")

    @app.get("/_test/state", include_in_schema=False)
    async def state() -> None:
        raise InvalidStateTransition("状态秘密")

    @app.get("/_test/transient", include_in_schema=False)
    async def transient() -> None:
        raise TransientError("数据库密码 secret")

    @app.get("/_test/policy", include_in_schema=False)
    async def policy() -> None:
        raise PolicyViolation("业务内容 secret")

    client = _ApiClient(app)
    headers = {"X-Tenant-Id": "tenant-a"}
    cases = [
        ("validation", 400, "validation_error", "请求参数无效", None),
        ("permission", 403, "forbidden", "没有权限", None),
        ("state", 409, "invalid_state", "当前状态不允许此操作", None),
        ("transient", 503, "service_unavailable", "服务暂时不可用", "23"),
        ("policy", 400, "request_rejected", "请求被安全策略拒绝", None),
    ]
    for path, status, code, message, retry_after in cases:
        response = client.get(f"/_test/{path}", headers=headers)
        assert response.status_code == status
        assert response.json() == {"code": code, "message": message}
        assert response.headers.get("Retry-After") == retry_after
        assert "secret" not in response.text


def test_http_and_unexpected_errors_never_reflect_detail_or_exception() -> None:
    app, *_ = _app()

    @app.get("/_test/http", include_in_schema=False)
    async def http_error() -> None:
        raise HTTPException(status_code=418, detail="credential-secret")

    @app.get("/_test/unexpected", include_in_schema=False)
    async def unexpected() -> None:
        raise RuntimeError("dsn-secret")

    client = _ApiClient(app, raise_server_exceptions=True)
    headers = {"X-Tenant-Id": "tenant-a"}
    http_response = client.get("/_test/http", headers=headers)
    unexpected_response = client.get("/_test/unexpected", headers=headers)

    assert http_response.status_code == 418
    assert http_response.json() == {
        "code": "http_error",
        "message": "请求未完成",
    }
    assert "credential-secret" not in http_response.text
    assert unexpected_response.status_code == 500
    assert unexpected_response.json() == {
        "code": "internal_error",
        "message": "服务处理请求失败",
    }
    assert "dsn-secret" not in unexpected_response.text


@pytest.mark.parametrize("response_completed", [False, True])
def test_safe_unhandled_middleware_swallows_error_after_response_started(
    response_completed: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.api.middleware import SafeUnhandledExceptionMiddleware

    async def broken_after_start(scope: object, receive: object, send: Any) -> None:
        del scope, receive
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"partial",
                "more_body": not response_completed,
            }
        )
        raise RuntimeError("response-started-secret")

    app = SafeUnhandledExceptionMiddleware(broken_after_start)
    with caplog.at_level("ERROR", logger="apps.api.middleware"):
        response = _ApiClient(app, raise_server_exceptions=True).get("/")

    assert response.status_code == 200
    assert response.text == "partial"
    assert "response-started-secret" not in caplog.text


def test_safe_unhandled_middleware_propagates_cancel_and_non_http_faults() -> None:
    from apps.api.middleware import SafeUnhandledExceptionMiddleware

    async def cancelled(scope: object, receive: object, send: object) -> None:
        del scope, receive, send
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        _ApiClient(
            SafeUnhandledExceptionMiddleware(cancelled),
            raise_server_exceptions=True,
        ).get("/")

    async def startup_fault(scope: object, receive: object, send: object) -> None:
        del scope, receive, send
        raise RuntimeError("startup-secret")

    middleware = SafeUnhandledExceptionMiddleware(startup_fault)

    async def receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def send(message: object) -> None:
        del message

    with pytest.raises(RuntimeError, match="startup-secret"):
        asyncio.run(
            middleware(
                {"type": "lifespan", "asgi": {"version": "3.0"}},
                receive,
                send,
            )
        )


@pytest.mark.parametrize("failure_stage", ["first_send", "fallback_send"])
def test_safe_unhandled_middleware_swallows_ordinary_send_failure_without_context_leak(
    failure_stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.api.middleware import SafeUnhandledExceptionMiddleware

    async def application(scope: object, receive: object, send: Any) -> None:
        del scope, receive
        if failure_stage == "first_send":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                }
            )
            return
        raise RuntimeError("dsn-secret")

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def failing_send(message: object) -> None:
        del message
        raise RuntimeError("send-secret")

    middleware = SafeUnhandledExceptionMiddleware(application)
    with caplog.at_level("ERROR", logger="apps.api.middleware"):
        asyncio.run(
            middleware(
                {"type": "http", "method": "GET", "headers": []},
                receive,
                failing_send,
            )
        )

    assert "dsn-secret" not in caplog.text
    assert "send-secret" not in caplog.text
    assert caplog.records
    assert all(record.error_type == "RuntimeError" for record in caplog.records)


def test_safe_unhandled_middleware_unwinds_stream_on_first_wire_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apps.api.middleware import SafeUnhandledExceptionMiddleware

    produced = 0
    wire_calls = 0

    async def streaming_application(
        scope: object, receive: object, send: Any
    ) -> None:
        nonlocal produced
        del scope, receive
        for index in range(5):
            produced += 1
            await send(
                {
                    "type": "http.response.body",
                    "body": str(index).encode(),
                    "more_body": index < 4,
                }
            )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def failing_wire_send(message: object) -> None:
        nonlocal wire_calls
        del message
        wire_calls += 1
        raise OSError("wire-secret")

    middleware = SafeUnhandledExceptionMiddleware(streaming_application)
    with caplog.at_level("ERROR", logger="apps.api.middleware"):
        asyncio.run(
            middleware(
                {"type": "http", "method": "GET", "headers": []},
                receive,
                failing_wire_send,
            )
        )

    assert produced == 1
    assert wire_calls == 1
    assert len(caplog.records) == 1
    assert caplog.records[0].error_type == "OSError"
    assert "wire-secret" not in caplog.text


def test_zero_arg_dependencies_fail_closed_but_openapi_remains_pure() -> None:
    from apps.api.main import create_app

    app = create_app()

    @app.get("/_test/configured", include_in_schema=False)
    async def configured(
        dependencies: Annotated[
            ConfiguredApiDependencies, _CONFIGURED_DEPENDENCY
        ],
    ) -> dict[str, bool]:
        return {"ok": dependencies is not None}

    assert app.openapi() is not None
    response = _ApiClient(app).get(
        "/_test/configured",
        headers={"X-Tenant-Id": app.state.settings.tenant_id},
    )
    assert response.status_code == 503
    assert response.json() == {
        "code": "service_unavailable",
        "message": "服务暂时不可用",
    }
    assert response.headers["Retry-After"] == str(
        app.state.settings.retry_after_seconds
    )


def test_multiple_apps_do_not_share_settings_dependencies_or_handlers() -> None:
    app_a, service_a, *_ = _app(tenant_id="tenant-a", retry_after_seconds=11)
    app_b, service_b, *_ = _app(
        _employee(employee_id="emp-b", tenant_id="tenant-b"),
        tenant_id="tenant-b",
        retry_after_seconds=29,
    )
    _install_identity_route(app_a)
    _install_identity_route(app_b)

    a = _ApiClient(app_a).get(
        "/_test/identity",
        headers={"X-Tenant-Id": "tenant-a", "X-Employee-Id": "emp-sales"},
    )
    b = _ApiClient(app_b).get(
        "/_test/identity",
        headers={"X-Tenant-Id": "tenant-b", "X-Employee-Id": "emp-b"},
    )
    wrong = _ApiClient(app_a).get(
        "/_test/identity",
        headers={"X-Tenant-Id": "tenant-b", "X-Employee-Id": "emp-b"},
    )

    assert a.status_code == b.status_code == 200
    assert a.json()["employee_id"] == "emp-sales"
    assert b.json()["employee_id"] == "emp-b"
    assert wrong.status_code == 403
    assert [str(call[1]) for call in service_a.calls] == ["tenant-a"]
    assert [str(call[1]) for call in service_b.calls] == ["tenant-b"]
