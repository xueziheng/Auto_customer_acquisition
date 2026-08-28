"""成本域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

from shared.schemas.money import FxRate, Money


def _parse_costing_decimal(value: object) -> Decimal:
    """兼容 FastAPI 已解码 JSON，同时拒绝 float/int/bool。"""
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("金额必须是有效十进制字符串") from exc
    else:
        raise ValueError("金额必须是十进制字符串")  # noqa: TRY004
    if not parsed.is_finite():
        raise ValueError("金额必须是有限十进制字符串")
    return parsed


def _parse_costing_datetime(value: object) -> datetime:
    """接受 HTTP 的带时区 ISO 8601 字符串或内部已解析 datetime。"""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("时间必须是有效 ISO 8601 字符串") from exc
    else:
        raise ValueError("时间必须是带时区 ISO 8601 字符串")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须含时区")
    return parsed


CostingDecimalInput = Annotated[
    Decimal,
    BeforeValidator(_parse_costing_decimal),
    WithJsonSchema({"type": "string"}),
]

CostingDatetimeInput = Annotated[
    datetime,
    BeforeValidator(_parse_costing_datetime),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


class FxRateCreate(BaseModel):
    """人工录入的不可变汇率快照明细。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    base_currency: str
    quote_currency: str
    rate: CostingDecimalInput
    observed_at: datetime
    source: str


class CostSheetCreate(BaseModel):
    """创建成本表的公共命令；租户与录入人由服务端身份绑定。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    version_type: str
    quantity: int
    base_currency: str
    quote_currency: str
    fx_snapshot_id: str | None = None
    fx_rates: list[FxRateCreate] = Field(default_factory=list)


class CostItemCreate(BaseModel):
    """人工确认成本项；金额在 JSON 边界必须是十进制字符串。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    item_type: str
    amount: CostingDecimalInput
    currency: str
    price_basis: str
    is_per_unit: bool
    source_ref: str
    note: str | None = None


class CostSheetCreated(BaseModel):
    """成本表创建结果。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    cost_sheet_id: str


class QuoteReadinessCheck(BaseModel):
    """人工确认的业务场景成本项清单；检查本身不锁定成本表。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    expected_item_types: list[str]


class CostGroups(BaseModel):
    """同一数量口径下的三组单位成本。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    goods: Decimal
    variable: Decimal
    fixed: Decimal

    @model_validator(mode="after")
    def validate_non_negative_costs(self) -> CostGroups:
        """拒绝负成本，避免错误输入虚增任何利润指标。"""
        if any(value < Decimal(0) for value in (self.goods, self.variable, self.fixed)):
            raise ValueError("成本分组必须为非负有限 Decimal")
        return self


class ProfitMetrics(BaseModel):
    """指定客户单价下的确定性利润指标。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    unit_full_cost: Decimal
    minimum_price: Decimal
    target_price: Decimal
    gross_profit: Decimal
    contribution_profit: Decimal
    full_cost_profit: Decimal
    margin_rate: Decimal
    discount_headroom: Decimal
    additional_acquisition_headroom: Decimal


class PricingPolicyCreate(BaseModel):
    """老板确认的版本化核算和利润政策输入。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    category: str | None
    minimum_margin_rate: CostingDecimalInput
    target_margin_rate: CostingDecimalInput
    cost_groups: dict[str, Literal["goods", "variable", "fixed"]]
    effective_from: CostingDatetimeInput
    source_ref: str

    @model_validator(mode="after")
    def validate_policy(self) -> PricingPolicyCreate:
        """保证政策可明确覆盖全部成本项，且不凭默认值定价。"""
        rates = (self.minimum_margin_rate, self.target_margin_rate)
        if any(not rate.is_finite() for rate in rates):
            raise ValueError("利润率必须是有限 Decimal")
        if (
            not Decimal(0)
            <= self.minimum_margin_rate
            <= self.target_margin_rate
            < Decimal(1)
        ):
            raise ValueError("利润率必须满足 0 ≤ 底线 ≤ 目标 < 1")
        if (
            self.effective_from.tzinfo is None
            or self.effective_from.utcoffset() is None
        ):
            raise ValueError("政策生效时间必须含时区")
        if self.category is not None and not self.category.strip():
            raise ValueError("政策品类不能为空白")
        if not self.source_ref.strip():
            raise ValueError("政策必须有确认来源")
        from domains.costing.models import CostItemType

        expected = {item_type.value for item_type in CostItemType}
        if set(self.cost_groups) != expected:
            raise ValueError("成本归类必须完整且仅覆盖全部成本类型")
        return self


class PricingPolicyView(PricingPolicyCreate):
    """可复现计算所需的已确认政策视图。"""

    policy_id: str
    content_hash: str
    confirmed_by: str
    confirmed_at: datetime

    @model_validator(mode="after")
    def validate_confirmation(self) -> PricingPolicyView:
        """政策只有带可追溯确认事实时才能用于正式计算。"""
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.policy_id, self.content_hash, self.confirmed_by)
        ):
            raise ValueError("政策确认字段不能为空")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("政策确认时间必须含时区")
        return self


class RoundingPolicy(BaseModel):
    """客户展示单价与总额的显式舍入规则。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    unit_places: int
    total_places: int
    strategy: str

    @model_validator(mode="after")
    def validate_rounding(self) -> RoundingPolicy:
        """限制精度并复用 Money 支持的舍入策略。"""
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 12
            for value in (self.unit_places, self.total_places)
        ):
            raise ValueError("报价舍入精度必须是 0 到 12 的整数")
        try:
            Money(Decimal(0), "USD").round_to(self.unit_places, self.strategy)
        except Exception as exc:
            raise ValueError("报价舍入策略无效") from exc
        return self


class PricingOptions(BaseModel):
    """一次报价测算的售价、汇率、展示精度和算法版本。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    mode: Literal["target", "manual"]
    unit_price: Money | None
    rounding: RoundingPolicy
    quote_fx: FxRate | None
    algorithm_version: Literal["costing-v1"]

    @model_validator(mode="after")
    def validate_mode(self) -> PricingOptions:
        """区分内部目标测算与人工实际客户价，禁止伪实际售价。"""
        if self.mode == "manual" and self.unit_price is None:
            raise ValueError("manual 计价必须提供客户单价")
        if self.mode == "target" and self.unit_price is not None:
            raise ValueError("target 计价不得传入伪实际售价")
        return self


class CalculationSnapshot(BaseModel):
    """一次可重现计算的不可变输出快照。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    cost_sheet_id: str
    policy_id: str
    inputs_hash: str
    context_hash: str
    version_number: int
    computed_at: datetime
    base_currency: str
    quote_currency: str
    metrics: ProfitMetrics
    effective_unit_revenue: Money
    displayed_unit_price: Money
    displayed_total: Money


@dataclass(frozen=True)
class FxRateView:
    base_currency: str
    quote_currency: str
    rate: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class CostItemView:
    item_type: str
    item_label: str
    amount: Money
    price_basis: str
    is_per_unit: bool
    note: str | None = None
    source_ref: str | None = None
    entered_by_id: str | None = None
    entered_by_name: str | None = None
    is_pending_confirmation: bool = False
    """模型建议、尚未经人确认。前端要区分显示——未确认项不参与合计。"""


@dataclass(frozen=True)
class CostSheetView:
    """成本表视图。

    ``breakdown`` 为 None 表示还算不了（成本项为空或未锁定汇率）。
    """

    cost_sheet_id: str
    opportunity_id: str
    version_type: str
    version_number: int
    quantity: int
    base_currency: str
    quote_currency: str
    items: list[CostItemView]
    created_at: datetime
    is_locked: bool
    has_indicative_items: bool
    fx_snapshot_id: str | None = None
    fx_rates: tuple[FxRateView, ...] = ()
    unit_full_cost: Money | None = None
    minimum_sellable_price: Money | None = None
    margin_rate: Decimal | None = None
    fx_rate_display: str | None = None
    risk_accepted_by: str | None = None


@dataclass(frozen=True)
class QuoteReadiness:
    """只读可报价性检查结果，不代表成本表已锁定或报价已获审批。

    字段：
        ready
        blockers:   不能报价的原因清单（人类可读）
        indicative_items:  仍是参考价的成本项
        missing_items:     对照期望清单的漏项
    """

    ready: bool
    blockers: list[str] = field(default_factory=list)
    indicative_items: list[str] = field(default_factory=list)
    missing_items: list[str] = field(default_factory=list)
