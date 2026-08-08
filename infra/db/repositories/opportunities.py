"""Opportunity / ScoreSnapshot 仓储实现（infra 层）。

域模型 ↔ ORM 行转换在本文件完成；租户过滤由 ``TenantScopedRepository`` 基类
注入（构造时绑定租户，仓储永不查询绑定租户之外的行，硬边界 8）。
金额以 ``Numeric(18,2)`` + ``CHAR(3)`` 成对存取，无 float（硬边界 2）。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import CursorResult, Select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domains.opportunities.models import (
    LossReason,
    Opportunity,
    OpportunityState,
    ScoreSnapshot,
    SortKey,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import OpportunityRow, ScoreSnapshotRow
from shared.errors import InvalidStateTransition
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    ScoreSnapshotId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money


def _amount(money: Money | None) -> Decimal | None:
    return money.amount if money is not None else None


def _currency(money: Money | None) -> str | None:
    return money.currency if money is not None else None


def _money(amount: Decimal | None, currency: str | None) -> Money | None:
    """两列 → Money；数据库 CHECK 保证成对，不成对是数据损坏，明确抛错。"""
    if amount is None and currency is None:
        return None
    if amount is None or currency is None:
        raise ValueError("金额与币种必须成对（数据库 CHECK 约束兜底）")
    return Money(amount, CurrencyCode(currency))


def _opp_to_row(opp: Opportunity) -> OpportunityRow:
    return OpportunityRow(
        opportunity_id=opp.opportunity_id,
        tenant_id=opp.tenant_id,
        account_id=opp.account_id,
        account_name=opp.account_name,
        country=opp.country,
        need_id=opp.need_id,
        product_category=opp.product_category,
        state=opp.state.value,
        created_at=opp.created_at,
        quantity=opp.quantity,
        spec_summary=opp.spec_summary,
        application=opp.application,
        destination=opp.destination,
        required_by=opp.required_by,
        target_price_amount=_amount(opp.target_price),
        target_price_currency=_currency(opp.target_price),
        decision_maker=opp.decision_maker,
        current_supply_solution=opp.current_supply_solution,
        current_supply_problem=opp.current_supply_problem,
        can_source=opp.can_source,
        estimated_cost_amount=_amount(opp.estimated_cost),
        estimated_cost_currency=_currency(opp.estimated_cost),
        estimated_profit_amount=_amount(opp.estimated_profit),
        estimated_profit_currency=_currency(opp.estimated_profit),
        owner=opp.owner,
        next_action=opp.next_action,
        next_action_due=opp.next_action_due,
        loss_reason=opp.loss_reason.value if opp.loss_reason is not None else None,
        died_at_state=opp.died_at_state.value if opp.died_at_state is not None else None,
        closed_by=opp.closed_by,
        closed_at=opp.closed_at,
        assigned_by=opp.assigned_by,
        assigned_at=opp.assigned_at,
    )


def _row_to_opp(row: OpportunityRow) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId(row.opportunity_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        account_name=row.account_name,
        country=row.country,
        need_id=ValidatedNeedId(row.need_id),
        product_category=row.product_category,
        state=OpportunityState(row.state),
        created_at=row.created_at,
        quantity=row.quantity,
        spec_summary=row.spec_summary,
        application=row.application,
        destination=row.destination,
        required_by=row.required_by,
        target_price=_money(row.target_price_amount, row.target_price_currency),
        decision_maker=row.decision_maker,
        current_supply_solution=row.current_supply_solution,
        current_supply_problem=row.current_supply_problem,
        can_source=row.can_source,
        estimated_cost=_money(row.estimated_cost_amount, row.estimated_cost_currency),
        estimated_profit=_money(row.estimated_profit_amount, row.estimated_profit_currency),
        owner=EmployeeId(row.owner) if row.owner is not None else None,
        next_action=row.next_action,
        next_action_due=row.next_action_due,
        loss_reason=LossReason(row.loss_reason) if row.loss_reason is not None else None,
        died_at_state=(
            OpportunityState(row.died_at_state) if row.died_at_state is not None else None
        ),
        closed_by=EmployeeId(row.closed_by) if row.closed_by is not None else None,
        closed_at=row.closed_at,
        assigned_by=EmployeeId(row.assigned_by) if row.assigned_by is not None else None,
        assigned_at=row.assigned_at,
    )


class OpportunityRepositoryImpl(TenantScopedRepository):
    """``OpportunityRepository`` 的 SQLAlchemy 实现。

    仓储按构造时绑定的租户过滤（基类 ``scoped_query`` 注入）。Protocol 方法
    的 ``tenant_id`` 参数与构造租户一致——UoW 按租户装配仓储，调用方不得对
    同一实例跨租户使用。
    """

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _scoped(self) -> Select[tuple[OpportunityRow]]:
        return self.scoped_query(OpportunityRow)

    async def add(self, opportunity: Opportunity) -> None:
        if opportunity.tenant_id != self._tenant_id:
            raise ValueError(
                f"机会租户 {opportunity.tenant_id} 与仓储绑定租户 {self._tenant_id} "
                "不一致：拒绝写入（硬边界 8，仓储按构造租户隔离）"
            )
        self._session.add(_opp_to_row(opportunity))

    async def get(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> Opportunity | None:
        if tenant_id != self._tenant_id:
            return None
        row = (
            await self._session.execute(
                self._scoped().where(OpportunityRow.opportunity_id == opportunity_id)
            )
        ).scalar_one_or_none()
        return _row_to_opp(row) if row is not None else None

    async def update(self, opportunity: Opportunity) -> None:
        """全量覆盖更新；终态（WON/LOST）只能经 close_*_if_state 原子推进。"""
        if opportunity.tenant_id != self._tenant_id:
            raise ValueError(
                f"机会租户 {opportunity.tenant_id} 与仓储绑定租户 {self._tenant_id} "
                "不一致：拒绝写入（硬边界 8，仓储按构造租户隔离）"
            )
        if opportunity.state in (OpportunityState.WON, OpportunityState.LOST):
            raise InvalidStateTransition(
                f"普通 update 不允许写入终态 {opportunity.state.value}；"
                "终态只能经 close_won_if_state / close_lost_if_state 原子推进"
            )
        row = _opp_to_row(opportunity)
        values = {
            col.name: getattr(row, col.name)
            for col in OpportunityRow.__table__.columns
            if col.name not in ("opportunity_id", "tenant_id")
        }
        await self._session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == self._tenant_id,
                OpportunityRow.opportunity_id == opportunity.opportunity_id,
            )
            .values(**values)
        )

    async def find_by_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> Opportunity | None:
        if tenant_id != self._tenant_id:
            return None
        row = (
            await self._session.execute(
                self._scoped().where(OpportunityRow.need_id == need_id)
            )
        ).scalar_one_or_none()
        return _row_to_opp(row) if row is not None else None

    async def list_by_owner(
        self,
        tenant_id: TenantId,
        owner: EmployeeId,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]:
        if tenant_id != self._tenant_id:
            return []
        query = self._scoped().where(OpportunityRow.owner == owner)
        if states is not None:
            query = query.where(OpportunityRow.state.in_([s.value for s in states]))
        rows = (await self._session.execute(query.limit(limit))).scalars().all()
        return [_row_to_opp(row) for row in rows]

    async def list_by_state(
        self, tenant_id: TenantId, state: OpportunityState, limit: int
    ) -> list[Opportunity]:
        if tenant_id != self._tenant_id:
            return []
        query = self._scoped().where(OpportunityRow.state == state.value).limit(limit)
        rows = (await self._session.execute(query)).scalars().all()
        return [_row_to_opp(row) for row in rows]

    async def advance_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        expected: OpportunityState,
        target: OpportunityState,
    ) -> bool:
        """原子推进：``UPDATE ... WHERE state = expected``；返回是否成功（并发已变则 False）。"""
        if tenant_id != self._tenant_id:
            return False
        result = await self._session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == self._tenant_id,
                OpportunityRow.opportunity_id == opportunity_id,
                OpportunityRow.state == expected.value,
            )
            .values(state=target.value)
        )
        return cast(CursorResult, result).rowcount > 0

    async def close_lost_if_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        expected: OpportunityState,
        reason: LossReason,
        detail: str | None,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> bool:
        """原子终结为 lost：``WHERE state = expected``，写 loss_reason / died_at_state /
        closed_by / closed_at；``died_at_state`` 记录转入 lost 之前的预期状态。"""
        if tenant_id != self._tenant_id:
            return False
        result = await self._session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == self._tenant_id,
                OpportunityRow.opportunity_id == opportunity_id,
                OpportunityRow.state == expected.value,
            )
            .values(
                state=OpportunityState.LOST.value,
                loss_reason=reason.value,
                died_at_state=expected.value,
                closed_by=actor,
                closed_at=confirmed_at,
            )
        )
        return cast(CursorResult, result).rowcount > 0

    async def close_won_if_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> bool:
        """原子终结为 won：仅 ``WHERE state = 'negotiating'``，写 closed_by/closed_at。"""
        if tenant_id != self._tenant_id:
            return False
        result = await self._session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == self._tenant_id,
                OpportunityRow.opportunity_id == opportunity_id,
                OpportunityRow.state == OpportunityState.NEGOTIATING.value,
            )
            .values(
                state=OpportunityState.WON.value,
                closed_by=actor,
                closed_at=confirmed_at,
            )
        )
        return cast(CursorResult, result).rowcount > 0

    async def assign_owner(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        owner: EmployeeId,
        assigned_by: EmployeeId,
        assigned_at: datetime,
    ) -> bool:
        """分配负责人并记录 assigner 与时间（审计）。"""
        if tenant_id != self._tenant_id:
            return False
        result = await self._session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == self._tenant_id,
                OpportunityRow.opportunity_id == opportunity_id,
            )
            .values(
                owner=owner,
                assigned_by=assigned_by,
                assigned_at=assigned_at,
            )
        )
        return cast(CursorResult, result).rowcount > 0


def _snap_to_row(snapshot: ScoreSnapshot) -> ScoreSnapshotRow:
    return ScoreSnapshotRow(
        snapshot_id=snapshot.snapshot_id,
        tenant_id=snapshot.tenant_id,
        opportunity_id=snapshot.opportunity_id,
        scored_at=snapshot.scored_at,
        scorer_version=snapshot.scorer_version,
        passed_gates=snapshot.passed_gates,
        failed_gates=snapshot.failed_gates,
        evidence_tier=snapshot.evidence_tier.value if snapshot.evidence_tier is not None else None,
        estimated_value_amount=_amount(snapshot.estimated_value),
        estimated_value_currency=_currency(snapshot.estimated_value),
        supply_available=snapshot.supply_available,
        sort_evidence_rank=snapshot.sort_key.evidence_rank,
        sort_value_band=snapshot.sort_key.value_band,
        sort_supply_rank=snapshot.sort_key.supply_rank,
        gate_reasons=snapshot.gate_reasons,
        rank_bucket=snapshot.rank_bucket,
    )


def _row_to_snap(row: ScoreSnapshotRow) -> ScoreSnapshot:
    return ScoreSnapshot(
        tenant_id=TenantId(row.tenant_id),
        snapshot_id=ScoreSnapshotId(row.snapshot_id),
        opportunity_id=OpportunityId(row.opportunity_id),
        scored_at=row.scored_at,
        scorer_version=row.scorer_version,
        passed_gates=row.passed_gates,
        failed_gates=row.failed_gates,
        evidence_tier=(
            ConfidenceTier(row.evidence_tier) if row.evidence_tier is not None else None
        ),
        estimated_value=_money(row.estimated_value_amount, row.estimated_value_currency),
        supply_available=row.supply_available,
        sort_key=SortKey(row.sort_evidence_rank, row.sort_value_band, row.sort_supply_rank),
        rank_bucket=row.rank_bucket,
        gate_reasons=row.gate_reasons,
    )


class ScoreSnapshotRepositoryImpl(TenantScopedRepository):
    """``ScoreSnapshotRepository`` 的 SQLAlchemy 实现（只追加，不更新）。

    数据库触发器拒绝 UPDATE/DELETE（只增）；本实现也不提供任何改写入口。
    """

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _scoped(self) -> Select[tuple[ScoreSnapshotRow]]:
        return self.scoped_query(ScoreSnapshotRow)

    async def add(self, tenant_id: TenantId, snapshot: ScoreSnapshot) -> None:
        if tenant_id != self._tenant_id or snapshot.tenant_id != self._tenant_id:
            raise ValueError(
                f"快照三方租户不一致：bound={self._tenant_id}, method={tenant_id}, "
                f"snapshot={snapshot.tenant_id}；add 要求三者一致（硬边界 8）"
            )
        self._session.add(_snap_to_row(snapshot))

    async def latest_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> ScoreSnapshot | None:
        if tenant_id != self._tenant_id:
            return None
        row = (
            await self._session.execute(
                self._scoped()
                .where(ScoreSnapshotRow.opportunity_id == opportunity_id)
                .order_by(ScoreSnapshotRow.scored_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _row_to_snap(row) if row is not None else None

    async def list_for_backtest(
        self, tenant_id: TenantId, since_days: int
    ) -> list[ScoreSnapshot]:
        """导出快照供权重回测：``scored_at >= now() - since_days``。"""
        if tenant_id != self._tenant_id:
            return []
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        rows = (
            await self._session.execute(
                self._scoped()
                .where(ScoreSnapshotRow.scored_at >= cutoff)
                .order_by(ScoreSnapshotRow.scored_at.asc())
            )
        ).scalars().all()
        return [_row_to_snap(row) for row in rows]
