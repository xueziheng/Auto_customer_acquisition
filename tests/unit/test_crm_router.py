"""S3-14 CRM router 的 HTTP 边界测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import ConfiguredApiDependencies
from apps.api.main import ApiSettings, create_app
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView, OwnershipLockView
from domains.opportunities.permissions import OpportunityAction
from domains.opportunities.schemas import OpportunityView, ProvenanceSummary
from domains.opportunities.service import OpportunityState
from shared.errors import InvalidStateTransition, PermissionDenied
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
)

_NOW = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
_TENANT = TenantId("tenant-a")
_HEADERS = {"X-Tenant-Id": "tenant-a", "X-Employee-Id": "emp-sales"}


class _Client:
    """经真实 ASGI 栈发请求，避免 TestClient 的废弃告警。"""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: object | None = None,
    ) -> Response:
        async def send() -> Response:
            transport = ASGITransport(app=self._app, raise_app_exceptions=False)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, headers=headers, json=json)

        return asyncio.run(send())


class _Authorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls: list[tuple[object, object, object, TenantId]] = []

    def require(
        self, actor: object, action: object, scope: object, tenant_id: TenantId
    ) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        if self.deny:
            raise PermissionDenied("拒绝原因不得出现在 HTTP 响应中")
        return "test:allow"


class _ManualRuntime:
    async def invoke(self, _ctx: object) -> object:
        raise AssertionError("CRM 机会测试不应调用手工发送")

    async def resolve(self, *_args: object) -> object:
        raise AssertionError("CRM 机会测试不应解析发送材料")

    async def build(self, *_args: object) -> str:
        raise AssertionError("CRM 机会测试不应生成退订链接")


class _Employees:
    def __init__(self, trace: list[str], *, role: str = "sales") -> None:
        self.trace = trace
        self.employee = EmployeeView(
            employee_id=EmployeeId("emp-sales"),
            tenant_id=_TENANT,
            name="销售员",
            role=role,
        )
        self.resolve_calls: list[tuple[object, ...]] = []
        self.raise_on: Exception | None = None

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: EmployeeActor
    ) -> EmployeeView:
        self.trace.append("identity_get_employee")
        assert tenant_id == _TENANT
        assert employee_id == self.employee.employee_id
        return self.employee

    async def list_active(
        self, tenant_id: TenantId, *, actor: EmployeeActor
    ) -> list[EmployeeView]:
        self.trace.append("identity_list_active")
        assert tenant_id == _TENANT
        return [self.employee]

    async def resolve_owner(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        actor: EmployeeActor,
        country: str,
        need_category: str | None = None,
        **kwargs: object,
    ) -> OwnershipLockView:
        self.trace.append("resolve_owner")
        self.resolve_calls.append(
            (tenant_id, account_id, actor, country, need_category, kwargs)
        )
        if self.raise_on is not None:
            raise self.raise_on
        return OwnershipLockView(
            tenant_id=tenant_id,
            account_id=account_id,
            owner=EmployeeId("emp-owner"),
            locked_at=_NOW,
            locked_by_rule="country",
        )


class _EmployeeScope:
    def __init__(self, service: _Employees, trace: list[str]) -> None:
        self.service = service
        self.trace = trace
        self.entries = 0

    @asynccontextmanager
    async def __call__(self, tenant_id: TenantId):
        assert tenant_id == _TENANT
        prefix = "identity_scope" if self.entries == 0 else "ownership_scope"
        self.entries += 1
        self.trace.append(f"{prefix}_enter")
        try:
            yield self.service
        finally:
            self.trace.append(f"{prefix}_exit")


class _Opportunities:
    def __init__(self, trace: list[str], *, create_result: str | None = "opp-1") -> None:
        self.trace = trace
        self.create_result = create_result
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.raise_on: dict[str, Exception] = {}
        self.get_result: object | None = None

    def _record(self, name: str, *args: object, **kwargs: object) -> None:
        self.trace.append(name)
        self.calls.append((name, args, kwargs))
        error = self.raise_on.get(name)
        if error is not None:
            raise error

    async def create_from_need(self, tenant_id, request, evidence, *, actor):
        self._record("create", tenant_id, request, evidence, actor=actor)
        return self.create_result

    async def assign(self, tenant_id, opportunity_id, owner, assigned_by, *, actor):
        self._record(
            "assign", tenant_id, opportunity_id, owner, assigned_by, actor=actor
        )

    async def get(
        self, tenant_id: TenantId, opportunity_id: OpportunityId, *, actor: object
    ) -> OpportunityView:
        self._record("get", tenant_id, opportunity_id, actor=actor)
        if self.get_result is not None:
            return self.get_result  # type: ignore[return-value]
        return _opportunity_view(str(opportunity_id))

    async def list_opportunities(self, tenant_id, actor, *, scope, states=None, limit=50):
        self._record("list", tenant_id, actor, scope=scope, states=states, limit=limit)
        return [_opportunity_view("opp-1")]

    async def transition(self, tenant_id, opportunity_id, target, *, actor):
        self._record("transition", tenant_id, opportunity_id, target, actor=actor)

    async def mark_lost(
        self,
        tenant_id,
        opportunity_id,
        reason,
        *,
        actor,
        confirmed_by,
        confirmed_at,
        detail=None,
    ):
        self._record(
            "mark_lost",
            tenant_id,
            opportunity_id,
            reason,
            actor=actor,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            detail=detail,
        )


def _opportunity_view(opportunity_id: str) -> OpportunityView:
    return OpportunityView(
        opportunity_id=opportunity_id,
        account_id="account-1",
        account_name="Acme",
        country="US",
        need_id="need-1",
        product_category="hinges",
        state="qualified",
        created_at=_NOW,
        provenance=[
            ProvenanceSummary(
                field_name="quantity",
                source_type="conversation",
                source_id="message-1",
                extracted_by="human",
                extracted_at=_NOW,
                confirmed_by=None,
                confirmed_at=None,
                source_url=None,
                page_hash=None,
            )
        ],
    )


def _app(
    *,
    role: str = "sales",
    create_result: str | None = "opp-1",
    deny_first_gate: bool = False,
) -> tuple[FastAPI, _Opportunities, _Employees, _Authorizer]:
    trace: list[str] = []
    employees = _Employees(trace, role=role)
    opportunities = _Opportunities(trace, create_result=create_result)
    opportunity_authorizer = _Authorizer(deny=deny_first_gate)
    manual_runtime = _ManualRuntime()
    dependencies = ConfiguredApiDependencies(
        opportunities=opportunities,
        outreach=manual_runtime,
        sending_identities=manual_runtime,
        tool_gateway=manual_runtime,
        delivery_materials=manual_runtime,
        unsubscribe_links=manual_runtime,
        employees=_EmployeeScope(employees, trace),
        opportunity_authorizer=opportunity_authorizer,
        employee_authorizer=_Authorizer(),
        workflow_engine=object(),
        outbox_deliverer=object(),
        notification_router=object(),
        notification_dedup_store=object(),
        employee_lookup_actor=EmployeeActor(
            actor_id="system:api-identity", scope=EmployeeScope.SYSTEM, role="system"
        ),
    )
    app = create_app(
        settings=ApiSettings(tenant_id="tenant-a", dev_mode=True, retry_after_seconds=5),
        dependencies=dependencies,
    )
    return app, opportunities, employees, opportunity_authorizer


def _create_body() -> dict[str, object]:
    return {
        "request": {
            "need_id": "need-1",
            "account_id": "account-1",
            "account_name": "Acme",
            "country": "US",
            "product_category": "hinges",
            "evidence_tier": "customer_interest_reply",
            "has_verified_contact": True,
            "category_allowed": True,
            "minimum_order_value": {"amount": "1000", "currency": "USD"},
            "field_provenance": {
                "account_name": {
                    "source_type": "conversation",
                    "source_id": "message-1",
                    "extracted_by": "human",
                    "extracted_at": "2026-08-09T10:00:00Z",
                },
                "country": {
                    "source_type": "conversation",
                    "source_id": "message-1",
                    "extracted_by": "human",
                    "extracted_at": "2026-08-09T10:00:00Z",
                },
            },
        },
        "evidence": {
            "level": "customer_interest_reply",
            "provenance": {
                "source_type": "conversation",
                "source_id": "message-1",
                "extracted_by": "human",
                "extracted_at": "2026-08-09T10:00:00Z",
            },
        },
    }


def _call_names(opportunities: _Opportunities) -> list[str]:
    return [name for name, _, _ in opportunities.calls]


def test_create_runs_intake_in_order_and_returns_provenance_view() -> None:
    """若漏掉 resolve/assign/get 或乱序，创建结果会丢失归属或来源摘要。"""
    app, opportunities, employees, authorizer = _app(role="boss")
    response = _Client(app).request(
        "POST", "/crm/opportunities", headers=_HEADERS, json=_create_body()
    )

    assert response.status_code == 201
    assert response.json()["opportunity_id"] == "opp-1"
    assert response.json()["provenance"] == [
        {
            "field_name": "quantity",
            "source_type": "conversation",
            "source_id": "message-1",
            "extracted_by": "human",
            "extracted_at": "2026-08-09T10:00:00Z",
            "confirmed_by": None,
            "confirmed_at": None,
            "source_url": None,
            "page_hash": None,
        }
    ]
    assert opportunities.trace == [
        "identity_scope_enter",
        "identity_get_employee",
        "identity_scope_exit",
        "create",
        "ownership_scope_enter",
        "resolve_owner",
        "ownership_scope_exit",
        "assign",
        "get",
    ]
    assert len(employees.resolve_calls) == 1
    tenant_id, account_id, employee_actor, country, category, extras = employees.resolve_calls[0]
    assert (tenant_id, account_id, country, category, extras) == (
        _TENANT,
        ProspectAccountId("account-1"),
        "US",
        "hinges",
        {},
    )
    assert employee_actor.actor_id == "emp-sales"
    assign = opportunities.calls[1]
    assert assign[1][1:4] == ("opp-1", EmployeeId("emp-owner"), EmployeeId("emp-sales"))
    assert authorizer.calls == [
        (
            opportunities.calls[0][2]["actor"],
            OpportunityAction.OPPORTUNITY_CREATE,
            opportunities.calls[0][2]["actor"].scope,
            _TENANT,
        )
    ]


def test_create_gate_none_stops_before_owner_resolution_and_assignment() -> None:
    """若硬门槛未过后仍分配负责人，会制造不存在机会的归属锁。"""
    app, opportunities, employees, _ = _app(role="boss", create_result=None)
    response = _Client(app).request(
        "POST", "/crm/opportunities", headers=_HEADERS, json=_create_body()
    )

    assert response.status_code == 204
    assert response.content == b""
    assert _call_names(opportunities) == ["create"]
    assert employees.resolve_calls == []
    assert opportunities.trace == [
        "identity_scope_enter",
        "identity_get_employee",
        "identity_scope_exit",
        "create",
    ]


@pytest.mark.parametrize(
    ("failure", "expected_trace"),
    [
        ("create", ["create"]),
        ("resolve", ["create", "ownership_scope_enter", "resolve_owner", "ownership_scope_exit"]),
        ("assign", ["create", "ownership_scope_enter", "resolve_owner", "ownership_scope_exit", "assign"]),
    ],
)
def test_create_failure_short_circuits_every_later_cross_domain_step(
    failure: str, expected_trace: list[str]
) -> None:
    """若任一步失败后仍继续，可能对未创建或未归属机会写入后续状态。"""
    app, opportunities, employees, _ = _app(role="boss")
    if failure == "resolve":
        employees.raise_on = PermissionDenied("resolve denied")
    else:
        opportunities.raise_on[failure] = PermissionDenied(f"{failure} denied")

    response = _Client(app).request(
        "POST", "/crm/opportunities", headers=_HEADERS, json=_create_body()
    )

    assert response.status_code == 403
    assert opportunities.trace[3:] == expected_trace


@pytest.mark.parametrize("role", ["sales", "manager"])
def test_create_rejects_non_boss_before_any_opportunity_service_call(
    role: str,
) -> None:
    app, opportunities, _, _ = _app(role=role)

    response = _Client(app).request(
        "POST", "/crm/opportunities", headers=_HEADERS, json=_create_body()
    )

    assert response.status_code == 403
    assert opportunities.calls == []


def test_invalid_service_view_is_response_validated_and_safely_hidden() -> None:
    """若服务返回坏视图仍被透传，客户端会收到未验证的内部对象。"""
    app, opportunities, _, _ = _app()
    opportunities.get_result = object()
    response = _Client(app).request("GET", "/crm/opportunities/opp-1", headers=_HEADERS)

    assert response.status_code == 500
    assert response.json() == {"code": "internal_error", "message": "服务处理请求失败"}


@pytest.mark.parametrize(
    ("method", "path", "body", "action", "service_call"),
    [
        ("GET", "/crm/opportunities?states=qualified&limit=3", None, OpportunityAction.OPPORTUNITY_LIST, "list"),
        ("GET", "/crm/opportunities/opp-1", None, OpportunityAction.OPPORTUNITY_READ, "get"),
        ("POST", "/crm/opportunities/opp-1/transition", {"target": "assigned"}, OpportunityAction.OPPORTUNITY_TRANSITION, "transition"),
        ("POST", "/crm/opportunities/opp-1/mark-lost", {"reason": "price_too_high", "detail": "competitor"}, OpportunityAction.OPPORTUNITY_MARK_LOST, "mark_lost"),
    ],
)
def test_each_existing_opportunity_route_uses_its_typed_first_gate(
    method: str,
    path: str,
    body: object | None,
    action: OpportunityAction,
    service_call: str,
) -> None:
    """若 action/scope/tenant 未传给 gate，HTTP 层会绕过 ABAC 审计。"""
    app, opportunities, _, authorizer = _app()
    response = _Client(app).request(method, path, headers=_HEADERS, json=body)

    assert response.status_code == 200
    assert _call_names(opportunities) == [service_call]
    if service_call in {"get", "transition", "mark_lost"}:
        assert opportunities.calls[0][1][1] == OpportunityId("opp-1")
    actor, actual_action, scope, tenant_id = authorizer.calls[0]
    assert actual_action is action
    assert scope is actor.scope
    assert tenant_id == _TENANT
    if service_call == "list":
        assert opportunities.calls[0][2] == {
            "scope": actor.scope,
            "states": [OpportunityState.QUALIFIED],
            "limit": 3,
        }
    if service_call == "mark_lost":
        call = opportunities.calls[0]
        assert call[2]["confirmed_by"] == EmployeeId("emp-sales")
        assert call[2]["confirmed_at"].tzinfo is not None
        assert call[2]["detail"] == "competitor"


@pytest.mark.parametrize(
    ("method", "path", "body", "expected_authorizer_calls"),
    [
        ("POST", "/crm/opportunities", {"request": {}, "evidence": {}}, 0),
        ("GET", "/crm/opportunities?limit=0", None, 1),
        ("POST", "/crm/opportunities/opp-1/transition", {"target": "force"}, 1),
        ("POST", "/crm/opportunities/opp-1/mark-lost", {"detail": "missing reason"}, 1),
        ("POST", "/crm/opportunities/opp-1/mark-lost", {"reason": "unknown"}, 1),
    ],
)
def test_malformed_bodies_and_query_are_rejected_before_any_business_call(
    method: str, path: str, body: object | None, expected_authorizer_calls: int
) -> None:
    """若无效输入到达服务层，可能写入无原因 lost 或绕开类型门禁。"""
    app, opportunities, employees, authorizer = _app()
    response = _Client(app).request(method, path, headers=_HEADERS, json=body)

    assert response.status_code == 400
    assert opportunities.calls == []
    assert employees.resolve_calls == []
    assert len(authorizer.calls) == expected_authorizer_calls


def test_non_crm_role_and_first_gate_denial_fail_closed_without_service_calls() -> None:
    """若 header 可把非 CRM 角色带入路由，未授权用户即可读取机会。"""
    app, opportunities, _, authorizer = _app(role="sourcing")
    role_response = _Client(app).request("GET", "/crm/opportunities", headers=_HEADERS)
    assert role_response.status_code == 403
    assert opportunities.calls == []
    assert authorizer.calls == []

    app, opportunities, _, authorizer = _app(deny_first_gate=True)
    denial_response = _Client(app).request("GET", "/crm/opportunities", headers=_HEADERS)
    assert denial_response.status_code == 403
    assert denial_response.json() == {"code": "forbidden", "message": "没有权限"}
    assert opportunities.calls == []
    assert len(authorizer.calls) == 1


def test_domain_service_denial_and_invalid_transition_use_safe_middleware_mappings() -> None:
    """若 router 吞掉域错误或回显异常文本，服务层授权审计就失去意义。"""
    app, opportunities, _, _ = _app()
    opportunities.raise_on["get"] = PermissionDenied("private opp id must not leak")
    denial = _Client(app).request("GET", "/crm/opportunities/opp-1", headers=_HEADERS)
    assert denial.status_code == 403
    assert denial.json() == {"code": "forbidden", "message": "没有权限"}
    assert "opp-1" not in denial.text

    app, opportunities, _, _ = _app()
    opportunities.raise_on["transition"] = InvalidStateTransition("qualified -> quoted")
    transition = _Client(app).request(
        "POST",
        "/crm/opportunities/opp-1/transition",
        headers=_HEADERS,
        json={"target": "quoted"},
    )
    assert transition.status_code == 409
    assert transition.json() == {
        "code": "invalid_state",
        "message": "当前状态不允许此操作",
    }


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Tenant-Id": "tenant-b", "X-Employee-Id": "emp-sales"},
        {"X-Tenant-Id": "tenant-a"},
    ],
)
def test_missing_or_wrong_tenant_and_identity_headers_fail_closed(
    headers: dict[str, str]
) -> None:
    """若租户或身份缺失时仍调用服务，会形成跨租户读取路径。"""
    app, opportunities, _, _ = _app()
    response = _Client(app).request("GET", "/crm/opportunities", headers=headers)

    assert response.status_code in {401, 403}
    assert opportunities.calls == []
