from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from domains.costing.calculation import canonical_pricing_hash, compute_metrics
from domains.costing.errors import MissingFxSnapshotError
from domains.costing.schemas import (
    CostGroups,
    PricingOptions,
    PricingPolicyView,
    RoundingPolicy,
)
from domains.costing.service import compute_breakdown
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, FxRate, Money

_models = importlib.import_module("domains.costing.models")
CostItem = _models.CostItem
CostItemType = _models.CostItemType
CostSheet = _models.CostSheet
CostSheetVersion = _models.CostSheetVersion
MarginRule = _models.MarginRule


def test_manual_price_is_used_instead_of_target_price() -> None:
    """正式报价利润必须以人工选定售价计算，而非内部目标售价。"""
    value = compute_metrics(
        CostGroups(goods=D(6), variable=D(1), fixed=D(1)),
        D("0.20"),
        D("0.36"),
        D(10),
    )

    assert value.unit_full_cost == D(8)
    assert value.minimum_price == D(10)
    assert value.target_price == D("12.5")
    assert value.gross_profit == D(4)
    assert value.contribution_profit == D(3)
    assert value.full_cost_profit == D(2)
    assert value.margin_rate == D("0.2")
    assert value.additional_acquisition_headroom == D(0)


@pytest.mark.parametrize(
    ("costs", "minimum", "target", "price", "message"),
    [
        (
            CostGroups(goods=D(1), variable=D(0), fixed=D(0)),
            D("-0.01"),
            D(0),
            D(1),
            "利润率",
        ),
        (
            CostGroups(goods=D(1), variable=D(0), fixed=D(0)),
            D("0.3"),
            D("0.2"),
            D(1),
            "利润率",
        ),
        (CostGroups(goods=D(1), variable=D(0), fixed=D(0)), D(0), D(1), D(1), "利润率"),
        (CostGroups(goods=D(1), variable=D(0), fixed=D(0)), D(0), D(0), D(0), "售价"),
        (
            CostGroups(goods=D(0), variable=D(0), fixed=D(0)),
            D(0),
            D(0),
            D(1),
            "完整成本",
        ),
    ],
)
def test_metrics_rejects_invalid_costs_rates_prices_and_zero_total_cost(
    costs: CostGroups,
    minimum: D,
    target: D,
    price: D,
    message: str,
) -> None:
    """错误输入必须被阻断，不能静默用零或无穷值继续定价。"""
    with pytest.raises(ValidationError, match=message):
        compute_metrics(costs, minimum, target, price)


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity")])
def test_cost_groups_reject_non_finite_decimal(value: D) -> None:
    """成本输入必须为有限 Decimal，避免把非数值带入公式。"""
    with pytest.raises(ValueError):
        CostGroups(goods=value, variable=D(0), fixed=D(0))


def test_cost_groups_reject_negative_costs() -> None:
    """负成本会虚增利润，必须在成本分组边界拒绝。"""
    with pytest.raises(ValueError, match="非负"):
        CostGroups(goods=D("-0.01"), variable=D(0), fixed=D(0))


def test_canonical_pricing_hash_normalizes_values_without_dropping_repeated_items() -> (
    None
):
    """等值 Decimal 归一，列表项仍逐个参与输入哈希。"""
    first = canonical_pricing_hash(
        {
            "amount": D("1.0"),
            "observed_at": datetime(2026, 8, 28, 8, tzinfo=UTC),
            "items": [{"source": "line-1"}, {"source": "line-1"}],
        }
    )
    equivalent_decimal = canonical_pricing_hash(
        {
            "amount": D("1.00"),
            "observed_at": datetime(2026, 8, 28, 8, tzinfo=UTC),
            "items": [{"source": "line-1"}, {"source": "line-1"}],
        }
    )
    single_item = canonical_pricing_hash(
        {
            "amount": D("1.00"),
            "observed_at": datetime(2026, 8, 28, 8, tzinfo=UTC),
            "items": [{"source": "line-1"}],
        }
    )

    assert first == equivalent_decimal
    assert first != single_item


@pytest.mark.parametrize(
    "changed",
    [
        {"rule": "policy-v2"},
        {"quantity": 101},
        {"unit_price": D("12.51")},
        {"rounding": {"unit_places": 3, "total_places": 2}},
        {"source_ref": "supplier-line-2"},
        {"quote_fx": D("0.81")},
    ],
)
def test_canonical_pricing_hash_changes_when_commercial_input_changes(
    changed: dict[str, object],
) -> None:
    """规则、数量、价格、精度、来源和汇率都是冻结计算依据。"""
    baseline = {
        "rule": "policy-v1",
        "quantity": 100,
        "unit_price": D("12.50"),
        "rounding": {"unit_places": 2, "total_places": 2},
        "source_ref": "supplier-line-1",
        "quote_fx": D("0.80"),
    }

    assert canonical_pricing_hash(baseline) != canonical_pricing_hash(
        baseline | changed
    )


def _pricing_policy(
    *,
    policy_id: str = "policy-1",
    minimum: D = D("0.20"),
    target: D = D("0.36"),
) -> PricingPolicyView:
    return PricingPolicyView(
        policy_id=policy_id,
        content_hash="policy-content-hash",
        category=None,
        minimum_margin_rate=minimum,
        target_margin_rate=target,
        cost_groups={
            item_type.value: (
                "goods"
                if item_type is CostItemType.PRODUCT_PURCHASE
                else "variable"
                if item_type is CostItemType.PACKAGING
                else "fixed"
            )
            for item_type in CostItemType
        },
        effective_from=datetime(2026, 8, 28, 8, tzinfo=UTC),
        source_ref="boss-policy-record",
        confirmed_by="boss-1",
        confirmed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
    )


def _quoted_sheet(
    *,
    quantity: int = 3,
    fx_snapshot_id: str = "cost-fx-1",
    product_source: str = "supplier-quote",
    include_pending_item: bool = False,
) -> CostSheet:
    """构造含外币货品、单位费用和整单费用的完整报价成本表。"""
    items = [
        CostItem(
            item_type=CostItemType.PRODUCT_PURCHASE,
            amount=Money(D(7), CurrencyCode("CNY")),
            price_basis="quoted",
            is_per_unit=True,
            source_ref=product_source,
            entered_by=EmployeeId("buyer-1"),
        ),
        CostItem(
            item_type=CostItemType.PACKAGING,
            amount=Money(D("0.02"), CurrencyCode("USD")),
            price_basis="quoted",
            is_per_unit=True,
            source_ref="packing-quote",
            entered_by=EmployeeId("buyer-1"),
        ),
        CostItem(
            item_type=CostItemType.DOMESTIC_FREIGHT,
            amount=Money(D(3), CurrencyCode("USD")),
            price_basis="quoted",
            is_per_unit=False,
            source_ref="freight-quote",
            entered_by=EmployeeId("buyer-1"),
        ),
    ]
    if include_pending_item:
        items.append(
            CostItem(
                item_type=CostItemType.AGENT_API_ALLOCATION,
                amount=Money(D(999), CurrencyCode("USD")),
                price_basis="quoted",
                is_per_unit=True,
                source_ref=None,
                entered_by=None,
            )
        )
    return CostSheet(
        cost_sheet_id=CostSheetId("sheet-1"),
        tenant_id=TenantId("tenant-1"),
        opportunity_id=OpportunityId("opportunity-1"),
        version_type=CostSheetVersion.QUOTED,
        version_number=3,
        quantity=quantity,
        base_currency="USD",
        quote_currency="EUR",
        created_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        fx_snapshot_id=FxSnapshotId(fx_snapshot_id),
        fx_rates=(
            FxRate(
                base=CurrencyCode("CNY"),
                quote=CurrencyCode("USD"),
                rate=D("0.14"),
                observed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
                source="locked-cost-fx",
            ),
        ),
        items=items,
    )


def _margin_rule(
    *, minimum: D = D("0.20"), target: D = D("0.36")
) -> MarginRule:
    """构造与测试政策一致的旧兼容利润规则。"""
    return MarginRule(
        tenant_id=TenantId("tenant-1"),
        minimum_margin_rate=minimum,
        target_margin_rate=target,
        effective_from=datetime(2026, 8, 28, 8, tzinfo=UTC),
    )


def _manual_options(
    *,
    unit_price: D = D("2.005"),
    unit_places: int = 2,
    total_places: int = 2,
    quote_fx: FxRate | None = None,
) -> PricingOptions:
    """构造人工客户价与锁定核算币到报价币直连汇率。"""
    return PricingOptions(
        mode="manual",
        unit_price=Money(unit_price, CurrencyCode("EUR")),
        rounding=RoundingPolicy(
            unit_places=unit_places,
            total_places=total_places,
            strategy="ROUND_HALF_UP",
        ),
        quote_fx=quote_fx
        or FxRate(
            base=CurrencyCode("USD"),
            quote=CurrencyCode("EUR"),
            rate=D("0.8"),
            observed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
            source="locked-quote-fx",
        ),
        algorithm_version="costing-v1",
    )


def _breakdown(
    *,
    sheet: CostSheet | None = None,
    policy: PricingPolicyView | None = None,
    margin_rule: MarginRule | None = None,
    options: PricingOptions | None = None,
    now: datetime = datetime(2026, 8, 28, 9, tzinfo=UTC),
):
    """只通过公开计算接口取得快照，测试不接触内部哈希构造。"""
    return compute_breakdown(
        sheet or _quoted_sheet(),
        margin_rule or _margin_rule(),
        policy=policy or _pricing_policy(),
        options=options or _manual_options(),
        coverage_hash="coverage-hash",
        context_hash="context-hash",
        now=now,
    )


def test_breakdown_uses_grouped_costs_and_rounded_customer_revenue() -> None:
    """汇总按归类/数量/方向汇率，利润以已展示的客户总额还原收入。"""
    sheet = CostSheet(
        cost_sheet_id=CostSheetId("sheet-1"),
        tenant_id=TenantId("tenant-1"),
        opportunity_id=OpportunityId("opportunity-1"),
        version_type=CostSheetVersion.QUOTED,
        version_number=3,
        quantity=3,
        base_currency="USD",
        quote_currency="EUR",
        created_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        fx_snapshot_id=FxSnapshotId("cost-fx-1"),
        fx_rates=(
            FxRate(
                base=CurrencyCode("CNY"),
                quote=CurrencyCode("USD"),
                rate=D("0.14"),
                observed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
                source="locked-cost-fx",
            ),
        ),
        items=[
            CostItem(
                item_type=CostItemType.PRODUCT_PURCHASE,
                amount=Money(D(7), CurrencyCode("CNY")),
                price_basis="quoted",
                is_per_unit=True,
                source_ref="supplier-quote",
                entered_by=EmployeeId("buyer-1"),
            ),
            CostItem(
                item_type=CostItemType.PACKAGING,
                amount=Money(D("0.02"), CurrencyCode("USD")),
                price_basis="quoted",
                is_per_unit=True,
                source_ref="packing-quote",
                entered_by=EmployeeId("buyer-1"),
            ),
            CostItem(
                item_type=CostItemType.DOMESTIC_FREIGHT,
                amount=Money(D(3), CurrencyCode("USD")),
                price_basis="quoted",
                is_per_unit=False,
                source_ref="freight-quote",
                entered_by=EmployeeId("buyer-1"),
            ),
        ],
    )
    margin_rule = MarginRule(
        tenant_id=TenantId("tenant-1"),
        minimum_margin_rate=D("0.20"),
        target_margin_rate=D("0.36"),
        effective_from=datetime(2026, 8, 28, 8, tzinfo=UTC),
    )

    result = compute_breakdown(
        sheet,
        margin_rule,
        policy=_pricing_policy(),
        options=PricingOptions(
            mode="manual",
            unit_price=Money(D("2.005"), CurrencyCode("EUR")),
            rounding=RoundingPolicy(
                unit_places=2,
                total_places=2,
                strategy="ROUND_HALF_UP",
            ),
            quote_fx=FxRate(
                base=CurrencyCode("USD"),
                quote=CurrencyCode("EUR"),
                rate=D("0.8"),
                observed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
                source="locked-quote-fx",
            ),
            algorithm_version="costing-v1",
        ),
        coverage_hash="coverage-hash",
        context_hash="context-hash",
        now=datetime(2026, 8, 28, 9, tzinfo=UTC),
    )

    assert result.metrics.unit_full_cost == D("2.00")
    assert result.metrics.gross_profit == D("1.5325")
    assert result.metrics.contribution_profit == D("1.5125")
    assert result.metrics.full_cost_profit == D("0.5125")
    assert result.displayed_unit_price == Money(D("2.01"), CurrencyCode("EUR"))
    assert result.displayed_total == Money(D("6.03"), CurrencyCode("EUR"))
    assert result.effective_unit_revenue == Money(D("2.5125"), CurrencyCode("USD"))
    assert result.computed_at == datetime(2026, 8, 28, 9, tzinfo=UTC)


def test_breakdown_inputs_hash_covers_real_commercial_inputs_but_not_clock() -> None:
    """真实计算快照的哈希随商业依据变化，不能随计算时钟漂移。"""
    baseline = _breakdown()
    changed_results = [
        _breakdown(policy=_pricing_policy(policy_id="policy-2")),
        _breakdown(sheet=_quoted_sheet(quantity=4)),
        _breakdown(options=_manual_options(unit_price=D("2.015"))),
        _breakdown(options=_manual_options(unit_places=3, total_places=2)),
        _breakdown(sheet=_quoted_sheet(product_source="supplier-quote-v2")),
        _breakdown(
            options=_manual_options(
                quote_fx=FxRate(
                    base=CurrencyCode("USD"),
                    quote=CurrencyCode("EUR"),
                    rate=D("0.81"),
                    observed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
                    source="locked-quote-fx-v2",
                )
            )
        ),
        _breakdown(sheet=_quoted_sheet(fx_snapshot_id="cost-fx-2")),
    ]

    assert all(result.inputs_hash != baseline.inputs_hash for result in changed_results)
    assert (
        _breakdown(now=datetime(2026, 8, 29, 9, tzinfo=UTC)).inputs_hash
        == baseline.inputs_hash
    )


def test_breakdown_excludes_pending_amount_and_applies_distinct_unit_total_precision() -> None:
    """未确认金额不入成本；客户单价和总额必须依次按各自精度舍入。"""
    with_pending = _breakdown(sheet=_quoted_sheet(include_pending_item=True))
    different_precision = _breakdown(
        options=_manual_options(unit_places=2, total_places=0)
    )

    assert with_pending.metrics.unit_full_cost == D("2.00")
    assert with_pending.metrics.full_cost_profit == D("0.5125")
    assert different_precision.displayed_unit_price == Money(
        D("2.01"), CurrencyCode("EUR")
    )
    assert different_precision.displayed_total == Money(D(6), CurrencyCode("EUR"))
    assert different_precision.effective_unit_revenue == Money(
        D("2.5"), CurrencyCode("USD")
    )


def test_breakdown_rejects_missing_or_reverse_quote_fx() -> None:
    """报价换算只能使用存在的核算币种到报价币种直连冻结汇率。"""
    missing = PricingOptions(
        mode="manual",
        unit_price=Money(D("2.005"), CurrencyCode("EUR")),
        rounding=RoundingPolicy(
            unit_places=2,
            total_places=2,
            strategy="ROUND_HALF_UP",
        ),
        quote_fx=None,
        algorithm_version="costing-v1",
    )
    reverse = _manual_options(
        quote_fx=FxRate(
            base=CurrencyCode("EUR"),
            quote=CurrencyCode("USD"),
            rate=D("1.25"),
            observed_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
            source="reverse-quote-fx",
        )
    )

    with pytest.raises(MissingFxSnapshotError, match="锁定报价汇率"):
        _breakdown(options=missing)
    with pytest.raises(ValidationError, match="直连方向"):
        _breakdown(options=reverse)


@pytest.mark.parametrize(
    ("unit_price", "full_cost_profit"),
    [(D(9), D(1)), (D(7), D(-1))],
)
def test_breakdown_preserves_below_floor_and_loss_metrics(
    unit_price: D,
    full_cost_profit: D,
) -> None:
    """低于底线和亏损仍是待审批报价的事实，不能在计算阶段被隐藏。"""
    sheet = CostSheet(
        cost_sheet_id=CostSheetId("sheet-floor"),
        tenant_id=TenantId("tenant-1"),
        opportunity_id=OpportunityId("opportunity-1"),
        version_type=CostSheetVersion.QUOTED,
        version_number=1,
        quantity=1,
        base_currency="USD",
        quote_currency="USD",
        created_at=datetime(2026, 8, 28, 8, tzinfo=UTC),
        fx_snapshot_id=FxSnapshotId("cost-fx-1"),
        items=[
            CostItem(
                item_type=CostItemType.PRODUCT_PURCHASE,
                amount=Money(D(8), CurrencyCode("USD")),
                price_basis="quoted",
                is_per_unit=True,
                source_ref="supplier-quote",
                entered_by=EmployeeId("buyer-1"),
            )
        ],
    )
    margin_rule = MarginRule(
        tenant_id=TenantId("tenant-1"),
        minimum_margin_rate=D("0.20"),
        target_margin_rate=D("0.36"),
        effective_from=datetime(2026, 8, 28, 8, tzinfo=UTC),
    )

    result = compute_breakdown(
        sheet,
        margin_rule,
        policy=_pricing_policy(),
        options=PricingOptions(
            mode="manual",
            unit_price=Money(unit_price, CurrencyCode("USD")),
            rounding=RoundingPolicy(
                unit_places=2,
                total_places=2,
                strategy="ROUND_HALF_UP",
            ),
            quote_fx=None,
            algorithm_version="costing-v1",
        ),
        coverage_hash="coverage-hash",
        context_hash="context-hash",
        now=datetime(2026, 8, 28, 9, tzinfo=UTC),
    )

    assert result.metrics.minimum_price == D(10)
    assert result.metrics.full_cost_profit == full_cost_profit
    assert result.metrics.margin_rate < D("0.20")
    assert result.metrics.discount_headroom == D(0)
    assert result.metrics.additional_acquisition_headroom == D(0)
