"""成本域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from shared.schemas.money import Money


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


CostingDecimalInput = Annotated[
    Decimal,
    BeforeValidator(_parse_costing_decimal),
    WithJsonSchema({"type": "string"}),
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
