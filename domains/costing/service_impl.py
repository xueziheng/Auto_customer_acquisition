"""人工成本表的授权、版本化与公共视图实现。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from domains.costing.errors import (
    CostSheetNotFoundError,
    EmptyCostSheetError,
    MissingFxSnapshotError,
)
from domains.costing.models import CostItem, CostItemType, CostSheet, CostSheetVersion
from domains.costing.permissions import (
    CostingAction,
    CostingActor,
    CostingAuthorizer,
)
from domains.costing.repository import CostingUnitOfWorkFactory
from domains.costing.schemas import (
    CostItemCreate,
    CostItemView,
    CostSheetCreate,
    CostSheetView,
    FxRateView,
    QuoteReadiness,
)
from domains.costing.service import (
    assess_quote_readiness,
    compute_unit_full_cost,
    cost_sheet_content_hash,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
    new_id,
)
from shared.schemas.money import CurrencyCode, FxRate, Money

_ITEM_LABELS = {
    CostItemType.PRODUCT_PURCHASE: "产品采购",
    CostItemType.SAMPLE_FEE: "样品",
    CostItemType.MOLD_FEE: "模具",
    CostItemType.CUSTOMIZATION_FEE: "定制",
    CostItemType.LOGO_PRINTING: "Logo 印刷",
    CostItemType.PACKAGING: "包装",
    CostItemType.QUALITY_INSPECTION: "质检",
    CostItemType.WASTAGE: "损耗",
    CostItemType.DOMESTIC_FREIGHT: "国内运输",
    CostItemType.INTERNATIONAL_FREIGHT: "国际运输",
    CostItemType.INSURANCE: "保险",
    CostItemType.CUSTOMS_CLEARANCE: "报关",
    CostItemType.DUTIES_AND_TAXES: "关税与税费",
    CostItemType.DESTINATION_FREIGHT: "目的地运输",
    CostItemType.WAREHOUSING: "仓储",
    CostItemType.PAYMENT_FEES: "支付手续费",
    CostItemType.SALES_COMMISSION: "销售佣金",
    CostItemType.CUSTOMER_ACQUISITION: "获客",
    CostItemType.CONTACT_DATA_COST: "联系人数据",
    CostItemType.AD_ALLOCATION: "广告分摊",
    CostItemType.AGENT_API_ALLOCATION: "Agent/API 分摊",
    CostItemType.RETURNS_RESERVE: "退货售后预留",
}


def _clock(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("成本服务时间必须含时区")
    return value


def _view(sheet: CostSheet) -> CostSheetView:
    unit_full_cost = None
    try:
        unit_full_cost = compute_unit_full_cost(sheet)
    except (EmptyCostSheetError, MissingFxSnapshotError):
        pass
    return CostSheetView(
        cost_sheet_id=str(sheet.cost_sheet_id),
        opportunity_id=str(sheet.opportunity_id),
        version_type=sheet.version_type.value,
        version_number=sheet.version_number,
        quantity=sheet.quantity,
        base_currency=sheet.base_currency,
        quote_currency=sheet.quote_currency,
        items=[
            CostItemView(
                item_type=item.item_type.value,
                item_label=_ITEM_LABELS[item.item_type],
                amount=item.amount,
                price_basis=item.price_basis,
                is_per_unit=item.is_per_unit,
                item_sequence=item.item_sequence,
                note=item.note,
                source_ref=item.source_ref,
                entered_by_id=(
                    str(item.entered_by) if item.entered_by is not None else None
                ),
                is_pending_confirmation=item.entered_by is None,
            )
            for item in sheet.items
        ],
        created_at=sheet.created_at,
        is_locked=sheet.locked_at is not None,
        has_indicative_items=sheet.has_indicative_items(),
        content_hash=cost_sheet_content_hash(sheet),
        fx_snapshot_id=(
            str(sheet.fx_snapshot_id) if sheet.fx_snapshot_id is not None else None
        ),
        fx_rates=tuple(
            FxRateView(
                base_currency=str(rate.base),
                quote_currency=str(rate.quote),
                rate=rate.rate,
                observed_at=rate.observed_at,
                source=rate.source,
            )
            for rate in sheet.fx_rates
        ),
        unit_full_cost=unit_full_cost,
        risk_accepted_by=(
            str(sheet.risk_acceptance.accepted_by)
            if sheet.risk_acceptance is not None
            else None
        ),
    )


class CostingServiceImpl:
    """所有读写先授权，再在 tenant-bound UoW 内完成。"""

    def __init__(
        self,
        uow_factory: CostingUnitOfWorkFactory,
        authorizer: CostingAuthorizer,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(uow_factory, CostingUnitOfWorkFactory):
            raise ValidationError("成本事务依赖无效")
        if not isinstance(authorizer, CostingAuthorizer):
            raise ValidationError("成本授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self, tenant_id: TenantId, actor: CostingActor, action: CostingAction
    ) -> None:
        self._authorizer.require(actor, action, tenant_id)

    async def create_sheet(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        command: CostSheetCreate,
        *,
        actor: CostingActor,
    ) -> CostSheetId:
        self._require(tenant_id, actor, CostingAction.SHEET_CREATE)
        if not isinstance(command, CostSheetCreate):
            raise ValidationError("成本表创建参数无效")
        try:
            version_type = CostSheetVersion(command.version_type)
        except ValueError as exc:
            raise ValidationError("成本表版本类型无效") from exc
        rates = tuple(
            FxRate(
                base=CurrencyCode(rate.base_currency),
                quote=CurrencyCode(rate.quote_currency),
                rate=rate.rate,
                observed_at=rate.observed_at,
                source=rate.source,
            )
            for rate in command.fx_rates
        )
        now = _clock(self._now())
        async with self._uow_factory(tenant_id) as uow:
            version_number = await uow.sheets.next_version_number(
                tenant_id, opportunity_id, version_type.value
            )
            sheet = CostSheet(
                cost_sheet_id=CostSheetId(new_id("cost")),
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                version_type=version_type,
                version_number=version_number,
                quantity=command.quantity,
                base_currency=command.base_currency,
                quote_currency=command.quote_currency,
                created_at=now,
                fx_snapshot_id=(
                    FxSnapshotId(command.fx_snapshot_id)
                    if command.fx_snapshot_id is not None
                    else None
                ),
                fx_rates=rates,
                created_by=EmployeeId(actor.actor_id),
            )
            await uow.sheets.add(sheet)
        return sheet.cost_sheet_id

    async def add_item(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        command: CostItemCreate,
        *,
        actor: CostingActor,
    ) -> None:
        self._require(tenant_id, actor, CostingAction.ITEM_ADD)
        if not isinstance(command, CostItemCreate):
            raise ValidationError("成本项参数无效")
        try:
            item_type = CostItemType(command.item_type)
        except ValueError as exc:
            raise ValidationError("成本项类型无效") from exc
        async with self._uow_factory(tenant_id) as uow:
            sheet = await uow.sheets.get_for_update(tenant_id, cost_sheet_id)
            if sheet is None:
                raise ValidationError("成本表不存在")
            item = CostItem(
                item_type=item_type,
                amount=Money(command.amount, CurrencyCode(command.currency)),
                price_basis=command.price_basis,
                is_per_unit=command.is_per_unit,
                note=command.note,
                source_ref=command.source_ref,
                entered_by=EmployeeId(actor.actor_id),
                item_sequence=max((item.item_sequence or 0 for item in sheet.items), default=0) + 1,
            )
            sheet.items.append(item)
            await uow.sheets.update(sheet)

    async def assess_for_quote(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        expected_item_types: tuple[str, ...],
        *,
        actor: CostingActor,
    ) -> QuoteReadiness:
        self._require(tenant_id, actor, CostingAction.QUOTE_READINESS_ASSESS)
        try:
            expected = [CostItemType(value) for value in expected_item_types]
        except ValueError as exc:
            raise ValidationError("预期成本项类型无效") from exc
        async with self._uow_factory(tenant_id) as uow:
            sheet = await uow.sheets.get(tenant_id, cost_sheet_id)
            if sheet is None:
                raise ValidationError("成本表不存在")
            return assess_quote_readiness(sheet, expected)

    async def get_sheet(
        self,
        tenant_id: TenantId,
        cost_sheet_id: CostSheetId,
        *,
        actor: CostingActor,
    ) -> CostSheetView:
        self._require(tenant_id, actor, CostingAction.SHEET_READ)
        async with self._uow_factory(tenant_id) as uow:
            sheet = await uow.sheets.get(tenant_id, cost_sheet_id)
            if sheet is None:
                raise CostSheetNotFoundError()
            return _view(sheet)

    async def list_versions(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: CostingActor,
    ) -> list[CostSheetView]:
        self._require(tenant_id, actor, CostingAction.SHEET_READ)
        async with self._uow_factory(tenant_id) as uow:
            sheets = await uow.sheets.list_by_opportunity(tenant_id, opportunity_id)
            return [_view(sheet) for sheet in sheets]


__all__ = ("CostingServiceImpl",)
