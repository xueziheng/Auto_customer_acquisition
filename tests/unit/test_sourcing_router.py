"""Sourcing Center HTTP 的角色、身份与命令边界。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial
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
from domains.sourcing.schemas import (
    PublicSourcingPlanReadView,
    PublicSourcingQueryReadView,
    SourcingAdmissionReadView,
)
from shared.errors import InvalidStateTransition
from shared.schemas.identifiers import EmployeeId, SourcingPlanId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
CASE_ID = "src_01K39P9M5D6K4A91YEQ80EJZ0X"
ADMISSION_ID = "sad_01K39P9M5D6K4A91YEQ80EJZ0X"
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


class _PlanSourcing(_Sourcing):
    async def get_public_plan_read_view(self, tenant_id, case_id, *, actor):
        del tenant_id, case_id, actor
        return PublicSourcingPlanReadView(
            plan_id=SourcingPlanId("spl-source"),
            case_id=CASE_ID,
            target_countries=("US",),
            product_category="hinges",
            queries=(
                PublicSourcingQueryReadView(
                    query_text="marine hinge supplier", target_country="US"
                ),
            ),
            max_search_queries=1,
            max_pages_read=1,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=10,
            worst_case_credits=1,
            version=1,
            expected_case_version=6,
            plan_hash="a" * 64,
            status="draft",
            confirmed_by=None,
            confirmed_at=None,
            created_at=datetime.now(UTC),
        )


class _PlanApplication:
    def __init__(self) -> None:
        self.command = None

    async def create_plan(self, tenant_id, case_id, command, *, actor) -> None:
        self.command = (tenant_id, case_id, command, actor)


def _admission_view(*, state: str = "waiting") -> SourcingAdmissionReadView:
    return SourcingAdmissionReadView(
        admission_id=ADMISSION_ID,
        case_id=CASE_ID,
        need_id="need_01K39P9M5D6K4A91YEQ80EJZ0X",
        state=state,
        snapshot_id="sps_01K39P9M5D6K4A91YEQ80EJZ0X",
        cluster_id="ncl_01K39P9M5D6K4A91YEQ80EJZ0X",
        cluster_member_count=8,
        ready_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        facts_observed_at=datetime(2026, 9, 2, 11, tzinfo=UTC),
        ranking_version="need-cluster-admission-v1",
        explanation="该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。",
        waiting_duration_seconds=86_400,
        admitted_at=datetime(2026, 9, 2, 12, tzinfo=UTC)
        if state == "admitted"
        else None,
        admitted_by=str(EMPLOYEE) if state == "admitted" else None,
        can_current_user_manual_start=state == "waiting",
    )


class _AdmissionApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_read_view(self, tenant_id, *, state, limit, actor):
        self.calls.append(("list", tenant_id, state, limit, actor))
        return {
            "policy": {
                "status": "automatic_admission_disabled",
                "directive_id": "dir-policy",
                "directive_version": 7,
                "automatic_admission_enabled": False,
                "batch_limit": 3,
            },
            "items": (_admission_view(),),
        }

    async def get_read_view(self, tenant_id, admission_id, *, actor):
        self.calls.append(("get", tenant_id, admission_id, actor))
        if str(admission_id).endswith("Z0Y"):
            return None
        return {
            "policy": {
                "status": "policy_not_configured",
                "directive_id": None,
                "directive_version": None,
                "automatic_admission_enabled": None,
                "batch_limit": None,
            },
            "admission": _admission_view(),
        }

    async def admit_one(self, tenant_id, admission_id, *, request_id, actor):
        self.calls.append(("admit", tenant_id, admission_id, request_id, actor))
        return _admission_view(state="admitted")


def _dependencies_with_admission(admission: _AdmissionApplication) -> SimpleNamespace:
    return SimpleNamespace(
        sourcing=_Sourcing(),
        sourcing_application=_SourcingApplication(),
        sourcing_admission_application=admission,
    )


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


def test_plan_draft_accepts_json_arrays_but_passes_immutable_domain_tuples() -> None:
    app, _ = _app("boss")
    application = _PlanApplication()
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=_PlanSourcing(), sourcing_application=application
    )

    response = _request(
        app,
        "POST",
        f"/sourcing-cases/{CASE_ID}/public-search-plan",
        json={
            "plan_id": "spl-source",
            "case_id": CASE_ID,
            "target_countries": ["US"],
            "product_category": "hinges",
            "queries": [
                {"query_text": "marine hinge supplier", "target_country": "US"}
            ],
            "max_search_queries": 1,
            "max_pages_read": 1,
            "provider": "tavily",
            "search_depth": "basic",
            "usage_credits_remaining": 10,
            "worst_case_credits": 1,
            "version": 1,
            "expected_case_version": 6,
        },
    )

    assert application.command is not None
    assert response.status_code == 200
    command = application.command[2]
    assert command.target_countries == ("US",)
    assert isinstance(command.queries, tuple)


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


def test_admission_list_and_detail_use_safe_views_for_existing_read_roles() -> None:
    for role in ("boss", "product", "sourcing", "finance"):
        app, _ = _app(role)
        admission = _AdmissionApplication()
        app.dependency_overrides[get_api_dependencies] = partial(
            _dependencies_with_admission, admission
        )

        listed = _request(app, "GET", "/sourcing-admissions?state=waiting&limit=7")
        detailed = _request(app, "GET", f"/sourcing-admissions/{ADMISSION_ID}")

        assert listed.status_code == 200
        assert listed.json()["policy"]["status"] == "automatic_admission_disabled"
        assert listed.json()["policy"]["batch_limit"] == 3
        assert listed.json()["items"][0]["cluster_member_count"] == 8
        assert detailed.status_code == 200
        assert detailed.json()["policy"]["status"] == "policy_not_configured"
        assert detailed.json()["admission"]["admission_id"] == ADMISSION_ID
        assert "claim_token" not in detailed.text
        assert "claim_expires_at" not in detailed.text
        assert admission.calls[0][0:4] == ("list", TENANT, "waiting", 7)


def test_admission_read_role_and_tenant_gates_run_before_application_io() -> None:
    app, _ = _app("manager")
    admission = _AdmissionApplication()
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=_Sourcing(),
        sourcing_application=_SourcingApplication(),
        sourcing_admission_application=admission,
    )

    denied_role = _request(app, "GET", "/sourcing-admissions")

    async def wrong_tenant() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.get(
                "/sourcing-admissions",
                headers=[
                    ("X-Employee-Id", str(EMPLOYEE)),
                    ("X-Tenant-Id", "tn_01K39P9M5D6K4A91YEQ80EJZ0Y"),
                ],
            )

    denied_tenant = asyncio.run(wrong_tenant())

    assert denied_role.status_code == 403
    assert denied_tenant.status_code == 403
    assert admission.calls == []


def test_admission_detail_absent_is_404_and_dependency_absence_is_503() -> None:
    app, _ = _app("boss")
    admission = _AdmissionApplication()
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=_Sourcing(),
        sourcing_application=_SourcingApplication(),
        sourcing_admission_application=admission,
    )
    absent_id = "sad_01K39P9M5D6K4A91YEQ80EJZ0Y"

    absent = _request(app, "GET", f"/sourcing-admissions/{absent_id}")
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=_Sourcing(),
        sourcing_application=_SourcingApplication(),
        sourcing_admission_application=None,
    )
    unavailable = _request(app, "GET", "/sourcing-admissions")

    assert absent.status_code == 404
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "code": "service_unavailable",
        "message": "服务暂时不可用",
    }


def test_manual_admit_is_boss_or_sourcing_only_with_exact_raw_request_id() -> None:
    for role, expected in (("boss", 200), ("sourcing", 200), ("product", 403)):
        app, _ = _app(role)
        admission = _AdmissionApplication()
        app.dependency_overrides[get_api_dependencies] = partial(
            _dependencies_with_admission, admission
        )

        response = _request(
            app,
            "POST",
            f"/sourcing-admissions/{ADMISSION_ID}/admit",
            headers=[("Idempotency-Key", "manual-admit-raw-key")],
        )

        assert response.status_code == expected
        if expected == 200:
            assert response.json()["state"] == "admitted"
            assert admission.calls[0][0:4] == (
                "admit",
                TENANT,
                ADMISSION_ID,
                "manual-admit-raw-key",
            )
        else:
            assert admission.calls == []


def test_manual_admit_rejects_missing_duplicate_or_invalid_raw_key_before_io() -> None:
    app, _ = _app("boss")
    admission = _AdmissionApplication()
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        sourcing=_Sourcing(),
        sourcing_application=_SourcingApplication(),
        sourcing_admission_application=admission,
    )

    responses = [
        _request(app, "POST", f"/sourcing-admissions/{ADMISSION_ID}/admit"),
        _request(
            app,
            "POST",
            f"/sourcing-admissions/{ADMISSION_ID}/admit",
            headers=[("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
        ),
        _request(
            app,
            "POST",
            f"/sourcing-admissions/{ADMISSION_ID}/admit",
            headers=[("Idempotency-Key", " padded")],
        ),
    ]

    assert [response.status_code for response in responses] == [400, 400, 400]
    assert admission.calls == []
    operation = app.openapi()["paths"]["/sourcing-admissions/{admission_id}/admit"][
        "post"
    ]
    header = next(
        item
        for item in operation["parameters"]
        if item["in"] == "header" and item["name"] == "Idempotency-Key"
    )
    assert header["required"] is True
