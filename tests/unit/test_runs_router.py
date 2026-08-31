"""Run Center API 只允许老板读取安全审计投影。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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
from shared.schemas.identifiers import EmployeeId, RunId, TenantId
from workflows.engine.audit import (
    RunAuditActor,
    RunDetailView,
    RunSourcingLadderView,
    RunSourcingStopView,
    RunSourcingView,
    RunSummaryView,
)

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
BOSS = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
SALES = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0Y")
RUN_ID = RunId("run_01K39P9M5D6K4A91YEQ80EJZ0X")
NOW = datetime(2026, 8, 23, 9, tzinfo=UTC)


def _summary() -> RunSummaryView:
    return RunSummaryView(
        run_id=RUN_ID,
        workflow_type="demand_discovery",
        workflow_version=1,
        subject_ref="schedule:daily",
        current_step="discover",
        status="running",
        created_at=NOW,
        last_activity_at=NOW,
        next_poll_at=None,
        retry_count=0,
        last_error=None,
    )


class _RunAudit:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_runs(
        self,
        tenant_id: TenantId,
        *,
        actor: RunAuditActor,
        workflow_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[RunSummaryView]:
        self.calls.append(("list", tenant_id, actor, workflow_type, status, limit))
        return [_summary()]

    async def get_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        *,
        actor: RunAuditActor,
    ) -> RunDetailView | None:
        self.calls.append(("get", tenant_id, run_id, actor))
        return RunDetailView(
            summary=_summary(),
            steps=(),
            tool_calls=(),
            artifacts=(),
            approvals=(),
        )


def _identity(role: str) -> RequestIdentity:
    employee_id = BOSS if role == "boss" else SALES
    employee = EmployeeView(
        employee_id=employee_id,
        tenant_id=TENANT,
        name="老板" if role == "boss" else "业务员",
        role=role,
    )
    return RequestIdentity(
        tenant_id=TENANT,
        employee=employee,
        employee_actor=EmployeeActor(
            str(employee_id),
            EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF,
            role,
        ),
        opportunity_actor=OpportunityActor(str(employee_id), OpportunityScope(), role),
    )


def _app(role: str = "boss") -> tuple[object, _RunAudit]:
    audit = _RunAudit()
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        run_audit=audit
    )
    return app, audit


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


def test_boss_lists_and_reads_tenant_run_audit() -> None:
    app, audit = _app()

    listed = _get(
        app,
        "/runs?workflow_type=demand_discovery&status=running&limit=20",
    )
    detail = _get(app, f"/runs/{RUN_ID}")

    assert listed.status_code == 200
    assert listed.json()[0]["run_id"] == str(RUN_ID)
    assert detail.status_code == 200
    assert detail.json()["summary"]["workflow_type"] == "demand_discovery"
    actor = RunAuditActor(str(BOSS), "boss")
    assert audit.calls == [
        ("list", TENANT, actor, "demand_discovery", "running", 20),
        ("get", TENANT, RUN_ID, actor),
    ]


def test_non_boss_is_rejected_before_run_audit_service_call() -> None:
    app, audit = _app("sales")

    response = _get(app, "/runs")

    assert response.status_code == 403
    assert audit.calls == []


def test_invalid_run_id_is_rejected_without_querying_service() -> None:
    app, audit = _app()

    response = _get(app, "/runs/not-a-run")

    assert response.status_code == 400
    assert audit.calls == []


def test_sourcing_summary_exposes_only_the_safe_whitelist() -> None:
    view = RunSourcingView(
        case_id="src_01K39P9M5D6K4A91YEQ80EJZ0X",
        plan_status="running",
        ladder=(RunSourcingLadderView(rung=1, outcome="no_qualified_supply"),),
        search_attempt_count=2,
        page_attempt_count=3,
        candidate_count=1,
        primary_count=1,
        alternate_count=0,
        consumed_credits=1,
        reserved_credits=0,
        uncertain_credits=0,
        stop_reason=RunSourcingStopView(
            code="page_limit",
            stage="public_search",
            query_index=1,
            provider_http_status=None,
            observed_count=3,
            configured_limit=3,
        ),
    )

    payload = view.model_dump(mode="json")

    assert set(payload) == {
        "case_id",
        "plan_status",
        "ladder",
        "search_attempt_count",
        "page_attempt_count",
        "candidate_count",
        "primary_count",
        "alternate_count",
        "consumed_credits",
        "reserved_credits",
        "uncertain_credits",
        "stop_reason",
    }
    serialized = str(payload).casefold()
    assert all(
        forbidden not in serialized
        for forbidden in (
            "query_text",
            "page_text",
            "context",
            "email",
            "phone",
            "secret_ref",
        )
    )
