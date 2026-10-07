"""寻源准入老板提案 HTTP 的严格输入、权限与确认边界。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.directives.schemas import DirectiveView, ProposalView
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
OTHER_TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0Y")
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
PROPOSAL_ID = "dpr_01K39P9M5D6K4A91YEQ80EJZ0X"
NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


class _Directives:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.proposal = ProposalView(
            proposal_id=PROPOSAL_ID,
            raw_text="按需求簇排序，每轮最多启动 3 个寻源案例",
            interpretation_summary="按需求簇规模排序后有界启动寻源案例",
            expected_behavior_changes=[
                "自动寻源准入：启用",
                "每轮最多启动 3 个寻源案例",
            ],
            parsed_fields={},
            state="pending_confirmation",
            created_at=NOW,
            sourcing_admission_mode="cluster_ranked",
            automatic_sourcing_admission_enabled=True,
            sourcing_admission_batch_limit=3,
        )

    async def submit_sourcing_admission_proposal(
        self,
        tenant_id,
        raw_text,
        config,
        interpretation_summary,
        expected_behavior_changes,
        parsed_by,
        *,
        submitted_by,
    ):
        self.calls.append(
            (
                "submit",
                tenant_id,
                raw_text,
                config,
                interpretation_summary,
                expected_behavior_changes,
                parsed_by,
                submitted_by,
            )
        )
        return PROPOSAL_ID

    async def get_proposal(self, tenant_id, proposal_id):
        self.calls.append(("get_proposal", tenant_id, proposal_id))
        return self.proposal if proposal_id == PROPOSAL_ID else None

    async def confirm_proposal(self, tenant_id, proposal_id, confirmed_by):
        self.calls.append(("confirm", tenant_id, proposal_id, confirmed_by))
        return "dir-confirmed"

    async def get_active(self, tenant_id):
        self.calls.append(("get_active", tenant_id))
        return DirectiveView(
            directive_id="dir-confirmed",
            source_proposal_id=PROPOSAL_ID,
            version=8,
            objective="focus_existing_needs",
            activated_at=NOW,
            activated_by_name="测试老板",
            sourcing_admission_mode="cluster_ranked",
            automatic_sourcing_admission_enabled=True,
            sourcing_admission_batch_limit=3,
        )


class _ExplodingWorkflowEngine:
    async def start(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("确认寻源准入指令不得启动 Workflow")


class _UnavailableDirectives(_Directives):
    async def submit_sourcing_admission_proposal(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("postgres://user:secret@example.invalid/directives")


class _UnavailableConfirmDirectives(_Directives):
    async def confirm_proposal(self, *args, **kwargs):
        del args, kwargs
        raise RuntimeError("postgres://user:secret@example.invalid/directives")


class _InvalidProjectionDirectives(_Directives):
    async def get_proposal(self, *args, **kwargs):
        del args, kwargs
        raise ValidationError("employee projection leaked secret-token")


class _EmployeeAuthorizer:
    def require(self, *args, **kwargs) -> str:
        del args, kwargs
        return "allowed"


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


def _app(role: str, directives: _Directives | None = None):
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        directives=directives,
        workflow_engine=_ExplodingWorkflowEngine(),
        employee_authorizer=_EmployeeAuthorizer(),
    )
    return app


def _request(
    app,
    method: str,
    path: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    json: object | None = None,
) -> Response:
    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
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


def _body() -> dict[str, object]:
    return {
        "message": "按需求簇排序，每轮最多启动 3 个寻源案例",
        "mode": "cluster_ranked",
        "automatic_admission_enabled": True,
        "batch_limit": 3,
    }


def test_boss_creates_strict_admission_proposal_with_explicit_behavior() -> None:
    directives = _Directives()

    response = _request(
        _app("boss", directives),
        "POST",
        "/commands/sourcing-admission-proposals",
        json=_body(),
    )

    assert response.status_code == 200
    assert response.json()["expected_behavior_changes"] == [
        "自动寻源准入：启用",
        "每轮最多启动 3 个寻源案例",
    ]
    submitted = directives.calls[0]
    assert submitted[0:3] == (
        "submit",
        TENANT,
        "按需求簇排序，每轮最多启动 3 个寻源案例",
    )
    config = submitted[3]
    assert config.mode == "cluster_ranked"
    assert config.automatic_admission_enabled is True
    assert config.batch_limit == 3


def test_admission_proposal_body_rejects_defaults_coercion_and_extra_fields() -> None:
    invalid_bodies = [
        {"message": "x", "automatic_admission_enabled": True, "batch_limit": 3},
        {**_body(), "mode": "other"},
        {**_body(), "automatic_admission_enabled": 1},
        {**_body(), "batch_limit": True},
        {**_body(), "batch_limit": 0},
        {**_body(), "batch_limit": 51},
        {**_body(), "tenant_id": str(OTHER_TENANT)},
    ]
    directives = _Directives()
    app = _app("boss", directives)

    responses = [
        _request(
            app,
            "POST",
            "/commands/sourcing-admission-proposals",
            json=body,
        )
        for body in invalid_bodies
    ]

    assert [response.status_code for response in responses] == [400] * len(
        invalid_bodies
    )
    assert directives.calls == []


def test_proposal_create_and_confirm_are_boss_only_before_directive_io() -> None:
    directives = _Directives()
    app = _app("sourcing", directives)

    created = _request(
        app,
        "POST",
        "/commands/sourcing-admission-proposals",
        json=_body(),
    )
    confirmed = _request(
        app,
        "POST",
        f"/commands/sourcing-admission-proposals/{PROPOSAL_ID}/confirm",
        headers=[("Idempotency-Key", "confirm-admission-policy")],
    )

    assert created.status_code == confirmed.status_code == 403
    assert directives.calls == []


def test_confirm_returns_active_version_and_never_starts_workflow() -> None:
    directives = _Directives()

    response = _request(
        _app("boss", directives),
        "POST",
        f"/commands/sourcing-admission-proposals/{PROPOSAL_ID}/confirm",
        headers=[("Idempotency-Key", "confirm-admission-policy")],
    )

    assert response.status_code == 200
    assert response.json() == {
        "proposal_id": PROPOSAL_ID,
        "directive_id": "dir-confirmed",
        "directive_version": 8,
        "mode": "cluster_ranked",
        "automatic_admission_enabled": True,
        "batch_limit": 3,
    }
    assert [call[0] for call in directives.calls] == [
        "get_proposal",
        "confirm",
        "get_active",
    ]


def test_confirm_requires_one_raw_idempotency_key_and_existing_proposal() -> None:
    directives = _Directives()
    app = _app("boss", directives)

    missing_header = _request(
        app,
        "POST",
        f"/commands/sourcing-admission-proposals/{PROPOSAL_ID}/confirm",
    )
    duplicate_header = _request(
        app,
        "POST",
        f"/commands/sourcing-admission-proposals/{PROPOSAL_ID}/confirm",
        headers=[("Idempotency-Key", "a"), ("Idempotency-Key", "b")],
    )
    absent = _request(
        app,
        "POST",
        "/commands/sourcing-admission-proposals/dpr_01K39P9M5D6K4A91YEQ80EJZ0Y/confirm",
        headers=[("Idempotency-Key", "confirm-absent")],
    )

    assert missing_header.status_code == 400
    assert duplicate_header.status_code == 400
    assert absent.status_code == 404


def test_missing_directive_dependency_is_fixed_503() -> None:
    response = _request(
        _app("boss", None),
        "POST",
        "/commands/sourcing-admission-proposals",
        json=_body(),
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "service_unavailable",
        "message": "服务暂时不可用",
    }


def test_directive_failures_are_fixed_503_and_openapi_documents_safe_errors() -> None:
    create_response = _request(
        _app("boss", _UnavailableDirectives()),
        "POST",
        "/commands/sourcing-admission-proposals",
        json=_body(),
    )
    confirm_response = _request(
        _app("boss", _UnavailableConfirmDirectives()),
        "POST",
        f"/commands/sourcing-admission-proposals/{PROPOSAL_ID}/confirm",
        headers=[("Idempotency-Key", "confirm-admission-policy")],
    )

    assert create_response.status_code == confirm_response.status_code == 503
    for response in (create_response, confirm_response):
        assert response.json() == {
            "code": "service_unavailable",
            "message": "服务暂时不可用",
        }
        assert "secret" not in response.text

    paths = _app("boss", _Directives()).openapi()["paths"]
    for path in (
        "/commands/sourcing-admission-proposals",
        "/commands/sourcing-admission-proposals/{proposal_id}/confirm",
    ):
        assert {"400", "403", "404", "409", "503"} <= set(
            paths[path]["post"]["responses"]
        )


def test_internal_proposal_projection_validation_is_sanitized_503() -> None:
    app = _app("boss", _InvalidProjectionDirectives())
    responses = (
        _request(
            app,
            "POST",
            "/commands/sourcing-admission-proposals",
            json=_body(),
        ),
        _request(
            app,
            "POST",
            f"/commands/sourcing-admission-proposals/{PROPOSAL_ID}/confirm",
            headers=[("Idempotency-Key", "confirm-projection-failure")],
        ),
    )

    for response in responses:
        assert response.status_code == 503
        assert response.json() == {
            "code": "service_unavailable",
            "message": "服务暂时不可用",
        }
        assert "secret-token" not in response.text
