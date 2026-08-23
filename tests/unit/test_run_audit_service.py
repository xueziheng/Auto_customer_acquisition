"""Run 审计服务必须二次判权，并且只返回仓储提供的安全读模型。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from shared.errors import PermissionDenied
from shared.schemas.identifiers import RunId, TenantId
from workflows.engine.audit import (
    Phase1RunAuditAuthorizer,
    RunApprovalView,
    RunArtifactView,
    RunAuditActor,
    RunAuditService,
    RunDetailView,
    RunStepView,
    RunSummaryView,
    RunToolCallView,
)

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
RUN_ID = RunId("run_01K39P9M5D6K4A91YEQ80EJZ0X")
NOW = datetime(2026, 8, 23, 9, tzinfo=UTC)


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[TenantId, str | None, str | None, int]] = []

    async def list_runs(
        self,
        tenant_id: TenantId,
        *,
        workflow_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[RunSummaryView]:
        self.calls.append((tenant_id, workflow_type, status, limit))
        return [
            RunSummaryView(
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
        ]

    async def get_run(
        self, tenant_id: TenantId, run_id: RunId
    ) -> RunDetailView | None:
        assert tenant_id == TENANT
        assert run_id == RUN_ID
        summary = (await self.list_runs(
            tenant_id,
            workflow_type=None,
            status=None,
            limit=1,
        ))[0]
        return RunDetailView(
            summary=summary,
            steps=(
                RunStepView(
                    step_id="wfs_01K39P9M5D6K4A91YEQ80EJZ0X",
                    step_name="discover",
                    status="completed",
                    attempt=1,
                    due_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                    error=None,
                ),
            ),
            tool_calls=(
                RunToolCallView(
                    tool_call_id="tc_01K39P9M5D6K4A91YEQ80EJZ0X",
                    tool_id="web.search",
                    tool_version="1",
                    risk_level="low",
                    cost_class="low",
                    status="succeeded",
                    attempt_count=1,
                    error_category=None,
                    created_at=NOW,
                    completed_at=NOW,
                ),
            ),
            artifacts=(
                RunArtifactView(
                    artifact_id="art_01K39P9M5D6K4A91YEQ80EJZ0X",
                    kind="email_draft",
                    mime_type="application/vnd.tradeos.email-draft+json",
                    subject_ref="enr_01K39P9M5D6K4A91YEQ80EJZ0X",
                    generated_by="outreach-agent",
                    generated_at=NOW,
                ),
            ),
            approvals=(
                RunApprovalView(
                    approval_id="apr_01K39P9M5D6K4A91YEQ80EJZ0X",
                    approval_type="campaign_activation",
                    state="approved",
                    created_at=NOW,
                    expires_at=NOW,
                    decided_at=NOW,
                ),
            ),
        )


def test_boss_can_list_tenant_runs_after_service_authorization() -> None:
    repository = _Repository()
    service = RunAuditService(repository, Phase1RunAuditAuthorizer(TENANT))

    result = asyncio.run(
        service.list_runs(
            TENANT,
            actor=RunAuditActor("emp_boss", "boss"),
            workflow_type="demand_discovery",
            status="running",
            limit=25,
        )
    )

    assert [item.run_id for item in result] == [RUN_ID]
    assert repository.calls == [(TENANT, "demand_discovery", "running", 25)]


def test_non_boss_and_cross_tenant_queries_fail_before_repository_access() -> None:
    repository = _Repository()
    service = RunAuditService(repository, Phase1RunAuditAuthorizer(TENANT))

    with pytest.raises(PermissionDenied):
        asyncio.run(
            service.list_runs(
                TENANT,
                actor=RunAuditActor("emp_sales", "sales"),
                workflow_type=None,
                status=None,
                limit=25,
            )
        )
    with pytest.raises(PermissionDenied):
        asyncio.run(
            service.list_runs(
                TenantId("tn_other"),
                actor=RunAuditActor("emp_boss", "boss"),
                workflow_type=None,
                status=None,
                limit=25,
            )
        )

    assert repository.calls == []


def test_boss_can_read_safe_run_detail_without_context_or_business_payloads() -> None:
    service = RunAuditService(_Repository(), Phase1RunAuditAuthorizer(TENANT))

    detail = asyncio.run(
        service.get_run(
            TENANT,
            RUN_ID,
            actor=RunAuditActor("emp_boss", "boss"),
        )
    )

    assert detail is not None
    payload = detail.model_dump(mode="json")
    assert payload["steps"][0]["step_name"] == "discover"
    assert payload["tool_calls"][0]["tool_id"] == "web.search"
    assert payload["artifacts"][0]["kind"] == "email_draft"
    assert payload["approvals"][0]["state"] == "approved"
    assert "context" not in str(payload)
    assert "data" not in payload["steps"][0]
