"""Run Center 的 Postgres 安全读模型实现。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.identifiers import (
    ApprovalId,
    ArtifactId,
    RunId,
    StepId,
    TenantId,
    ToolCallId,
)
from workflows.engine.audit import (
    RunApprovalView,
    RunArtifactView,
    RunDetailView,
    RunResearchView,
    RunStepView,
    RunSummaryView,
    RunToolCallView,
)

from .tables import (
    ApprovalPackageRow,
    GeneratedArtifactRow,
    SearchQuotaReservationRow,
    SearchQuotaRunRow,
    ToolCallRow,
    WorkflowRunRow,
    WorkflowStepRow,
)

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class PostgresRunAuditRepository:
    """只读取审计白名单列；每个查询都显式绑定 ``tenant_id``。"""

    def __init__(self, session_scope: SessionScope) -> None:
        self._session_scope = session_scope

    @staticmethod
    def _summary_statement(tenant_id: TenantId) -> Select[Any]:
        last_activity = (
            select(func.max(WorkflowStepRow.updated_at))
            .where(
                WorkflowStepRow.tenant_id == tenant_id,
                WorkflowStepRow.run_id == WorkflowRunRow.run_id,
            )
            .correlate(WorkflowRunRow)
            .scalar_subquery()
        )
        research_keys = (
            "execution_mode", "completion_reason", "planned_discovery_lanes", "discovery_lanes",
            "searches_used", "pages_used", "signal_count", "hypothesis_count",
            "pending_verification_count", "validated_need_count", "qualified_opportunity_count", "queued_count",
        )
        # SQL 只读取白名单 JSON 字段，不把整个 context 带入应用投影。
        research = func.jsonb_build_object(*[
            value for key in research_keys for value in (key, WorkflowRunRow.context[key])
        ]).label("research_metadata")
        counts = [
            select(func.count()).select_from(SearchQuotaReservationRow).where(
                SearchQuotaReservationRow.tenant_id == tenant_id,
                SearchQuotaReservationRow.run_id == WorkflowRunRow.run_id,
                SearchQuotaReservationRow.provider == "tavily",
                SearchQuotaReservationRow.status == status,
            ).correlate(WorkflowRunRow).scalar_subquery().label(f"{status}_credits")
            for status in ("consumed", "reserved", "uncertain")
        ]
        stop = select(SearchQuotaRunRow.stop_reason).where(
            SearchQuotaRunRow.tenant_id == tenant_id,
            SearchQuotaRunRow.run_id == WorkflowRunRow.run_id,
        ).correlate(WorkflowRunRow).scalar_subquery().label("research_stop_reason")
        return select(
            WorkflowRunRow.run_id,
            WorkflowRunRow.workflow_type,
            WorkflowRunRow.workflow_version,
            WorkflowRunRow.subject_ref,
            WorkflowRunRow.current_step,
            WorkflowRunRow.status,
            WorkflowRunRow.created_at,
            func.coalesce(last_activity, WorkflowRunRow.created_at).label(
                "last_activity_at"
            ),
            WorkflowRunRow.next_poll_at,
            WorkflowRunRow.retry_count,
            WorkflowRunRow.last_error,
            research, *counts, stop,
        ).where(WorkflowRunRow.tenant_id == tenant_id)

    @staticmethod
    def _summary(row: Any) -> RunSummaryView:
        return RunSummaryView(
            run_id=RunId(row.run_id),
            workflow_type=row.workflow_type,
            workflow_version=row.workflow_version,
            subject_ref=row.subject_ref,
            current_step=row.current_step,
            status=row.status,
            created_at=row.created_at,
            last_activity_at=row.last_activity_at,
            next_poll_at=row.next_poll_at,
            retry_count=row.retry_count,
            last_error=row.last_error,
            research=PostgresRunAuditRepository._research(row),
        )

    @staticmethod
    def _research(row: Any) -> RunResearchView | None:
        """未决预留优先于工作流完成摘要；只研究零下游计数由工作流提供。"""
        metadata = getattr(row, "research_metadata", None)
        if not isinstance(metadata, dict) or metadata.get("execution_mode") != "research_only":
            return None
        values = {key: value for key, value in metadata.items() if value is not None}
        for key in ("planned_discovery_lanes", "discovery_lanes"):
            values[key] = tuple(values.get(key, ()))
        for status in ("consumed", "reserved", "uncertain"):
            values[f"{status}_credits"] = getattr(row, f"{status}_credits", 0)
        values["stop_reason"] = (
            "request_uncertain" if values["reserved_credits"] or values["uncertain_credits"]
            else getattr(row, "research_stop_reason", None) or values.get("completion_reason")
        )
        return RunResearchView(**values)

    async def list_runs(
        self,
        tenant_id: TenantId,
        *,
        workflow_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[RunSummaryView]:
        statement = self._summary_statement(tenant_id)
        if workflow_type is not None:
            statement = statement.where(
                WorkflowRunRow.workflow_type == workflow_type
            )
        if status is not None:
            statement = statement.where(WorkflowRunRow.status == status)
        statement = statement.order_by(
            WorkflowRunRow.created_at.desc(), WorkflowRunRow.run_id.desc()
        ).limit(limit)
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        return [self._summary(row) for row in rows]

    async def get_run(
        self, tenant_id: TenantId, run_id: RunId
    ) -> RunDetailView | None:
        summary_statement = self._summary_statement(tenant_id).where(
            WorkflowRunRow.run_id == run_id
        )
        steps_statement = (
            select(
                WorkflowStepRow.step_id,
                WorkflowStepRow.step_name,
                WorkflowStepRow.status,
                WorkflowStepRow.attempt,
                WorkflowStepRow.due_at,
                WorkflowStepRow.created_at,
                WorkflowStepRow.updated_at,
                WorkflowStepRow.error,
            )
            .where(
                WorkflowStepRow.tenant_id == tenant_id,
                WorkflowStepRow.run_id == run_id,
            )
            .order_by(WorkflowStepRow.created_at, WorkflowStepRow.step_id)
        )
        tool_calls_statement = (
            select(
                ToolCallRow.tool_call_id,
                ToolCallRow.tool_id,
                ToolCallRow.tool_version,
                ToolCallRow.risk_level,
                ToolCallRow.cost_class,
                ToolCallRow.status,
                ToolCallRow.attempt_count,
                ToolCallRow.error_category,
                ToolCallRow.created_at,
                ToolCallRow.completed_at,
            )
            .where(ToolCallRow.tenant_id == tenant_id, ToolCallRow.run_id == run_id)
            .order_by(ToolCallRow.created_at, ToolCallRow.tool_call_id)
        )
        artifacts_statement = (
            select(
                GeneratedArtifactRow.artifact_id,
                GeneratedArtifactRow.kind,
                GeneratedArtifactRow.mime_type,
                GeneratedArtifactRow.subject_ref,
                GeneratedArtifactRow.generated_by,
                GeneratedArtifactRow.generated_at,
            )
            .where(
                GeneratedArtifactRow.tenant_id == tenant_id,
                GeneratedArtifactRow.workflow_run_id == run_id,
            )
            .order_by(
                GeneratedArtifactRow.generated_at,
                GeneratedArtifactRow.artifact_id,
            )
        )
        approvals_statement = (
            select(
                ApprovalPackageRow.approval_id,
                ApprovalPackageRow.approval_type,
                ApprovalPackageRow.state,
                ApprovalPackageRow.created_at,
                ApprovalPackageRow.expires_at,
                ApprovalPackageRow.decided_at,
            )
            .where(
                ApprovalPackageRow.tenant_id == tenant_id,
                ApprovalPackageRow.proposed_by_run == run_id,
            )
            .order_by(ApprovalPackageRow.created_at, ApprovalPackageRow.approval_id)
        )
        async with self._session_scope() as session:
            summary_row = (await session.execute(summary_statement)).first()
            if summary_row is None:
                return None
            steps = (await session.execute(steps_statement)).all()
            tool_calls = (await session.execute(tool_calls_statement)).all()
            artifacts = (await session.execute(artifacts_statement)).all()
            approvals = (await session.execute(approvals_statement)).all()
        return RunDetailView(
            summary=self._summary(summary_row),
            steps=tuple(
                RunStepView(
                    step_id=StepId(row.step_id),
                    step_name=row.step_name,
                    status=row.status,
                    attempt=row.attempt,
                    due_at=row.due_at,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    error=row.error,
                )
                for row in steps
            ),
            tool_calls=tuple(
                RunToolCallView(
                    tool_call_id=ToolCallId(row.tool_call_id),
                    tool_id=row.tool_id,
                    tool_version=row.tool_version,
                    risk_level=row.risk_level,
                    cost_class=row.cost_class,
                    status=row.status,
                    attempt_count=row.attempt_count,
                    error_category=row.error_category,
                    created_at=row.created_at,
                    completed_at=row.completed_at,
                )
                for row in tool_calls
            ),
            artifacts=tuple(
                RunArtifactView(
                    artifact_id=ArtifactId(row.artifact_id),
                    kind=row.kind,
                    mime_type=row.mime_type,
                    subject_ref=row.subject_ref,
                    generated_by=row.generated_by,
                    generated_at=row.generated_at,
                )
                for row in artifacts
            ),
            approvals=tuple(
                RunApprovalView(
                    approval_id=ApprovalId(row.approval_id),
                    approval_type=row.approval_type,
                    state=row.state,
                    created_at=row.created_at,
                    expires_at=row.expires_at,
                    decided_at=row.decided_at,
                )
                for row in approvals
            ),
        )


__all__ = ("PostgresRunAuditRepository",)
