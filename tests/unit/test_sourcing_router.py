"""Sourcing Center HTTP 的角色、身份与命令边界。"""

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
from shared.errors import InvalidStateTransition
from shared.schemas.identifiers import EmployeeId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
CASE_ID = "src_01K39P9M5D6K4A91YEQ80EJZ0X"
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")


class _Sourcing:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_case_read_views(self, tenant_id, *, actor, limit=50):
        self.calls.append(("list", tenant_id, actor, limit))
        return []

    async def get_candidate_read_views(self, tenant_id, case_id, *, actor, limit=50):
        self.calls.append(("candidates", tenant_id, case_id, actor, limit))
        return []


class _SourcingApplication:
    async def confirm_plan(self, *args, **kwargs):
        raise AssertionError("authorization gates must run before confirm_plan")


class _ConflictingSourcingApplication:
    async def confirm_plan(self, *args, **kwargs):
        raise InvalidStateTransition("当前状态不允许此操作")


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


def _app(role: str) -> tuple[object, _Sourcing]:
    sourcing = _Sourcing()
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=sourcing,
        sourcing_application=_SourcingApplication(),
    )
    return app, sourcing


def _request(
    app: object,
    method: str,
    path: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    json: object | None = None,
) -> Response:
    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                path,
                headers=[
                    ("X-Employee-Id", str(EMPLOYEE)),
                    ("X-Tenant-Id", str(TENANT)),
                    *(headers or []),
                ],
                json=json,
            )

    return asyncio.run(run())


def test_sourcing_reads_are_limited_to_explicit_internal_roles() -> None:
    allowed_app, sourcing = _app("sourcing")
    denied_app, denied_sourcing = _app("manager")

    allowed = _request(allowed_app, "GET", "/sourcing-cases")
    denied = _request(denied_app, "GET", "/sourcing-cases")

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert len(sourcing.calls) == 1
    assert denied_sourcing.calls == []


def test_confirm_requires_boss_and_one_raw_idempotency_key() -> None:
    body = {"plan_id": "spl-source", "expected_plan_hash": "a" * 64}
    sourcing_app, _ = _app("sourcing")
    boss_app, _ = _app("boss")

    denied = _request(
        sourcing_app,
        "POST",
        f"/sourcing-cases/{CASE_ID}/public-search-plan/confirm",
        headers=[("Idempotency-Key", "confirm-a")],
        json=body,
    )
    missing = _request(
        boss_app,
        "POST",
        f"/sourcing-cases/{CASE_ID}/public-search-plan/confirm",
        json=body,
    )
    duplicate = _request(
        boss_app,
        "POST",
        f"/sourcing-cases/{CASE_ID}/public-search-plan/confirm",
        headers=[("Idempotency-Key", "confirm-a"), ("Idempotency-Key", "confirm-b")],
        json=body,
    )

    assert denied.status_code == 403
    assert missing.status_code == 400
    assert duplicate.status_code == 400


def test_review_rejects_forged_actor_opportunity_and_cost_fields() -> None:
    app, _ = _app("boss")
    response = _request(
        app,
        "POST",
        f"/sourcing-cases/{CASE_ID}/review",
        headers=[("Idempotency-Key", "review-a")],
        json={
            "primary_option_id": "sso-primary",
            "alternate_option_ids": [],
            "reason": "规格与数量档均已核验",
            "expected_case_version": 1,
            "tenant_id": "tn_forged",
            "actor_id": "emp_forged",
            "opportunity_id": "opp_forged",
            "cost_sheet_id": "cst_forged",
        },
    )

    assert response.status_code == 400


def test_candidate_read_limit_is_forwarded_and_openapi_documents_command_truth() -> (
    None
):
    app, sourcing = _app("boss")

    response = _request(app, "GET", f"/sourcing-cases/{CASE_ID}/candidates?limit=7")
    schema = app.openapi()
    command_paths = (
        "/sourcing-cases/{case_id}/public-search-plan/confirm",
        "/sourcing-cases/{case_id}/run",
        "/sourcing-cases/{case_id}/review",
        "/sourcing-cases/{case_id}/reconcile-uncertain-request",
    )

    assert response.status_code == 200
    for path in command_paths:
        operation = schema["paths"][path]["post"]
        header = next(
            parameter
            for parameter in operation["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
        )
        assert header["required"] is True
        assert "409" in operation["responses"]
    assert sourcing.calls[-1][-1] == 7


def test_confirm_invalid_state_is_documented_as_a_conflict() -> None:
    app, _ = _app("boss")
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=_Sourcing(),
        sourcing_application=_ConflictingSourcingApplication(),
    )

    response = _request(
        app,
        "POST",
        f"/sourcing-cases/{CASE_ID}/public-search-plan/confirm",
        headers=[("Idempotency-Key", "confirm-conflict")],
        json={"plan_id": "spl-source", "expected_plan_hash": "a" * 64},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "invalid_state",
        "message": "当前状态不允许此操作",
    }


def test_openapi_lists_only_safe_quota_review_and_uncertain_recovery_reads() -> None:
    app, _ = _app("boss")
    paths = app.openapi()["paths"]

    assert "/sourcing-cases/{case_id}/current-quota" in paths
    assert "/sourcing-cases/{case_id}/review" in paths
    assert "/sourcing-cases/{case_id}/uncertain-reconciliations" in paths
    assert set(paths["/sourcing-cases/{case_id}/review"]) == {"get", "post"}
