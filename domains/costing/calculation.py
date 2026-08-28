"""成本域的可复现纯计算。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Any

from domains.costing.errors import EmptyCostSheetError, MissingFxSnapshotError
from domains.costing.models import CostSheet, MarginRule
from domains.costing.schemas import (
    CalculationSnapshot,
    CostGroups,
    PricingOptions,
    PricingPolicyView,
    ProfitMetrics,
)
from shared.errors import ValidationError
from shared.schemas.money import CurrencyCode, Money, convert

_COSTING_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)


def _canonical_decimal(value: Decimal) -> str:
    """以不依赖当前 Decimal 上下文的十进制文本表示相同数值。"""
    if not value.is_finite():
        raise ValidationError("哈希输入 Decimal 必须有限")
    sign, digits, exponent = value.as_tuple()
    text = "".join(str(digit) for digit in digits).lstrip("0") or "0"
    if text == "0":
        return "0"
    while exponent < 0 and text.endswith("0"):
        text = text[:-1]
        exponent += 1
    if exponent >= 0:
        result = text + "0" * exponent
    elif len(text) + exponent > 0:
        split = len(text) + exponent
        result = f"{text[:split]}.{text[split:]}"
    else:
        result = f"0.{('0' * -(len(text) + exponent))}{text}"
    return f"-{result}" if sign else result


def _canonical_json_value(value: object) -> Any:
    """规范化已确定类型的 JSON 数据，拒绝无序或隐式精度的输入。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("哈希输入时间必须含时区")
        return (
            value.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValidationError("哈希输入不接受 float")
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError("哈希输入对象键必须是字符串")
            normalized[key] = _canonical_json_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    raise ValidationError(f"哈希输入类型无效：{type(value).__name__}")


def canonical_pricing_hash(payload: Mapping[str, object]) -> str:
    """计算价格输入的稳定 SHA-256，不读取时钟或任何外部状态。"""
    if not isinstance(payload, Mapping):
        raise ValidationError("哈希输入必须是映射")
    canonical = _canonical_json_value(payload)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_finite_decimal(value: object, *, field: str) -> Decimal:
    """将内部计算输入限定为有限 Decimal，拒绝隐式数值转换。"""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError(f"{field}必须是有限 Decimal")
    return value


def compute_metrics(
    costs: CostGroups,
    minimum: Decimal,
    target: Decimal,
    price: Decimal,
) -> ProfitMetrics:
    """按人工选定售价计算三组成本下的利润指标，不读取外部状态。"""
    minimum = _require_finite_decimal(minimum, field="最低利润率")
    target = _require_finite_decimal(target, field="目标利润率")
    price = _require_finite_decimal(price, field="售价")
    if not Decimal(0) <= minimum <= target < Decimal(1):
        raise ValidationError("利润率必须满足 0 ≤ 底线 ≤ 目标 < 1")
    if price <= Decimal(0):
        raise ValidationError("售价必须为正")
    with localcontext(_COSTING_CONTEXT):
        full = costs.goods + costs.variable + costs.fixed
        if full == Decimal(0):
            raise ValidationError("单位完整成本为零，不能自动定价")
        floor = full / (Decimal(1) - minimum)
        target_price = full / (Decimal(1) - target)
        profit = price - full
        margin = profit / price
        discount = max(Decimal(0), Decimal(1) - floor / price)
        acquisition = max(Decimal(0), price * (Decimal(1) - minimum) - full)
        return ProfitMetrics(
            unit_full_cost=full,
            minimum_price=floor,
            target_price=target_price,
            gross_profit=price - costs.goods,
            contribution_profit=price - costs.goods - costs.variable,
            full_cost_profit=profit,
            margin_rate=margin,
            discount_headroom=discount,
            additional_acquisition_headroom=acquisition,
        )


def _aggregate_cost_groups(sheet: CostSheet, policy: PricingPolicyView) -> CostGroups:
    """按锁定汇率、数量和老板确认归类聚合已确认成本。"""
    confirmed_items = [item for item in sheet.items if item.entered_by is not None]
    if not confirmed_items:
        raise EmptyCostSheetError("成本表没有已确认成本项，不能计算报价")
    rates = {rate.base: rate for rate in sheet.fx_rates}
    groups = {"goods": Decimal(0), "variable": Decimal(0), "fixed": Decimal(0)}
    base_currency = CurrencyCode(sheet.base_currency)
    for item in confirmed_items:
        amount = item.amount
        if amount.currency != base_currency:
            rate = rates.get(amount.currency)
            if rate is None:
                raise MissingFxSnapshotError(
                    f"汇率快照缺少 {amount.currency} 到 {sheet.base_currency} 的直连汇率"
                )
            amount = convert(amount, base_currency, rate)
        unit_amount = (
            amount.amount
            if item.is_per_unit
            else amount.amount / Decimal(sheet.quantity)
        )
        group = policy.cost_groups.get(item.item_type.value)
        if group is None:
            raise ValidationError(f"成本类型 {item.item_type.value} 没有确认归类")
        groups[group] += unit_amount
    return CostGroups(**groups)


def _require_hash(value: str, *, field: str) -> str:
    """阻断空的上下文或覆盖哈希，防止误把未知依据冻结为相同输入。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field}不能为空")
    return value


def _quote_to_base_revenue(
    quote_revenue: Money,
    *,
    sheet: CostSheet,
    options: PricingOptions,
) -> Money:
    """用锁定的核算币到报价币直连关系还原核算收入，不自动倒数查价。"""
    base_currency = CurrencyCode(sheet.base_currency)
    quote_currency = CurrencyCode(sheet.quote_currency)
    if quote_revenue.currency != quote_currency:
        raise ValidationError("客户单价币种必须与成本表报价币种一致")
    if base_currency == quote_currency:
        if options.quote_fx is not None and options.quote_fx.rate != Decimal(1):
            raise ValidationError("同币种报价汇率必须为 1")
        return Money(quote_revenue.amount, base_currency)
    quote_fx = options.quote_fx
    if quote_fx is None:
        raise MissingFxSnapshotError("报价币种不同于核算币种时必须提供锁定报价汇率")
    if quote_fx.base != base_currency or quote_fx.quote != quote_currency:
        raise ValidationError("报价汇率必须是核算币种到报价币种的直连方向")
    return Money(quote_revenue.amount / quote_fx.rate, base_currency)


def _base_to_quote_price(
    base_price: Decimal,
    *,
    sheet: CostSheet,
    options: PricingOptions,
) -> Money:
    """将内部核算售价按已声明方案换为客户报价币种。"""
    base_currency = CurrencyCode(sheet.base_currency)
    quote_currency = CurrencyCode(sheet.quote_currency)
    if base_currency == quote_currency:
        if options.quote_fx is not None and options.quote_fx.rate != Decimal(1):
            raise ValidationError("同币种报价汇率必须为 1")
        return Money(base_price, quote_currency)
    quote_fx = options.quote_fx
    if quote_fx is None:
        raise MissingFxSnapshotError("报价币种不同于核算币种时必须提供锁定报价汇率")
    if quote_fx.base != base_currency or quote_fx.quote != quote_currency:
        raise ValidationError("报价汇率必须是核算币种到报价币种的直连方向")
    return Money(base_price * quote_fx.rate, quote_currency)


def _calculation_inputs(
    sheet: CostSheet,
    margin_rule: MarginRule,
    policy: PricingPolicyView,
    options: PricingOptions,
    coverage_hash: str,
    context_hash: str,
) -> dict[str, object]:
    """构造覆盖全部数值依据的稳定哈希输入，不纳入调用时钟。"""
    return {
        "algorithm_version": options.algorithm_version,
        "coverage_hash": coverage_hash,
        "context_hash": context_hash,
        "margin_rule": {
            "minimum": margin_rule.minimum_margin_rate,
            "target": margin_rule.target_margin_rate,
            "category": margin_rule.category,
            "effective_from": margin_rule.effective_from,
        },
        "policy": {
            "policy_id": policy.policy_id,
            "content_hash": policy.content_hash,
            "minimum": policy.minimum_margin_rate,
            "target": policy.target_margin_rate,
            "cost_groups": policy.cost_groups,
            "effective_from": policy.effective_from,
            "source_ref": policy.source_ref,
            "confirmed_by": policy.confirmed_by,
            "confirmed_at": policy.confirmed_at,
        },
        "sheet": {
            "cost_sheet_id": str(sheet.cost_sheet_id),
            "fx_snapshot_id": (
                None if sheet.fx_snapshot_id is None else str(sheet.fx_snapshot_id)
            ),
            "version_number": sheet.version_number,
            "quantity": sheet.quantity,
            "base_currency": sheet.base_currency,
            "quote_currency": sheet.quote_currency,
            "items": [
                {
                    "item_type": item.item_type.value,
                    "amount": {
                        "value": item.amount.amount,
                        "currency": item.amount.currency,
                    },
                    "price_basis": item.price_basis,
                    "is_per_unit": item.is_per_unit,
                    "source_ref": item.source_ref,
                    "entered_by": item.entered_by,
                }
                for item in sheet.items
            ],
            "fx_rates": [
                {
                    "base": rate.base,
                    "quote": rate.quote,
                    "rate": rate.rate,
                    "observed_at": rate.observed_at,
                    "source": rate.source,
                }
                for rate in sheet.fx_rates
            ],
        },
        "options": {
            "mode": options.mode,
            "unit_price": (
                None
                if options.unit_price is None
                else {
                    "value": options.unit_price.amount,
                    "currency": options.unit_price.currency,
                }
            ),
            "rounding": options.rounding.model_dump(),
            "quote_fx": (
                None
                if options.quote_fx is None
                else {
                    "base": options.quote_fx.base,
                    "quote": options.quote_fx.quote,
                    "rate": options.quote_fx.rate,
                    "observed_at": options.quote_fx.observed_at,
                    "source": options.quote_fx.source,
                }
            ),
        },
    }


def compute_breakdown(
    sheet: CostSheet,
    margin_rule: MarginRule,
    *,
    policy: PricingPolicyView,
    options: PricingOptions,
    coverage_hash: str,
    context_hash: str,
    now: datetime,
) -> CalculationSnapshot:
    """生成不读数据库/时钟的成本、客户展示金额和利润可重现快照。"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValidationError("计算时间必须含时区")
    _require_hash(coverage_hash, field="成本覆盖哈希")
    _require_hash(context_hash, field="业务上下文哈希")
    if (
        margin_rule.minimum_margin_rate != policy.minimum_margin_rate
        or margin_rule.target_margin_rate != policy.target_margin_rate
    ):
        raise ValidationError("利润规则与已确认定价政策不一致")
    with localcontext(_COSTING_CONTEXT):
        costs = _aggregate_cost_groups(sheet, policy)
        preliminary = compute_metrics(
            costs,
            policy.minimum_margin_rate,
            policy.target_margin_rate,
            (costs.goods + costs.variable + costs.fixed)
            / (Decimal(1) - policy.target_margin_rate),
        )
        raw_customer_price = (
            _base_to_quote_price(preliminary.target_price, sheet=sheet, options=options)
            if options.mode == "target"
            else options.unit_price
        )
        if raw_customer_price is None:
            raise ValidationError("manual 计价必须提供客户单价")
        displayed_unit_price = raw_customer_price.round_to(
            options.rounding.unit_places, options.rounding.strategy
        )
        displayed_total = Money(
            displayed_unit_price.amount * Decimal(sheet.quantity),
            displayed_unit_price.currency,
        ).round_to(options.rounding.total_places, options.rounding.strategy)
        effective_quote_revenue = Money(
            displayed_total.amount / Decimal(sheet.quantity), displayed_total.currency
        )
        effective_base_revenue = _quote_to_base_revenue(
            effective_quote_revenue, sheet=sheet, options=options
        )
        metrics = compute_metrics(
            costs,
            policy.minimum_margin_rate,
            policy.target_margin_rate,
            effective_base_revenue.amount,
        )
        return CalculationSnapshot(
            cost_sheet_id=str(sheet.cost_sheet_id),
            policy_id=policy.policy_id,
            inputs_hash=canonical_pricing_hash(
                _calculation_inputs(
                    sheet, margin_rule, policy, options, coverage_hash, context_hash
                )
            ),
            context_hash=context_hash,
            version_number=sheet.version_number,
            computed_at=now,
            base_currency=sheet.base_currency,
            quote_currency=sheet.quote_currency,
            metrics=metrics,
            effective_unit_revenue=effective_base_revenue,
            displayed_unit_price=displayed_unit_price,
            displayed_total=displayed_total,
        )
