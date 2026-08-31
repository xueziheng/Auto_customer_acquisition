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

from shared.schemas.identifiers import (
    ArtifactId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
)
from shared.schemas.money import CurrencyCode, FxRate, Money
from shared.schemas.provenance import Provenance


def _stored_decimal(value: Decimal, *, precision: int = 28) -> None:
    """HTTP 与实体使用相同的无损 Numeric 输入边界。"""
    from domains.costing.models import _require_numeric_precision
    from shared.errors import ValidationError

    try:
        _require_numeric_precision(value, precision=precision, scale=12, field="金额或比例")
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


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


class SourcingEstimateCreate(BaseModel):
    """可信交接快照形成自动 ESTIMATED 成本表的唯一强类型命令。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    sourcing_case_id: SourcingCaseId
    primary_option_id: SourcingSupplyOptionId
    supplier_candidate_id: SupplierCandidateId | None = None
    product_id: ProductId
    opportunity_id: OpportunityId
    quantity: int = Field(ge=1)
    unit_amount: CostingDecimalInput
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_estimate(self) -> SourcingEstimateCreate:
        """自动成本只接受有限正金额及可无损持久化的精确来源。"""

        _stored_decimal(self.unit_amount)
        if self.unit_amount <= Decimal(0):
            raise ValueError("寻源估算单价必须为正")
        return self


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
        for rate in rates:
            _stored_decimal(rate, precision=18)
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
    source: SourceEvidence | None = None
    field_provenance: dict[str, Provenance] = Field(default_factory=dict)

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
            Money(Decimal(0), CurrencyCode("USD")).round_to(self.unit_places, self.strategy)
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
    item_sequence: int | None = None


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
    source_sourcing_case_id: str | None = None
    source_option_id: str | None = None
    source_product_id: str | None = None
    source_candidate_id: str | None = None
    content_hash: str = ""


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


NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
CurrencyInput = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
ContentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourceEvidence(BaseModel):
    """仅由可信来源 reader 产生的安全元数据；不含 bytes 或存储凭证。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: NonBlank
    source_ref: NonBlank
    artifact_id: NonBlank
    content_hash: ContentHash
    locator: NonBlank
    observed_at: CostingDatetimeInput
    source_type: Literal["upload", "conversation", "web_page", "employee_input", "external_api"]
    source_url: str | None = None

    @model_validator(mode="after")
    def validate_web_source(self) -> SourceEvidence:
        """网页必须保留真实 URL，不能为了省字段冒充上传。"""
        if self.source_type == "web_page" and (
            not self.source_url or not self.source_url.startswith(("https://", "http://"))
        ):
            raise ValueError("网页来源必须有 URL 和内容哈希")
        return self


class _PriceEvidenceCreate(BaseModel):
    """人工价格依据共有字段，不接收任何客户端确认事实。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    opportunity_id: NonBlank
    currency: CurrencyInput
    source_ref: NonBlank
    locator: NonBlank
    amount: CostingDecimalInput

    @model_validator(mode="after")
    def validate_amount(self) -> _PriceEvidenceCreate:
        """确认零费用是允许的，但负值和有损存储一律拒绝。"""
        if self.amount < 0:
            raise ValueError("费用不能为负")
        _stored_decimal(self.amount)
        return self


class SupplierPriceEvidenceCreate(_PriceEvidenceCreate):
    """采购价格按具体规格、计价单位、数量档和有效期确认。"""

    kind: Literal["supplier_price"]
    need_id: NonBlank
    supplier_ref: NonBlank
    specification: NonBlank
    unit: NonBlank
    destination: NonBlank
    basis: Literal["quoted", "indicative"]
    quantity_min: Annotated[int, Field(gt=0)]
    quantity_max: Annotated[int, Field(gt=0)]
    moq: Annotated[int, Field(gt=0)]
    quoted_at: CostingDatetimeInput
    valid_until: CostingDatetimeInput

    @model_validator(mode="after")
    def validate_scope(self) -> SupplierPriceEvidenceCreate:
        """拒绝倒置数量档、MOQ 与有效期，不自动换算量纲。"""
        if self.quantity_max < self.quantity_min or self.moq > self.quantity_min:
            raise ValueError("数量范围与 MOQ 不一致")
        if self.valid_until <= self.quoted_at:
            raise ValueError("价格有效期必须晚于报价时间")
        return self


class ExpenseEvidenceCreate(_PriceEvidenceCreate):
    """费用依据不伪造产品 MOQ，也不把实际凭证重标为 quoted。"""

    kind: Literal["confirmed_expense"]
    item_type: NonBlank
    allocation_scope: NonBlank
    is_per_unit: bool
    quantity: Annotated[int, Field(gt=0)]
    basis: Literal["quoted", "actual"]
    observed_at: CostingDatetimeInput
    valid_until: CostingDatetimeInput | None

    @model_validator(mode="after")
    def validate_expense(self) -> ExpenseEvidenceCreate:
        """费用范围只能属于非采购类型，时间范围必须明确有效。"""
        from domains.costing.models import CostItemType

        if self.item_type not in {item.value for item in CostItemType} - {"product_purchase"}:
            raise ValueError("费用类型无效或混入产品采购")
        if self.valid_until is not None and self.valid_until <= self.observed_at:
            raise ValueError("费用有效期必须晚于观察时间")
        return self


PriceEvidenceCreate = Annotated[
    SupplierPriceEvidenceCreate | ExpenseEvidenceCreate, Field(discriminator="kind")
]


class QuoteFxCreate(BaseModel):
    """独立的核算币种到报价币种直连汇率，绝不改写旧成本汇率。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    base_currency: CurrencyInput
    quote_currency: CurrencyInput
    source_ref: NonBlank
    rate: CostingDecimalInput
    observed_at: CostingDatetimeInput

    @model_validator(mode="after")
    def validate_rate(self) -> QuoteFxCreate:
        """汇率只接受可无损持久化的正 Decimal。"""
        if self.rate <= 0 or self.base_currency == self.quote_currency:
            raise ValueError("报价汇率必须为异币种间的正数")
        _stored_decimal(self.rate)
        return self


def _http_tuple(value: object) -> object:
    """只把 JSON 数组转为不可变序列，不接受任意迭代器。"""
    return tuple(value) if isinstance(value, list) else value


class CostItemBinding(BaseModel):
    """绑定持久明细身份与原文费用行，不能按浏览器数组位置猜测。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    item_sequence: Annotated[int, Field(gt=0)]
    evidence_id: NonBlank
    source_line_ref: NonBlank
    allocation_scope: NonBlank


class CostCoverageDecision(BaseModel):
    """每类费用必须明确适用且有金额，或明确不适用且有理由。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    item_type: NonBlank
    applicable: bool
    reason: str
    item_bindings: Annotated[tuple[CostItemBinding, ...], BeforeValidator(_http_tuple)]

    @model_validator(mode="after")
    def validate_decision(self) -> CostCoverageDecision:
        """缺失不等于零；不适用类型不能附带隐藏的金额绑定。"""
        if self.applicable != bool(self.item_bindings):
            raise ValueError("适用费用必须绑定金额，不适用费用不得绑定金额")
        if not self.applicable and not self.reason.strip():
            raise ValueError("不适用必须给出理由")
        return self


class CostCoverageCreate(BaseModel):
    """绑定当前成本内容的完整场景确认，不改变旧 readiness 语义。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    expected_sheet_hash: ContentHash
    decisions: Annotated[tuple[CostCoverageDecision, ...], BeforeValidator(_http_tuple)]
    acquisition_mode: Literal["summary", "detail"]

    @model_validator(mode="after")
    def validate_coverage(self) -> CostCoverageCreate:
        """22 类逐一覆盖，不允许重复类型挤掉遗漏项。"""
        from domains.costing.models import CostItemType

        expected = {item.value for item in CostItemType}
        if len(self.decisions) != len(expected) or {item.item_type for item in self.decisions} != expected:
            raise ValueError("完整性确认必须恰好覆盖全部22类成本")
        return self


class _ConfirmedEvidence(BaseModel):
    """服务端人工确认事实，与客户端输入在模型上分离。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    source: SourceEvidence
    field_provenance: dict[str, Provenance]
    confirmed_by: NonBlank
    confirmed_at: CostingDatetimeInput


class SupplierPriceEvidenceView(SupplierPriceEvidenceCreate, _ConfirmedEvidence):
    """供应商价格及逐字段确认事实。"""

    evidence_id: NonBlank
    evidence_hash: ContentHash


class ExpenseEvidenceView(ExpenseEvidenceCreate, _ConfirmedEvidence):
    """已确认费用及逐字段确认事实。"""

    evidence_id: NonBlank
    evidence_hash: ContentHash


PriceEvidenceView = Annotated[
    SupplierPriceEvidenceView | ExpenseEvidenceView, Field(discriminator="kind")
]


class QuoteFxView(QuoteFxCreate, _ConfirmedEvidence):
    """报价汇率保留方向、来源与确认事实。"""

    fx_id: NonBlank
    content_hash: ContentHash


class CostCoverageView(CostCoverageCreate):
    """持久场景确认，供后续报价冻结按 hash 读取。"""

    coverage_id: NonBlank
    cost_sheet_id: NonBlank
    content_hash: ContentHash
    confirmed_by: NonBlank
    confirmed_at: CostingDatetimeInput
    field_provenance: dict[str, Provenance]


PricingPolicyView.model_rebuild()

from domains.costing.freeze_schemas import (
    CostingContext as CostingContext,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.freeze_schemas import (
    CostScopeAccess as CostScopeAccess,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.freeze_schemas import (
    CostScopeConfirmationCommand as CostScopeConfirmationCommand,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.freeze_schemas import (
    CostScopeConfirmationView as CostScopeConfirmationView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.freeze_schemas import (
    CostScopeEvidenceBinding as CostScopeEvidenceBinding,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.costing.freeze_schemas import (
    FrozenCostBasis as FrozenCostBasis,  # noqa: PLC0414 - 同类型公开重导出
)

_HTTP_PUBLIC_EXPORTS = frozenset({
    "CostCalculationCommand", "CostCoveragePublicView", "CostScopePublicView",
    "ExpenseEvidencePublicView", "PriceEvidencePublicView", "PricingPolicyPublicView",
    "PricingSourceSummary", "QuoteFxPublicView", "SupplierPriceEvidencePublicView",
})


def __getattr__(name: str) -> object:
    """显式延迟重导出，避免HTTP复用原商业类型时产生schemas循环。"""
    if name in _HTTP_PUBLIC_EXPORTS:
        from domains.costing import http_schemas

        return getattr(http_schemas, name)
    raise AttributeError(name)
