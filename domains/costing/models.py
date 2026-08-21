"""成本域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
)
from shared.schemas.money import Money, PriceBasis


class CostItemType(str, Enum):
    """成本项类型。设计稿第 18 节的完整清单。

    **漏一项成本就是虚增一分利润。** 模型的「遗漏项提醒」对着这张
    清单查：报价含定制 Logo 但成本表没有 ``LOGO_PRINTING`` 项时提醒。
    """

    PRODUCT_PURCHASE = "product_purchase"
    SAMPLE_FEE = "sample_fee"
    MOLD_FEE = "mold_fee"
    CUSTOMIZATION_FEE = "customization_fee"
    LOGO_PRINTING = "logo_printing"
    PACKAGING = "packaging"
    QUALITY_INSPECTION = "quality_inspection"
    WASTAGE = "wastage"
    """损耗预留。按比例估，常被漏掉的一项。"""

    DOMESTIC_FREIGHT = "domestic_freight"
    INTERNATIONAL_FREIGHT = "international_freight"
    INSURANCE = "insurance"
    CUSTOMS_CLEARANCE = "customs_clearance"
    DUTIES_AND_TAXES = "duties_and_taxes"
    """关税和不可抵扣税。目的地国家决定税率，所以需求里的
    ``destination`` 字段不是可有可无的。"""

    DESTINATION_FREIGHT = "destination_freight"
    WAREHOUSING = "warehousing"
    PAYMENT_FEES = "payment_fees"
    SALES_COMMISSION = "sales_commission"
    CUSTOMER_ACQUISITION = "customer_acquisition"
    """获客成本分摊。没有它，「自动获客划不划算」永远算不清。"""

    CONTACT_DATA_COST = "contact_data_cost"
    AD_ALLOCATION = "ad_allocation"
    AGENT_API_ALLOCATION = "agent_api_allocation"
    RETURNS_RESERVE = "returns_reserve"
    """退货与售后预留。"""


class CostSheetVersion(str, Enum):
    ESTIMATED = "estimated"
    """估算。可以用 INDICATIVE 价格，供内部判断方向。"""

    QUOTED = "quoted"
    """报价锁定。要求产品采购价 basis 为 QUOTED，汇率快照绑定。
    **一经关联报价即不可变。**"""

    ACTUAL = "actual"
    """实际发生。事后核算。"""


@dataclass(frozen=True)
class CostItem:
    """一项成本。

    字段：
        item_type
        amount:        金额（Money，Decimal）
        price_basis:   indicative | quoted | actual —— 硬边界 7 的
                       判定依据在这里逐项记录
        is_per_unit:   单件成本还是整单成本
        note
        source_ref:    价格快照或人工录入的引用
        entered_by:    录入人。**模型建议的成本项必须经人确认后才有
                       这个字段**，未确认的建议不参与计算
    """

    item_type: CostItemType
    amount: Money
    price_basis: str
    is_per_unit: bool
    note: str | None = None
    source_ref: str | None = None
    entered_by: EmployeeId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.item_type, CostItemType):
            raise ValidationError("成本项类型无效")
        if not isinstance(self.amount, Money) or self.amount.amount < 0:
            raise ValidationError("成本项金额不能为负")
        if self.price_basis not in {
            PriceBasis.INDICATIVE,
            PriceBasis.QUOTED,
            CostSheetVersion.ACTUAL.value,
        }:
            raise ValidationError("成本项价格基准无效")
        if not isinstance(self.is_per_unit, bool):
            raise ValidationError("成本项计价方式无效")
        if self.entered_by is not None and (
            not isinstance(self.source_ref, str) or not self.source_ref.strip()
        ):
            raise ValidationError("人工确认成本项必须有来源引用")


@dataclass
class CostSheet:
    """成本表。

    字段：
        cost_sheet_id, tenant_id, opportunity_id
        version_type:   三版本之一
        version_number: 同类型内的版本号
        quantity:       计算基准数量（阶梯价场景一档一张表）
        items
        fx_snapshot_id: 汇率快照（QUOTED 版本必填）
        base_currency:  内部核算币种
        quote_currency: 客户报价币种
        created_at, created_by
        locked_at:      锁定时间，非 None 后不可修改
        risk_acceptance: INDICATIVE 基准被人工放行时的记录
    """

    cost_sheet_id: CostSheetId
    tenant_id: TenantId
    opportunity_id: OpportunityId
    version_type: CostSheetVersion
    version_number: int
    quantity: int
    base_currency: str
    quote_currency: str
    created_at: datetime
    items: list[CostItem] = field(default_factory=list)
    fx_snapshot_id: FxSnapshotId | None = None
    created_by: EmployeeId | None = None
    locked_at: datetime | None = None
    risk_acceptance: RiskAcceptance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.version_type, CostSheetVersion):
            raise ValidationError("成本表版本类型无效")
        if (
            isinstance(self.version_number, bool)
            or not isinstance(self.version_number, int)
            or self.version_number <= 0
        ):
            raise ValidationError("成本表版本号必须为正整数")
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or self.quantity <= 0
        ):
            raise ValidationError("成本表数量必须为正整数")
        for currency in (self.base_currency, self.quote_currency):
            if (
                not isinstance(currency, str)
                or len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or not currency.isupper()
            ):
                raise ValidationError("成本表币种无效")
        if self.created_at.utcoffset() is None:
            raise ValidationError("成本表创建时间必须含时区")
        if not isinstance(self.items, list) or any(
            not isinstance(item, CostItem) for item in self.items
        ):
            raise ValidationError("成本表项目无效")
        if self.version_type is CostSheetVersion.QUOTED and (
            not isinstance(self.fx_snapshot_id, str) or not self.fx_snapshot_id.strip()
        ):
            raise ValidationError("QUOTED 成本表必须绑定汇率快照")
        if self.locked_at is not None and (
            self.locked_at.utcoffset() is None or self.locked_at < self.created_at
        ):
            raise ValidationError("成本表锁定时间无效")
        if self.risk_acceptance is not None and not isinstance(
            self.risk_acceptance, RiskAcceptance
        ):
            raise ValidationError("参考价风险接受记录无效")

    def has_indicative_items(self) -> bool:
        """是否含参考价成本项。

        True 且无 ``risk_acceptance`` 时，这张表不能支撑客户可见报价
        （硬边界 7）。
        """
        return any(
            item.entered_by is not None
            and item.price_basis == PriceBasis.INDICATIVE
            for item in self.items
        )

    def missing_item_types(self, expected: list[CostItemType]) -> list[CostItemType]:
        """对照期望清单找漏项。

        ``expected`` 由调用方按业务场景给（例如含定制则应有
        CUSTOMIZATION_FEE 与 LOGO_PRINTING）。模型的遗漏提醒
        建立在这个确定性检查之上——模型提出「可能漏了什么」，
        这个方法验证「确实没有」。
        """
        confirmed = {
            item.item_type for item in self.items if item.entered_by is not None
        }
        missing: list[CostItemType] = []
        seen: set[CostItemType] = set()
        for item_type in expected:
            if item_type not in confirmed and item_type not in seen:
                missing.append(item_type)
                seen.add(item_type)
        return missing


@dataclass(frozen=True)
class RiskAcceptance:
    """INDICATIVE 价格被人工放行的记录。

    硬边界 7 的唯一例外通道。三个字段都必填——「谁、何时、为什么」
    缺一个，事后追责就没有依据。

    字段：
        accepted_by, accepted_at
        justification: 为什么接受（自由文本，必填非空）
    """

    accepted_by: EmployeeId
    accepted_at: datetime
    justification: str

    def __post_init__(self) -> None:
        if not isinstance(self.accepted_by, str) or not self.accepted_by.strip():
            raise ValidationError("风险接受人无效")
        if self.accepted_at.utcoffset() is None:
            raise ValidationError("风险接受时间必须含时区")
        if not isinstance(self.justification, str) or not self.justification.strip():
            raise ValidationError("风险接受理由不能为空")


@dataclass(frozen=True)
class CostBreakdown:
    """确定性计算结果。全部 Decimal。

    字段：
        unit_full_cost:        单位完整成本
        minimum_sellable_price: 最低可售价（成本 + 利润底线）
        target_price:          目标售价
        gross_profit:          毛利
        contribution_profit:   贡献利润
        margin_rate:           利润率
        max_discount:          最大允许折扣
        max_acquisition_cost:  最大允许获客成本
        computed_at
        cost_sheet_id, cost_sheet_version
        inputs_hash:           输入哈希。同一张表同一规则算两次结果
                               必须一致，哈希用于验证这一点
    """

    unit_full_cost: Money
    minimum_sellable_price: Money
    target_price: Money
    gross_profit: Money
    contribution_profit: Money
    margin_rate: Decimal
    max_discount: Decimal
    max_acquisition_cost: Money
    computed_at: datetime
    cost_sheet_id: CostSheetId
    cost_sheet_version: int
    inputs_hash: str


@dataclass(frozen=True)
class MarginRule:
    """利润规则。

    字段：
        tenant_id
        category:          适用品类（None = 全局默认）
        minimum_margin_rate: 利润率底线
        target_margin_rate
        effective_from

    低于底线的报价必须走审批，且**不能由机会负责人自批**
    （见 ``domains/approvals``）。
    """

    tenant_id: TenantId
    minimum_margin_rate: Decimal
    target_margin_rate: Decimal
    effective_from: datetime
    category: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise ValidationError("利润规则租户无效")
        rates = (self.minimum_margin_rate, self.target_margin_rate)
        if any(
            not isinstance(rate, Decimal) or not rate.is_finite() for rate in rates
        ):
            raise ValidationError("利润率必须是有限 Decimal")
        if not (
            Decimal(0)
            <= self.minimum_margin_rate
            <= self.target_margin_rate
            < Decimal(1)
        ):
            raise ValidationError("利润率必须满足 0 ≤ 底线 ≤ 目标 < 1")
        if self.effective_from.utcoffset() is None:
            raise ValidationError("利润规则生效时间必须含时区")
        if self.category is not None and (
            not isinstance(self.category, str) or not self.category.strip()
        ):
            raise ValidationError("利润规则品类无效")
