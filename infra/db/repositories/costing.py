"""成本表与利润规则 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from domains.costing.errors import LockedCostSheetError
from domains.costing.models import (
    CostItem,
    CostItemType,
    CostSheet,
    CostSheetVersion,
    MarginRule,
    RiskAcceptance,
)
from domains.costing.repository import CostSheetRepository, MarginRuleRepository
from infra.db.tables import (
    CostItemRow,
    CostSheetFxRateRow,
    CostSheetRow,
    MarginRuleRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
    new_id,
)
from shared.schemas.money import CurrencyCode, FxRate, Money

_tenant_logger = logging.getLogger("security.tenant_isolation")


class _TenantBoundRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")


def _sheet_values(sheet: CostSheet) -> dict[str, object]:
    risk = sheet.risk_acceptance
    return {
        "tenant_id": str(sheet.tenant_id),
        "cost_sheet_id": str(sheet.cost_sheet_id),
        "opportunity_id": str(sheet.opportunity_id),
        "version_type": sheet.version_type.value,
        "version_number": sheet.version_number,
        "quantity": sheet.quantity,
        "base_currency": sheet.base_currency,
        "quote_currency": sheet.quote_currency,
        "fx_snapshot_id": (
            str(sheet.fx_snapshot_id) if sheet.fx_snapshot_id is not None else None
        ),
        "created_by": str(sheet.created_by) if sheet.created_by is not None else None,
        "created_at": sheet.created_at,
        "locked_at": sheet.locked_at,
        "risk_accepted_by": (
            str(risk.accepted_by) if risk is not None else None
        ),
        "risk_accepted_at": risk.accepted_at if risk is not None else None,
        "risk_justification": risk.justification if risk is not None else None,
    }


def _item_row(sheet: CostSheet, item: CostItem, sequence: int) -> CostItemRow:
    return CostItemRow(
        tenant_id=str(sheet.tenant_id),
        cost_sheet_id=str(sheet.cost_sheet_id),
        item_sequence=sequence,
        item_type=item.item_type.value,
        amount=item.amount.amount,
        currency=str(item.amount.currency),
        price_basis=item.price_basis,
        is_per_unit=item.is_per_unit,
        note=item.note,
        source_ref=item.source_ref,
        entered_by=str(item.entered_by) if item.entered_by is not None else None,
    )


def _rate_row(sheet: CostSheet, rate: FxRate) -> CostSheetFxRateRow:
    return CostSheetFxRateRow(
        tenant_id=str(sheet.tenant_id),
        cost_sheet_id=str(sheet.cost_sheet_id),
        base_currency=str(rate.base),
        quote_currency=str(rate.quote),
        rate=rate.rate,
        observed_at=rate.observed_at,
        source=rate.source,
    )


def _to_sheet(
    row: CostSheetRow,
    item_rows: list[CostItemRow],
    rate_rows: list[CostSheetFxRateRow],
) -> CostSheet:
    risk = None
    if row.risk_accepted_by is not None:
        if row.risk_accepted_at is None or row.risk_justification is None:
            raise RuntimeError("成本表风险接受记录不完整")
        risk = RiskAcceptance(
            accepted_by=EmployeeId(row.risk_accepted_by),
            accepted_at=row.risk_accepted_at,
            justification=row.risk_justification,
        )
    return CostSheet(
        cost_sheet_id=CostSheetId(row.cost_sheet_id),
        tenant_id=TenantId(row.tenant_id),
        opportunity_id=OpportunityId(row.opportunity_id),
        version_type=CostSheetVersion(row.version_type),
        version_number=row.version_number,
        quantity=row.quantity,
        base_currency=row.base_currency,
        quote_currency=row.quote_currency,
        created_at=row.created_at,
        items=[
            CostItem(
                item_type=CostItemType(item.item_type),
                amount=Money(
                    Decimal(item.amount), CurrencyCode(item.currency)
                ),
                price_basis=item.price_basis,
                is_per_unit=item.is_per_unit,
                item_sequence=item.item_sequence,
                note=item.note,
                source_ref=item.source_ref,
                entered_by=(
                    EmployeeId(item.entered_by)
                    if item.entered_by is not None
                    else None
                ),
            )
            for item in item_rows
        ],
        fx_snapshot_id=(
            FxSnapshotId(row.fx_snapshot_id)
            if row.fx_snapshot_id is not None
            else None
        ),
        fx_rates=tuple(
            FxRate(
                base=CurrencyCode(rate.base_currency),
                quote=CurrencyCode(rate.quote_currency),
                rate=Decimal(rate.rate),
                observed_at=rate.observed_at,
                source=rate.source,
            )
            for rate in rate_rows
        ),
        created_by=(
            EmployeeId(row.created_by) if row.created_by is not None else None
        ),
        locked_at=row.locked_at,
        risk_acceptance=risk,
    )


class CostSheetRepositoryImpl(_TenantBoundRepository, CostSheetRepository):
    """成本表读写；服务层与数据库触发器共同保护锁定版本。"""

    async def mark_locked_once(self, tenant_id: TenantId, cost_sheet_id: CostSheetId, at: datetime) -> None:
        """冻结调用者已持FOR UPDATE；仅首次写锁定时刻，复用不改旧表。"""
        self._require_tenant(tenant_id,"cost_sheet_lock_once")
        await self._session.execute(update(CostSheetRow).where(
            CostSheetRow.tenant_id==tenant_id,CostSheetRow.cost_sheet_id==cost_sheet_id,
            CostSheetRow.locked_at.is_(None)).values(locked_at=at))

    async def add(self, sheet: CostSheet) -> None:
        self._require_tenant(sheet.tenant_id, "cost_sheet_add")
        if sheet.locked_at is not None:
            raise LockedCostSheetError("不能直接创建已锁定成本表")
        self._session.add(CostSheetRow(**_sheet_values(sheet)))
        await self._session.flush()
        self._add_children(sheet)
        await self._session.flush()

    async def get(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> CostSheet | None:
        return await self._get(tenant_id, cost_sheet_id, for_update=False)

    async def get_for_update(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> CostSheet | None:
        return await self._get(tenant_id, cost_sheet_id, for_update=True)

    async def _get(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        *,
        for_update: bool,
    ) -> CostSheet | None:
        self._require_tenant(tenant_id, "cost_sheet_get")
        statement = select(CostSheetRow).where(
            CostSheetRow.tenant_id == str(self._tenant_id),
            CostSheetRow.cost_sheet_id == str(cost_sheet_id),
        )
        if for_update:
            statement = statement.with_for_update()
        row = (
            await self._session.execute(statement)
        ).scalar_one_or_none()
        if row is None:
            return None
        return await self._hydrate(row)

    async def update(self, sheet: CostSheet) -> None:
        self._require_tenant(sheet.tenant_id, "cost_sheet_update")
        current = (
            await self._session.execute(
                select(CostSheetRow)
                .where(
                    CostSheetRow.tenant_id == str(self._tenant_id),
                    CostSheetRow.cost_sheet_id == str(sheet.cost_sheet_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise ValidationError("成本表不存在")
        if current.locked_at is not None:
            raise LockedCostSheetError("已锁定成本表不可修改")

        await self._session.execute(
            delete(CostItemRow).where(
                CostItemRow.tenant_id == str(self._tenant_id),
                CostItemRow.cost_sheet_id == str(sheet.cost_sheet_id),
            )
        )
        await self._session.execute(
            delete(CostSheetFxRateRow).where(
                CostSheetFxRateRow.tenant_id == str(self._tenant_id),
                CostSheetFxRateRow.cost_sheet_id == str(sheet.cost_sheet_id),
            )
        )
        values = _sheet_values(sheet)
        for field_name in (
            "tenant_id",
            "cost_sheet_id",
            "opportunity_id",
            "version_type",
            "version_number",
            "created_at",
        ):
            values.pop(field_name)
        for field_name, value in values.items():
            setattr(current, field_name, value)
        self._add_children(sheet)
        await self._session.flush()

    async def next_version_number(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        version_type: str,
    ) -> int:
        self._require_tenant(tenant_id, "cost_sheet_next_version")
        try:
            CostSheetVersion(version_type)
        except ValueError as exc:
            raise ValidationError("成本表版本类型无效") from exc
        lock_key = (
            f"cost-sheet-version:{self._tenant_id}:{opportunity_id}:{version_type}"
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": lock_key},
        )
        value = await self._session.scalar(
            select(func.coalesce(func.max(CostSheetRow.version_number), 0) + 1).where(
                CostSheetRow.tenant_id == str(self._tenant_id),
                CostSheetRow.opportunity_id == str(opportunity_id),
                CostSheetRow.version_type == version_type,
            )
        )
        if not isinstance(value, int):
            raise TypeError("成本表版本号分配结果类型无效")
        return value

    async def list_by_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[CostSheet]:
        self._require_tenant(tenant_id, "cost_sheet_list_opportunity")
        rows = (
            (
                await self._session.execute(
                    select(CostSheetRow)
                    .where(
                        CostSheetRow.tenant_id == str(self._tenant_id),
                        CostSheetRow.opportunity_id == str(opportunity_id),
                    )
                    .order_by(
                        CostSheetRow.version_type,
                        CostSheetRow.version_number,
                        CostSheetRow.cost_sheet_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [await self._hydrate(row) for row in rows]

    def _add_children(self, sheet: CostSheet) -> None:
        next_sequence = max((item.item_sequence or 0 for item in sheet.items), default=0)
        items: list[CostItem] = []
        for item in sheet.items:
            if item.item_sequence is None:
                next_sequence += 1
                item = replace(item, item_sequence=next_sequence)
            items.append(item)
        if len({item.item_sequence for item in items}) != len(items):
            raise ValidationError("成本明细序号重复")
        sheet.items = items
        self._session.add_all(
            [
                _item_row(sheet, item, item.item_sequence)
                for item in sheet.items
            ]
        )
        self._session.add_all([_rate_row(sheet, rate) for rate in sheet.fx_rates])

    async def _hydrate(self, row: CostSheetRow) -> CostSheet:
        item_rows = list(
            (
                await self._session.execute(
                    select(CostItemRow)
                    .where(
                        CostItemRow.tenant_id == str(self._tenant_id),
                        CostItemRow.cost_sheet_id == row.cost_sheet_id,
                    )
                    .order_by(CostItemRow.item_sequence)
                )
            )
            .scalars()
            .all()
        )
        rate_rows = list(
            (
                await self._session.execute(
                    select(CostSheetFxRateRow)
                    .where(
                        CostSheetFxRateRow.tenant_id == str(self._tenant_id),
                        CostSheetFxRateRow.cost_sheet_id == row.cost_sheet_id,
                    )
                    .order_by(CostSheetFxRateRow.base_currency)
                )
            )
            .scalars()
            .all()
        )
        return _to_sheet(row, item_rows, rate_rows)


class MarginRuleRepositoryImpl(_TenantBoundRepository, MarginRuleRepository):
    """利润规则只读取显式配置，不提供任何隐含默认值。"""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(session, tenant_id)
        self._now = now or (lambda: datetime.now(UTC))

    async def get_effective(
        self, tenant_id: TenantId, category: str | None
    ) -> MarginRule:
        self._require_tenant(tenant_id, "margin_rule_get_effective")
        category_filter = (
            MarginRuleRow.category.is_(None)
            if category is None
            else or_(
                MarginRuleRow.category == category,
                MarginRuleRow.category.is_(None),
            )
        )
        category_priority = (
            case((MarginRuleRow.category == category, 0), else_=1)
            if category is not None
            else case((MarginRuleRow.category.is_(None), 0), else_=1)
        )
        row = (
            await self._session.execute(
                select(MarginRuleRow)
                .where(
                    MarginRuleRow.tenant_id == str(self._tenant_id),
                    category_filter,
                    MarginRuleRow.effective_from <= self._now(),
                )
                .order_by(
                    category_priority,
                    MarginRuleRow.effective_from.desc(),
                    MarginRuleRow.margin_rule_id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValidationError("利润规则未配置")
        return MarginRule(
            tenant_id=TenantId(row.tenant_id),
            category=row.category,
            minimum_margin_rate=Decimal(row.minimum_margin_rate),
            target_margin_rate=Decimal(row.target_margin_rate),
            effective_from=row.effective_from,
        )

    async def set_rule(self, rule: MarginRule) -> None:
        self._require_tenant(rule.tenant_id, "margin_rule_set")
        self._session.add(
            MarginRuleRow(
                tenant_id=str(rule.tenant_id),
                margin_rule_id=new_id("mr"),
                category=rule.category,
                minimum_margin_rate=rule.minimum_margin_rate,
                target_margin_rate=rule.target_margin_rate,
                effective_from=rule.effective_from,
            )
        )
        await self._session.flush()


__all__ = ("CostSheetRepositoryImpl", "MarginRuleRepositoryImpl")
