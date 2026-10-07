"""Run Center 的 Postgres 安全读模型实现。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.identifiers import (
    ApprovalId,
    ArtifactId,
    RunId,
    SourcingCaseId,
    StepId,
    TenantId,
    ToolCallId,
)
from workflows.engine.audit import (
    RunApprovalView,
    RunArtifactView,
    RunDetailView,
    RunResearchView,
    RunSourcingLadderView,
    RunSourcingStopView,
    RunSourcingView,
    RunStepView,
    RunSummaryView,
    RunToolCallView,
)
from workflows.engine.observability import RunObservation, WebCoreObservation

from .tables import (
    ApprovalPackageRow,
    GeneratedArtifactRow,
    HandoffRow,
    OpportunityRow,
    SearchQuotaReservationRow,
    SearchQuotaRunRow,
    SourcingCandidateRow,
    SourcingCaseRow,
    SourcingLadderCheckRow,
    SourcingPageAttemptRow,
    SourcingPublicPlanRow,
    SourcingReviewRow,
    ToolCallRow,
    ValidatedNeedRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from .web_core_observability import read_web_core_observation

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class PostgresRunAuditRepository:
    """只读取审计白名单列；每个查询都显式绑定 ``tenant_id``。"""

    def __init__(self, session_scope: SessionScope) -> None:
        self._session_scope = session_scope

    async def get_observability(
        self, tenant_id: TenantId, *, start: datetime, end: datetime, observed_at: datetime,
    ) -> WebCoreObservation:
        """只读取已有事实的观测投影，不写历史账本。"""
        return await read_web_core_observation(
            self._session_scope, tenant_id, start=start, end=end, observed_at=observed_at,
        )

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
            "execution_mode",
            "completion_reason",
            "planned_discovery_lanes",
            "discovery_lanes",
            "planned_source_channels",
            "searched_source_channels",
            "source_channels",
            "searches_used",
            "pages_used",
            "signal_count",
            "hypothesis_count",
            "pending_verification_count",
            "validated_need_count",
            "qualified_opportunity_count",
            "queued_count",
        )
        # SQL 只读取白名单 JSON 字段，不把整个 context 带入应用投影。
        research = func.jsonb_build_object(
            *[
                value
                for key in research_keys
                for value in (key, WorkflowRunRow.context[key])
            ]
        ).label("research_metadata")
        counts = [
            select(func.count())
            .select_from(SearchQuotaReservationRow)
            .where(
                SearchQuotaReservationRow.tenant_id == tenant_id,
                SearchQuotaReservationRow.run_id == WorkflowRunRow.run_id,
                SearchQuotaReservationRow.provider == "tavily",
                SearchQuotaReservationRow.status == status,
            )
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label(f"{status}_credits")
            for status in ("consumed", "reserved", "uncertain")
        ]
        stop = (
            select(SearchQuotaRunRow.stop_reason)
            .where(
                SearchQuotaRunRow.tenant_id == tenant_id,
                SearchQuotaRunRow.run_id == WorkflowRunRow.run_id,
            )
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label("research_stop_reason")
        )
        bound_case = (
            SourcingCaseRow.tenant_id == tenant_id,
            SourcingCaseRow.case_id == WorkflowRunRow.subject_ref,
            SourcingCaseRow.workflow_version == 2,
            WorkflowRunRow.workflow_type == "sourcing_case",
            WorkflowRunRow.workflow_version == 2,
        )
        sourcing_case_id = (
            select(SourcingCaseRow.case_id)
            .where(*bound_case)
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label("sourcing_case_id")
        )
        sourcing_plan_status = (
            select(SourcingPublicPlanRow.status)
            .join(
                SourcingCaseRow,
                (
                    (SourcingCaseRow.tenant_id == SourcingPublicPlanRow.tenant_id)
                    & (SourcingCaseRow.case_id == SourcingPublicPlanRow.case_id)
                    & (
                        SourcingCaseRow.active_search_plan_id
                        == SourcingPublicPlanRow.plan_id
                    )
                ),
            )
            .where(*bound_case, SourcingPublicPlanRow.tenant_id == tenant_id)
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label("sourcing_plan_status")
        )
        sourcing_ladder = (
            select(
                func.jsonb_agg(
                    aggregate_order_by(
                        func.jsonb_build_object(
                            "rung",
                            SourcingLadderCheckRow.rung,
                            "outcome",
                            SourcingLadderCheckRow.outcome,
                        ),
                        SourcingLadderCheckRow.sequence_number,
                    )
                )
            )
            .where(
                SourcingLadderCheckRow.tenant_id == tenant_id,
                SourcingLadderCheckRow.case_id == sourcing_case_id,
            )
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label("sourcing_ladder")
        )

        def sourcing_count(table: type[Any], *criteria: Any) -> Any:
            return (
                select(func.count())
                .select_from(table)
                .where(table.tenant_id == tenant_id, *criteria)
                .correlate(WorkflowRunRow)
                .scalar_subquery()
            )

        sourcing_search_attempts = sourcing_count(
            ToolCallRow,
            ToolCallRow.run_id == WorkflowRunRow.run_id,
            ToolCallRow.tool_id == "web.search",
        ).label("sourcing_search_attempt_count")
        sourcing_page_attempts = sourcing_count(
            SourcingPageAttemptRow,
            SourcingPageAttemptRow.run_id == WorkflowRunRow.run_id,
            SourcingPageAttemptRow.case_id == sourcing_case_id,
        ).label("sourcing_page_attempt_count")
        sourcing_candidates = sourcing_count(
            SourcingCandidateRow,
            SourcingCandidateRow.case_id == sourcing_case_id,
        ).label("sourcing_candidate_count")
        sourcing_primary_count = sourcing_count(
            SourcingReviewRow,
            SourcingReviewRow.case_id == sourcing_case_id,
        ).label("sourcing_primary_count")
        sourcing_alternate_count = (
            select(
                func.coalesce(
                    func.max(
                        func.jsonb_array_length(SourcingReviewRow.alternate_option_ids)
                    ),
                    0,
                )
            )
            .where(
                SourcingReviewRow.tenant_id == tenant_id,
                SourcingReviewRow.case_id == sourcing_case_id,
            )
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label("sourcing_alternate_count")
        )
        sourcing_stop = (
            select(
                func.jsonb_build_object(
                    "code",
                    SourcingCaseRow.stop_code,
                    "stage",
                    SourcingCaseRow.stop_detail["stage"],
                    "query_index",
                    SourcingCaseRow.stop_detail["query_index"],
                    "provider_http_status",
                    SourcingCaseRow.stop_detail["provider_http_status"],
                    "observed_count",
                    SourcingCaseRow.stop_detail["observed_count"],
                    "configured_limit",
                    SourcingCaseRow.stop_detail["configured_limit"],
                )
            )
            .where(*bound_case, SourcingCaseRow.stop_code.is_not(None))
            .correlate(WorkflowRunRow)
            .scalar_subquery()
            .label("sourcing_stop_metadata")
        )
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
            research,
            *counts,
            stop,
            sourcing_case_id,
            sourcing_plan_status,
            sourcing_ladder,
            sourcing_search_attempts,
            sourcing_page_attempts,
            sourcing_candidates,
            sourcing_primary_count,
            sourcing_alternate_count,
            sourcing_stop,
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
            sourcing=PostgresRunAuditRepository._sourcing(row),
        )

    @staticmethod
    def _research(row: Any) -> RunResearchView | None:
        """未决预留优先于工作流完成摘要；只研究零下游计数由工作流提供。"""
        metadata = getattr(row, "research_metadata", None)
        if (
            not isinstance(metadata, dict)
            or metadata.get("execution_mode") != "research_only"
        ):
            return None
        values = {key: value for key, value in metadata.items() if value is not None}
        for key in (
            "planned_discovery_lanes", "discovery_lanes",
            "planned_source_channels", "searched_source_channels", "source_channels",
        ):
            values[key] = tuple(values.get(key, ()))
        for status in ("consumed", "reserved", "uncertain"):
            values[f"{status}_credits"] = getattr(row, f"{status}_credits", 0)
        values["stop_reason"] = (
            "request_uncertain"
            if values["reserved_credits"] or values["uncertain_credits"]
            else getattr(row, "research_stop_reason", None)
            or values.get("completion_reason")
        )
        return RunResearchView(**values)

    @staticmethod
    def _sourcing(row: Any) -> RunSourcingView | None:
        """只接受 SQL 已证明的同租户 Case/V2 绑定与安全聚合。"""
        case_id = getattr(row, "sourcing_case_id", None)
        if not isinstance(case_id, str):
            return None
        ladder = getattr(row, "sourcing_ladder", None) or []
        stop = getattr(row, "sourcing_stop_metadata", None)
        return RunSourcingView(
            case_id=SourcingCaseId(case_id),
            plan_status=getattr(row, "sourcing_plan_status", None),
            ladder=tuple(RunSourcingLadderView(**item) for item in ladder),
            search_attempt_count=getattr(row, "sourcing_search_attempt_count", 0),
            page_attempt_count=getattr(row, "sourcing_page_attempt_count", 0),
            candidate_count=getattr(row, "sourcing_candidate_count", 0),
            primary_count=getattr(row, "sourcing_primary_count", 0),
            alternate_count=getattr(row, "sourcing_alternate_count", 0),
            consumed_credits=getattr(row, "consumed_credits", 0),
            reserved_credits=getattr(row, "reserved_credits", 0),
            uncertain_credits=getattr(row, "uncertain_credits", 0),
            stop_reason=RunSourcingStopView(**stop) if isinstance(stop, dict) else None,
        )

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
            statement = statement.where(WorkflowRunRow.workflow_type == workflow_type)
        if status is not None:
            statement = statement.where(WorkflowRunRow.status == status)
        statement = statement.order_by(
            WorkflowRunRow.created_at.desc(), WorkflowRunRow.run_id.desc()
        ).limit(limit)
        async with self._session_scope() as session:
            rows = (await session.execute(statement)).all()
        return [self._summary(row) for row in rows]

    @staticmethod
    async def _run_binding(
        session: AsyncSession, tenant_id: TenantId, run_id: RunId,
    ) -> Any:
        """绑定必须由流程类型和同租户持久化关系证明，禁止subject前缀猜链。"""
        handoff = select(
            HandoffRow.handoff_id, HandoffRow.opportunity_id,
            ValidatedNeedRow.need_id, HandoffRow.assigned_to.label("responsible_employee_id"),
        ).join(WorkflowRunRow, (WorkflowRunRow.tenant_id == HandoffRow.tenant_id)
            & (WorkflowRunRow.subject_ref == HandoffRow.handoff_id)).join(
            OpportunityRow, (OpportunityRow.tenant_id == HandoffRow.tenant_id)
            & (OpportunityRow.opportunity_id == HandoffRow.opportunity_id),
        ).outerjoin(ValidatedNeedRow, (ValidatedNeedRow.tenant_id == OpportunityRow.tenant_id)
            & (ValidatedNeedRow.need_id == OpportunityRow.need_id)).where(
            WorkflowRunRow.tenant_id == tenant_id, WorkflowRunRow.run_id == run_id,
            WorkflowRunRow.workflow_type == "human_handoff",
            HandoffRow.tenant_id == tenant_id, OpportunityRow.tenant_id == tenant_id,
        )
        # 没有可信已分配人时保持null；不把老板审计权限误作任务归属。
        from sqlalchemy import null
        sourcing = select(
            null().label("handoff_id"), OpportunityRow.opportunity_id,
            ValidatedNeedRow.need_id, OpportunityRow.owner.label("responsible_employee_id"),
        ).select_from(SourcingCaseRow).join(WorkflowRunRow,
            (WorkflowRunRow.tenant_id == SourcingCaseRow.tenant_id)
            & (WorkflowRunRow.subject_ref == SourcingCaseRow.case_id)
            & (WorkflowRunRow.workflow_version == SourcingCaseRow.workflow_version),
        ).join(ValidatedNeedRow, (ValidatedNeedRow.tenant_id == SourcingCaseRow.tenant_id)
            & (ValidatedNeedRow.need_id == SourcingCaseRow.need_id)).outerjoin(
            OpportunityRow, (OpportunityRow.tenant_id == SourcingCaseRow.tenant_id)
            & (OpportunityRow.opportunity_id == SourcingCaseRow.opportunity_id)
            & (OpportunityRow.need_id == SourcingCaseRow.need_id),
        ).where(WorkflowRunRow.tenant_id == tenant_id, WorkflowRunRow.run_id == run_id,
            WorkflowRunRow.workflow_type == "sourcing_case",
            SourcingCaseRow.tenant_id == tenant_id, ValidatedNeedRow.tenant_id == tenant_id)
        return (await session.execute(handoff.union_all(sourcing))).first()

    @staticmethod
    def _run_observation(summary: Any, steps: Any, calls: Any, binding: Any) -> RunObservation:
        """只计算记录时间跨度；异序时钟明确未知，等待不冒充人工耗时。"""
        invalid = sum(1 for step in steps if step.updated_at < step.created_at
                      or step.created_at < summary.created_at)
        invalid += sum(1 for call in calls if call.created_at < summary.created_at
                       or (call.completed_at is not None and call.completed_at < call.created_at))
        elapsed = max(step.updated_at for step in steps) - summary.created_at if steps and not invalid else None
        return RunObservation(
            call_count=len({call.tool_call_id for call in calls if call.status != "duplicate"}),
            attempt_count=sum(call.attempt_count for call in calls if call.status != "duplicate"),
            duplicate_receipt_count=sum(1 for call in calls if call.status == "duplicate"),
            recorded_span_seconds=elapsed.days * 86400 + elapsed.seconds if elapsed is not None else None,
            invalid_time_count=invalid,
            handoff_id=binding.handoff_id if binding else None,
            opportunity_id=binding.opportunity_id if binding else None,
            need_id=binding.need_id if binding else None,
            responsible_employee_id=binding.responsible_employee_id if binding else None,
        )

    async def get_run(self, tenant_id: TenantId, run_id: RunId) -> RunDetailView | None:
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
            binding = await self._run_binding(session, tenant_id, run_id)
        return RunDetailView(
            summary=self._summary(summary_row),
            observation=self._run_observation(summary_row, steps, tool_calls, binding),
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
