"""报价域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from domains.quotations.context import QuoteBusinessContext as QuoteBusinessContext
from domains.quotations.context import QuoteIssuer as QuoteIssuer
from domains.quotations.context import (
    QuoteSpecificationFacts as QuoteSpecificationFacts,
)
from shared.schemas.money import Money
from shared.schemas.quote_creation import QuoteRoundingInput as QuoteRoundingInput
from shared.schemas.quote_creation import QuoteTerm as QuoteTerm


@dataclass(frozen=True)
class QuoteLineRequest:
    line_number: int
    description: str
    quantity: int
    unit_price: Money
    price_snapshot_ref: str
    moq: int | None = None
    lead_time_days: int | None = None


@dataclass(frozen=True)
class QuoteCreateRequest:
    """创建报价草稿的入参。

    ``cost_sheet_locked`` 由上层从 costing 域查得传入——本域不直接
    调 costing（域间零依赖），但必须验证这个前提。
    """

    opportunity_id: str
    currency: str
    valid_until: datetime
    cost_sheet_id: str
    cost_sheet_locked: bool
    lines: list[QuoteLineRequest]
    fx_snapshot_id: str | None = None
    payment_terms_note: str | None = None
    prepared_by: str | None = None


@dataclass(frozen=True)
class QuoteLineView:
    line_number: int
    description: str
    quantity: int
    unit_price: Money
    line_total: Money
    moq: int | None = None
    lead_time_days: int | None = None


@dataclass(frozen=True)
class QuoteView:
    """报价视图。

    ``margin_rate`` 与 ``below_margin_floor`` 只出现在内部/审批视图，
    **绝不能进客户可见渲染**——客户知道你的利润率等于谈判缴械。
    序列化层按视图类型裁剪，不靠前端隐藏。
    """

    quote_id: str
    opportunity_id: str
    version: int
    state: str
    currency: str
    total: Money
    valid_until: datetime
    lines: list[QuoteLineView]
    created_at: datetime
    prepared_by_name: str | None = None
    approved_by_name: str | None = None
    sent_at: datetime | None = None
    customer_feedback: str | None = None
    margin_rate: Decimal | None = None
    below_margin_floor: bool = False


@dataclass(frozen=True)
class QuoteApprovalPackage:
    """报价审批包 —— 审批人看到的全部内容。

    目标：审批人**不打开别的页面**就能做决定。

    字段：
        quote:              报价视图（内部版）
        opportunity_summary
        account_name, country
        margin_rate, margin_floor, below_floor
        cost_sheet_summary
        indicative_risk_note: 若成本表走过 INDICATIVE 风险接受，
                              在此显著标出
        previous_versions:  历史版本摘要（报过什么价、客户什么反应）
    """

    quote: QuoteView
    opportunity_summary: str
    account_name: str
    country: str
    margin_rate: Decimal
    margin_floor: Decimal
    below_floor: bool
    cost_sheet_summary: str
    indicative_risk_note: str | None = None
    previous_versions: list[str] = field(default_factory=list)
