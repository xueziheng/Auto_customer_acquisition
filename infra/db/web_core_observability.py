"""单SQL快照读取canonical实体与调用记录；不从Run context推断业务成绩。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.identifiers import TenantId
from workflows.engine.observability import (
    HandoffObservation,
    HandoffOwnerObservation,
    ObservationStage,
    SourceCallObservation,
    StageObservation,
    WebCoreObservation,
)

from .tables import (
    DemandSignalRow,
    HandoffRow,
    NeedHypothesisRow,
    OpportunityRow,
    QuotationRow,
    SearchQuotaReservationRow,
    ToolCallRow,
    ValidatedNeedRow,
)


async def read_web_core_observation(
    session_scope: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    tenant_id: TenantId,
    *,
    start: datetime,
    end: datetime,
    observed_at: datetime,
) -> WebCoreObservation:
    """各自时间窗与当前状态相交；单语句保证多个源处在同一PG快照。"""
    stages: tuple[tuple[ObservationStage, Any, Any, Any, tuple[Any, ...]], ...] = (
        (
            "demand_signal",
            DemandSignalRow,
            DemandSignalRow.signal_id,
            DemandSignalRow.observed_at,
            (),
        ),
        (
            "need_hypothesis",
            NeedHypothesisRow,
            NeedHypothesisRow.hypothesis_id,
            NeedHypothesisRow.created_at,
            (),
        ),
        (
            "validated_need",
            ValidatedNeedRow,
            ValidatedNeedRow.need_id,
            ValidatedNeedRow.created_at,
            (
                ValidatedNeedRow.status.in_(
                    ("validated", "sourcing_ready", "handed_to_sourcing")
                ),
            ),
        ),
        (
            "opportunity_record",
            OpportunityRow,
            OpportunityRow.opportunity_id,
            OpportunityRow.created_at,
            (),
        ),
        (
            "quote_record",
            QuotationRow,
            QuotationRow.quote_id,
            QuotationRow.created_at,
            (),
        ),
        (
            "human_execution",
            HandoffRow,
            HandoffRow.opportunity_id,
            HandoffRow.accepted_at,
            (),
        ),
        (
            "deal_outcome",
            OpportunityRow,
            OpportunityRow.opportunity_id,
            OpportunityRow.closed_at,
            (OpportunityRow.state.in_(("won", "lost")),),
        ),
    )
    counts = [
        select(func.count(func.distinct(identity)))
        .where(
            table.tenant_id == tenant_id,
            timestamp >= start,
            timestamp < end,
            *criteria,
        )
        .scalar_subquery()
        .label(stage)
        for stage, table, identity, timestamp, criteria in stages
    ]
    pending = (HandoffRow.tenant_id == tenant_id, HandoffRow.state == "requested")
    owners = (
        select(
            HandoffRow.assigned_to.label("employee_id"),
            func.count().label("queue_depth"),
        )
        .where(*pending)
        .group_by(HandoffRow.assigned_to)
        .subquery()
    )
    owner_json = (
        select(
            func.jsonb_agg(
                func.jsonb_build_object(
                    "employee_id",
                    owners.c.employee_id,
                    "queue_depth",
                    owners.c.queue_depth,
                )
            )
        )
        .scalar_subquery()
        .label("owners")
    )
    oldest = (
        select(func.min(HandoffRow.requested_at))
        .where(*pending)
        .scalar_subquery()
        .label("oldest")
    )
    invalid = (
        select(func.count())
        .select_from(HandoffRow)
        .where(
            *pending,
            HandoffRow.requested_at > observed_at,
        )
        .scalar_subquery()
        .label("invalid")
    )
    calls = (
        select(
            ToolCallRow.tool_id,
            func.count().filter(ToolCallRow.status != "duplicate").label("call_count"),
            func.coalesce(
                func.sum(
                    case(
                        (ToolCallRow.status != "duplicate", ToolCallRow.attempt_count),
                        else_=0,
                    )
                ),
                0,
            ).label("attempt_count"),
            func.count()
            .filter(ToolCallRow.status == "duplicate")
            .label("duplicate_receipt_count"),
        )
        .where(
            ToolCallRow.tenant_id == tenant_id,
            ToolCallRow.created_at >= start,
            ToolCallRow.created_at < end,
        )
        .group_by(ToolCallRow.tool_id)
        .subquery()
    )
    calls_json = (
        select(
            func.jsonb_agg(
                func.jsonb_build_object(
                    "tool_id",
                    calls.c.tool_id,
                    "call_count",
                    calls.c.call_count,
                    "attempt_count",
                    calls.c.attempt_count,
                    "duplicate_receipt_count",
                    calls.c.duplicate_receipt_count,
                )
            )
        )
        .scalar_subquery()
        .label("calls")
    )
    credits = [
        select(func.count())
        .select_from(SearchQuotaReservationRow)
        .where(
            SearchQuotaReservationRow.tenant_id == tenant_id,
            SearchQuotaReservationRow.provider == "tavily",
            SearchQuotaReservationRow.created_at >= start,
            SearchQuotaReservationRow.created_at < end,
            SearchQuotaReservationRow.status == status,
        )
        .scalar_subquery()
        .label(status)
        for status in ("consumed", "reserved", "uncertain")
    ]
    async with session_scope() as session:
        row = (
            await session.execute(
                select(*counts, owner_json, oldest, invalid, calls_json, *credits)
            )
        ).one()
    owner_values = tuple(
        sorted(
            (HandoffOwnerObservation(**item) for item in (row.owners or [])),
            key=lambda item: item.employee_id or "",
        )
    )
    stage_values = [
        StageObservation(
            stage=stage,
            count=getattr(row, stage),
            source=table.__tablename__,
            time_field=timestamp.key,
        )
        for stage, table, _, timestamp, _ in stages
    ]
    stage_values.insert(
        4,
        StageObservation(
            stage="supply_match",
            count=None,
            source="unavailable",
            time_field=None,
            missing_inputs=("supply_match_source_missing",),
        ),
    )
    elapsed = observed_at - row.oldest if row.oldest and not row.invalid else None
    return WebCoreObservation(
        window_start=start,
        window_end=end,
        observed_at=observed_at,
        stages=tuple(stage_values),
        source_calls=tuple(
            sorted(
                (SourceCallObservation(**item) for item in row.calls or []),
                key=lambda item: item.tool_id,
            )
        ),
        consumed_credits=row.consumed,
        reserved_credits=row.reserved,
        uncertain_credits=row.uncertain,
        handoffs=HandoffObservation(
            queue_depth=sum(item.queue_depth for item in owner_values),
            oldest_wait_seconds=elapsed.days * 86400 + elapsed.seconds
            if elapsed is not None
            else None,
            invalid_time_count=row.invalid,
            by_employee=owner_values,
        ),
    )
