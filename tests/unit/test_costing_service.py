from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self

import pytest

from domains.costing.permissions import (
    CostingActor,
    CostingScope,
    Phase1CostingAuthorizer,
)
from domains.costing.schemas import (
    CostItemCreate,
    CostSheetCreate,
    FxRateCreate,
)
from domains.costing.service_impl import CostingServiceImpl
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    TenantId,
)

_repository = importlib.import_module("domains.costing.repository")
CostSheetRepository = _repository.CostSheetRepository
CostingUnitOfWork = _repository.CostingUnitOfWork

NOW = datetime(2026, 8, 23, 14, tzinfo=UTC)
TENANT = TenantId("tenant-costing-service")
OPPORTUNITY = OpportunityId("opportunity-costing-service")


class _Sheets:
    def __init__(self) -> None:
        self.by_id: dict[CostSheetId, object] = {}

    async def add(self, sheet: object) -> None:
        self.by_id[sheet.cost_sheet_id] = sheet  # type: ignore[attr-defined]

    async def get(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> object | None:
        assert tenant_id == TENANT
        return self.by_id.get(cost_sheet_id)

    async def get_for_update(
        self, tenant_id: TenantId, cost_sheet_id: CostSheetId
    ) -> object | None:
        return await self.get(tenant_id, cost_sheet_id)

    async def update(self, sheet: object) -> None:
        self.by_id[sheet.cost_sheet_id] = sheet  # type: ignore[attr-defined]

    async def next_version_number(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        version_type: str,
    ) -> int:
        assert tenant_id == TENANT
        versions = [
            sheet.version_number  # type: ignore[attr-defined]
            for sheet in self.by_id.values()
            if sheet.opportunity_id == opportunity_id  # type: ignore[attr-defined]
            and sheet.version_type.value == version_type  # type: ignore[attr-defined]
        ]
        return max(versions, default=0) + 1

    async def list_by_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[object]:
        assert tenant_id == TENANT
        return [
            sheet
            for sheet in self.by_id.values()
            if sheet.opportunity_id == opportunity_id  # type: ignore[attr-defined]
        ]


class _Uow:
    def __init__(self, sheets: _Sheets) -> None:
        self.sheets = sheets
        self.margin_rules = _MarginRules()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        del exc_type, exc, tb


class _MarginRules:
    async def get_effective(self, tenant_id: TenantId, category: str | None) -> object:
        del tenant_id, category
        raise AssertionError("本测试不读取利润规则")

    async def set_rule(self, rule: object) -> None:
        del rule
        raise AssertionError("本测试不写利润规则")


class _Factory:
    def __init__(self, sheets: _Sheets) -> None:
        self.sheets = sheets

    def __call__(self, tenant_id: TenantId) -> object:
        assert tenant_id == TENANT
        return _Uow(self.sheets)  # type: ignore[return-value]


def _actor(role: str = "finance") -> CostingActor:
    return CostingActor(
        actor_id="employee-costing-service",
        role=role,
        scope=CostingScope.TENANT,
    )


def _service() -> tuple[CostingServiceImpl, _Sheets]:
    sheets = _Sheets()
    return (
        CostingServiceImpl(
            _Factory(sheets),
            Phase1CostingAuthorizer(TENANT),
            now=lambda: NOW,
        ),
        sheets,
    )


def _quoted_create() -> CostSheetCreate:
    return CostSheetCreate(
        version_type="quoted",
        quantity=100,
        base_currency="USD",
        quote_currency="EUR",
        fx_snapshot_id="fx-costing-service",
        fx_rates=[
            FxRateCreate(
                base_currency="CNY",
                quote_currency="USD",
                rate=Decimal("0.14"),
                observed_at=NOW,
                source="manual-bank-snapshot",
            )
        ],
    )


@pytest.mark.asyncio
async def test_create_sheet_binds_actor_fx_snapshot_and_allocates_versions() -> None:
    service, sheets = _service()

    first_id = await service.create_sheet(
        TENANT, OPPORTUNITY, _quoted_create(), actor=_actor()
    )
    second_id = await service.create_sheet(
        TENANT, OPPORTUNITY, _quoted_create(), actor=_actor()
    )

    first = sheets.by_id[first_id]
    second = sheets.by_id[second_id]
    assert first.created_by == EmployeeId("employee-costing-service")  # type: ignore[attr-defined]
    assert first.version_number == 1  # type: ignore[attr-defined]
    assert second.version_number == 2  # type: ignore[attr-defined]
    assert first.fx_snapshot_id == "fx-costing-service"  # type: ignore[attr-defined]
    assert first.fx_rates[0].rate == Decimal("0.14")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_manual_add_item_records_human_and_source_then_exposes_provenance() -> None:
    service, _sheets = _service()
    sheet_id = await service.create_sheet(
        TENANT, OPPORTUNITY, _quoted_create(), actor=_actor()
    )

    await service.add_item(
        TENANT,
        sheet_id,
        CostItemCreate(
            item_type="product_purchase",
            amount=Decimal("12.50"),
            currency="USD",
            price_basis="quoted",
            is_per_unit=True,
            source_ref="supplier-quote-artifact-1",
            note="100 件阶梯价",
        ),
        actor=_actor(),
    )
    view = await service.get_sheet(TENANT, sheet_id, actor=_actor())

    assert len(view.items) == 1
    assert view.items[0].amount.amount == Decimal("12.50")
    assert view.items[0].entered_by_id == "employee-costing-service"
    assert view.items[0].source_ref == "supplier-quote-artifact-1"
    assert view.fx_snapshot_id == "fx-costing-service"
    assert view.fx_rates[0].rate == Decimal("0.14")


@pytest.mark.asyncio
async def test_quote_readiness_is_read_only_and_reports_missing_scenario_items() -> None:
    service, sheets = _service()
    sheet_id = await service.create_sheet(
        TENANT, OPPORTUNITY, _quoted_create(), actor=_actor()
    )
    await service.add_item(
        TENANT,
        sheet_id,
        CostItemCreate(
            item_type="product_purchase",
            amount=Decimal("12.50"),
            currency="USD",
            price_basis="quoted",
            is_per_unit=True,
            source_ref="supplier-quote-artifact-1",
        ),
        actor=_actor(),
    )

    result = await service.assess_for_quote(
        TENANT,
        sheet_id,
        expected_item_types=("product_purchase", "packaging"),
        actor=_actor(),
    )

    assert result.ready is False
    assert result.missing_items == ["packaging"]
    assert sheets.by_id[sheet_id].locked_at is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_service_rejects_unconfigured_role_and_missing_sheet_indistinguishably() -> None:
    service, _sheets = _service()

    with pytest.raises(PermissionDenied, match="成本授权拒绝"):
        await service.list_versions(TENANT, OPPORTUNITY, actor=_actor("sales"))

    with pytest.raises(ValidationError, match="成本表不存在"):
        await service.get_sheet(
            TENANT,
            CostSheetId("cost-missing"),
            actor=_actor(),
        )


def test_repository_and_uow_contracts_cover_locked_reads() -> None:
    sheets = _Sheets()
    uow = _Uow(sheets)

    assert isinstance(sheets, CostSheetRepository)
    assert isinstance(uow, CostingUnitOfWork)
