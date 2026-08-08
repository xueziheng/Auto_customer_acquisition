"""成本域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from shared.schemas.money import Money


@dataclass(frozen=True)
class CostItemView:
    item_type: str
    item_label: str
    amount: Money
    price_basis: str
    is_per_unit: bool
    note: str | None = None
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
    unit_full_cost: Money | None = None
    minimum_sellable_price: Money | None = None
    margin_rate: Decimal | None = None
    fx_rate_display: str | None = None
    risk_accepted_by: str | None = None


@dataclass(frozen=True)
class QuoteReadiness:
    """可报价性检查结果 —— ``lock_for_quote`` 的返回。

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
