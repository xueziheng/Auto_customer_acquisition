"""老板指令域的 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.directives.models import (
    Directive,
    DirectiveContent,
    DirectiveObjective,
    DirectiveProposal,
    DiscoveryConfig,
    HandoffRules,
    MarketAssignment,
    OutreachBounds,
    ProposalState,
)
from domains.directives.repository import DirectiveRepository, ProposalRepository
from infra.db.tables import BossDirectiveRow, DirectiveProposalRow, DirectiveVersionRow
from shared.errors import (
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import DirectiveId, EmployeeId, TenantId

_logger = logging.getLogger("infra.db.repositories.directives")


class _TenantBound:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")


def _content_to_json(content: DirectiveContent) -> dict[str, object]:
    return {
        "objective": content.objective.value,
        "market_assignments": [
            {"country": item.country, "owner": str(item.owner)}
            for item in content.market_assignments
        ],
        "discovery": (
            None
            if content.discovery is None
            else {
                "need_first_ratio": content.discovery.need_first_ratio,
                "catalog_assisted_ratio": content.discovery.catalog_assisted_ratio,
                "focus_categories": list(content.discovery.focus_categories),
                "excluded_buyer_types": list(
                    content.discovery.excluded_buyer_types
                ),
            }
        ),
        "outreach": (
            None
            if content.outreach is None
            else {
                "primary_channel": content.outreach.primary_channel,
                "max_sequence_messages": content.outreach.max_sequence_messages,
                "stop_on_reply": content.outreach.stop_on_reply,
            }
        ),
        "handoff": (
            None
            if content.handoff is None
            else {
                "manager": str(content.handoff.manager),
                "triggers": list(content.handoff.triggers),
            }
        ),
        "paused_markets": list(content.paused_markets),
        "monthly_budget_credits": content.monthly_budget_credits,
        "notes": content.notes,
    }


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"指令持久化字段 {field} 无效")
    return cast(Mapping[str, object], value)


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"指令持久化字段 {field} 无效")
    return cast(list[str], value)


def _content_from_json(value: object) -> DirectiveContent:
    data = _mapping(value, "content")
    expected = {
        "objective",
        "market_assignments",
        "discovery",
        "outreach",
        "handoff",
        "paused_markets",
        "monthly_budget_credits",
        "notes",
    }
    if set(data) != expected:
        raise ValidationError("指令持久化字段集合无效")
    assignments_raw = data["market_assignments"]
    if not isinstance(assignments_raw, list):
        raise ValidationError("指令市场分配持久化字段无效")
    assignments: list[MarketAssignment] = []
    for value_item in assignments_raw:
        item = _mapping(value_item, "market_assignments")
        if set(item) != {"country", "owner"}:
            raise ValidationError("指令市场分配持久化字段无效")
        country = item["country"]
        owner = item["owner"]
        if not isinstance(country, str) or not isinstance(owner, str):
            raise ValidationError("指令市场分配持久化字段无效")
        assignments.append(MarketAssignment(country, EmployeeId(owner)))

    discovery_raw = data["discovery"]
    discovery = None
    if discovery_raw is not None:
        item = _mapping(discovery_raw, "discovery")
        if set(item) != {
            "need_first_ratio",
            "catalog_assisted_ratio",
            "focus_categories",
            "excluded_buyer_types",
        }:
            raise ValidationError("指令探索配置持久化字段无效")
        need_ratio = item["need_first_ratio"]
        catalog_ratio = item["catalog_assisted_ratio"]
        if type(need_ratio) is not int or type(catalog_ratio) is not int:
            raise ValidationError("指令探索配比持久化字段无效")
        discovery = DiscoveryConfig(
            need_first_ratio=need_ratio,
            catalog_assisted_ratio=catalog_ratio,
            focus_categories=_string_list(item["focus_categories"], "focus_categories"),
            excluded_buyer_types=_string_list(
                item["excluded_buyer_types"], "excluded_buyer_types"
            ),
        )

    outreach_raw = data["outreach"]
    outreach = None
    if outreach_raw is not None:
        item = _mapping(outreach_raw, "outreach")
        if set(item) != {
            "primary_channel",
            "max_sequence_messages",
            "stop_on_reply",
        }:
            raise ValidationError("指令触达配置持久化字段无效")
        primary_channel = item["primary_channel"]
        max_messages = item["max_sequence_messages"]
        stop_on_reply = item["stop_on_reply"]
        if (
            not isinstance(primary_channel, str)
            or type(max_messages) is not int
            or type(stop_on_reply) is not bool
        ):
            raise ValidationError("指令触达配置持久化字段无效")
        outreach = OutreachBounds(primary_channel, max_messages, stop_on_reply)

    handoff_raw = data["handoff"]
    handoff = None
    if handoff_raw is not None:
        item = _mapping(handoff_raw, "handoff")
        if set(item) != {"manager", "triggers"} or not isinstance(
            item["manager"], str
        ):
            raise ValidationError("指令接管配置持久化字段无效")
        handoff = HandoffRules(
            manager=EmployeeId(item["manager"]),
            triggers=_string_list(item["triggers"], "handoff.triggers"),
        )

    budget = data["monthly_budget_credits"]
    notes = data["notes"]
    if budget is not None and type(budget) is not int:
        raise ValidationError("指令预算持久化字段无效")
    if notes is not None and not isinstance(notes, str):
        raise ValidationError("指令备注持久化字段无效")
    objective = data["objective"]
    if not isinstance(objective, str):
        raise ValidationError("指令目标持久化字段无效")
    try:
        return DirectiveContent(
            objective=DirectiveObjective(objective),
            market_assignments=assignments,
            discovery=discovery,
            outreach=outreach,
            handoff=handoff,
            paused_markets=_string_list(data["paused_markets"], "paused_markets"),
            monthly_budget_credits=budget,
            notes=notes,
        )
    except ValueError as exc:
        raise ValidationError("指令目标持久化字段无效") from exc


def _row_to_proposal(row: DirectiveProposalRow) -> DirectiveProposal:
    try:
        state = ProposalState(row.state)
    except ValueError as exc:
        raise ValidationError("指令提案状态持久化字段无效") from exc
    changes = _string_list(row.expected_behavior_changes, "expected_behavior_changes")
    return DirectiveProposal(
        proposal_id=row.proposal_id,
        tenant_id=TenantId(row.tenant_id),
        raw_text=row.raw_text,
        parsed=_content_from_json(row.parsed_content),
        interpretation_summary=row.interpretation_summary,
        expected_behavior_changes=changes,
        parsed_by=row.parsed_by,
        created_at=row.created_at,
        state=state,
        decided_at=row.decided_at,
        decided_by=EmployeeId(row.decided_by) if row.decided_by is not None else None,
    )


def _row_to_directive(row: DirectiveVersionRow) -> Directive:
    return Directive(
        directive_id=DirectiveId(row.directive_id),
        tenant_id=TenantId(row.tenant_id),
        version=row.version,
        content=_content_from_json(row.content),
        source_proposal_id=row.source_proposal_id,
        activated_at=row.activated_at,
        activated_by=EmployeeId(row.activated_by),
        superseded_at=row.superseded_at,
        rollback_of=row.rollback_of,
    )


class ProposalRepositoryImpl(_TenantBound, ProposalRepository):
    async def add(self, proposal: DirectiveProposal) -> None:
        self._require_tenant(proposal.tenant_id, "directive_proposal_add")
        self._session.add(
            DirectiveProposalRow(
                tenant_id=str(proposal.tenant_id),
                proposal_id=proposal.proposal_id,
                raw_text=proposal.raw_text,
                parsed_content=_content_to_json(proposal.parsed),
                interpretation_summary=proposal.interpretation_summary,
                expected_behavior_changes=list(proposal.expected_behavior_changes),
                parsed_by=proposal.parsed_by,
                state=proposal.state.value,
                created_at=proposal.created_at,
                decided_at=proposal.decided_at,
                decided_by=(
                    str(proposal.decided_by)
                    if proposal.decided_by is not None
                    else None
                ),
            )
        )
        await self._session.flush()

    async def get(
        self, tenant_id: TenantId, proposal_id: str
    ) -> DirectiveProposal | None:
        return await self._get(tenant_id, proposal_id, for_update=False)

    async def get_for_update(
        self, tenant_id: TenantId, proposal_id: str
    ) -> DirectiveProposal | None:
        return await self._get(tenant_id, proposal_id, for_update=True)

    async def _get(
        self, tenant_id: TenantId, proposal_id: str, *, for_update: bool
    ) -> DirectiveProposal | None:
        self._require_tenant(tenant_id, "directive_proposal_get")
        statement = select(DirectiveProposalRow).where(
            DirectiveProposalRow.tenant_id == str(self._tenant_id),
            DirectiveProposalRow.proposal_id == proposal_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _row_to_proposal(row) if row is not None else None

    async def update(self, proposal: DirectiveProposal) -> None:
        self._require_tenant(proposal.tenant_id, "directive_proposal_update")
        result = await self._session.execute(
            sa_update(DirectiveProposalRow)
            .where(
                DirectiveProposalRow.tenant_id == str(self._tenant_id),
                DirectiveProposalRow.proposal_id == proposal.proposal_id,
                DirectiveProposalRow.state == ProposalState.PENDING_CONFIRMATION.value,
            )
            .values(
                state=proposal.state.value,
                decided_at=proposal.decided_at,
                decided_by=(
                    str(proposal.decided_by)
                    if proposal.decided_by is not None
                    else None
                ),
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise InvalidStateTransition("指令提案状态已变化")

    async def list_pending(self, tenant_id: TenantId) -> list[DirectiveProposal]:
        self._require_tenant(tenant_id, "directive_proposal_list_pending")
        rows = (
            await self._session.execute(
                select(DirectiveProposalRow)
                .where(
                    DirectiveProposalRow.tenant_id == str(self._tenant_id),
                    DirectiveProposalRow.state
                    == ProposalState.PENDING_CONFIRMATION.value,
                )
                .order_by(
                    DirectiveProposalRow.created_at, DirectiveProposalRow.proposal_id
                )
            )
        ).scalars().all()
        return [_row_to_proposal(row) for row in rows]

    async def list_rejected(
        self, tenant_id: TenantId, limit: int
    ) -> list[DirectiveProposal]:
        self._require_tenant(tenant_id, "directive_proposal_list_rejected")
        rows = (
            await self._session.execute(
                select(DirectiveProposalRow)
                .where(
                    DirectiveProposalRow.tenant_id == str(self._tenant_id),
                    DirectiveProposalRow.state == ProposalState.REJECTED.value,
                )
                .order_by(
                    DirectiveProposalRow.decided_at.desc(),
                    DirectiveProposalRow.proposal_id,
                )
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_proposal(row) for row in rows]


class DirectiveRepositoryImpl(_TenantBound, DirectiveRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(session, tenant_id)
        self._now = now or (lambda: datetime.now(UTC))

    async def add(self, directive: Directive) -> None:
        self._require_tenant(directive.tenant_id, "directive_add")
        self._session.add(
            DirectiveVersionRow(
                tenant_id=str(directive.tenant_id),
                directive_id=str(directive.directive_id),
                version=directive.version,
                content=_content_to_json(directive.content),
                source_proposal_id=directive.source_proposal_id,
                activated_at=directive.activated_at,
                activated_by=str(directive.activated_by),
                superseded_at=directive.superseded_at,
                rollback_of=directive.rollback_of,
            )
        )
        await self._session.flush()

    async def get_active(self, tenant_id: TenantId) -> Directive | None:
        return await self._get_active(tenant_id, for_update=False)

    async def get_active_for_update(
        self, tenant_id: TenantId
    ) -> Directive | None:
        return await self._get_active(tenant_id, for_update=True)

    async def _get_active(
        self, tenant_id: TenantId, *, for_update: bool
    ) -> Directive | None:
        self._require_tenant(tenant_id, "directive_get_active")
        statement = (
            select(DirectiveVersionRow)
            .join(
                BossDirectiveRow,
                (BossDirectiveRow.tenant_id == DirectiveVersionRow.tenant_id)
                & (BossDirectiveRow.directive_id == DirectiveVersionRow.directive_id)
                & (BossDirectiveRow.version == DirectiveVersionRow.version),
            )
            .where(BossDirectiveRow.tenant_id == str(self._tenant_id))
        )
        if for_update:
            statement = statement.with_for_update(of=BossDirectiveRow)
        row = (await self._session.execute(statement)).scalar_one_or_none()
        return _row_to_directive(row) if row is not None else None

    async def get_version(
        self, tenant_id: TenantId, version: int
    ) -> Directive | None:
        self._require_tenant(tenant_id, "directive_get_version")
        row = (
            await self._session.execute(
                select(DirectiveVersionRow).where(
                    DirectiveVersionRow.tenant_id == str(self._tenant_id),
                    DirectiveVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
        return _row_to_directive(row) if row is not None else None

    async def next_version(self, tenant_id: TenantId) -> int:
        self._require_tenant(tenant_id, "directive_next_version")
        await self._session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"directive-version:{self._tenant_id}", 0
                    )
                )
            )
        )
        current = await self._session.scalar(
            select(func.max(DirectiveVersionRow.version)).where(
                DirectiveVersionRow.tenant_id == str(self._tenant_id)
            )
        )
        return 1 if current is None else int(current) + 1

    async def mark_superseded(
        self, tenant_id: TenantId, directive_id: DirectiveId
    ) -> None:
        self._require_tenant(tenant_id, "directive_mark_superseded")
        result = await self._session.execute(
            sa_update(DirectiveVersionRow)
            .where(
                DirectiveVersionRow.tenant_id == str(self._tenant_id),
                DirectiveVersionRow.directive_id == str(directive_id),
                DirectiveVersionRow.superseded_at.is_(None),
            )
            .values(superseded_at=self._now())
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise InvalidStateTransition("当前生效指令已变化")

    async def set_active(self, directive: Directive) -> None:
        self._require_tenant(directive.tenant_id, "directive_set_active")
        await self._session.execute(
            pg_insert(BossDirectiveRow)
            .values(
                tenant_id=str(self._tenant_id),
                directive_id=str(directive.directive_id),
                version=directive.version,
                activated_at=directive.activated_at,
            )
            .on_conflict_do_update(
                index_elements=(BossDirectiveRow.tenant_id,),
                set_={
                    "directive_id": str(directive.directive_id),
                    "version": directive.version,
                    "activated_at": directive.activated_at,
                },
            )
        )

    async def list_versions(
        self, tenant_id: TenantId, limit: int
    ) -> list[Directive]:
        self._require_tenant(tenant_id, "directive_list_versions")
        rows = (
            await self._session.execute(
                select(DirectiveVersionRow)
                .where(DirectiveVersionRow.tenant_id == str(self._tenant_id))
                .order_by(
                    DirectiveVersionRow.version.desc(),
                    DirectiveVersionRow.directive_id,
                )
                .limit(limit)
            )
        ).scalars().all()
        return [_row_to_directive(row) for row in rows]


__all__ = ("DirectiveRepositoryImpl", "ProposalRepositoryImpl")
