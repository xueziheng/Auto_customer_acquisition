"""成本报价HTTP安全白名单；来源摘要不授予原件读取权。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from domains.costing.freeze_schemas import CostScopeEvidenceBinding, _FreezeDTO
from domains.costing.schemas import (
    ContentHash,
    CostCoverageDecision,
    CostingDatetimeInput,
    CostingDecimalInput,
    CurrencyInput,
    NonBlank,
    PricingOptions,
    RoundingPolicy,
    _http_tuple,
)
from shared.schemas.identifiers import CostSheetId, OpportunityId, ValidatedNeedId
from shared.schemas.money import Money
from shared.schemas.provenance import ProvenanceSummary
from shared.schemas.quote_creation import QuoteTerm, QuoteText, QuoteTime


class _PublicDTO(BaseModel):
    """只承载明确字段，不接受额外或隐式类型强转。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class PricingSourceSummary(_PublicDTO):
    """来源身份及hash，不含原文、URL或locator。"""

    source_ref: NonBlank
    artifact_id: NonBlank
    content_hash: ContentHash
    source_type: Literal[
        "upload", "conversation", "web_page", "employee_input", "external_api"
    ]
    observed_at: CostingDatetimeInput


class PricingPolicyPublicView(_PublicDTO):
    """确认政策的原值与来源摘要，不改变历史政策可读性。"""

    policy_id: str
    content_hash: ContentHash
    category: str | None
    minimum_margin_rate: CostingDecimalInput
    target_margin_rate: CostingDecimalInput
    cost_groups: dict[str, Literal["goods", "variable", "fixed"]]
    effective_from: CostingDatetimeInput
    confirmed_by: str
    confirmed_at: CostingDatetimeInput
    source: PricingSourceSummary | None
    field_provenance: dict[str, ProvenanceSummary]


class _PriceEvidencePublicView(_PublicDTO):
    """两类依据只共享真实共同字段，不拼造另一类业务属性。"""

    evidence_id: NonBlank
    evidence_hash: ContentHash
    opportunity_id: NonBlank
    currency: CurrencyInput
    amount: CostingDecimalInput
    confirmed_by: NonBlank
    confirmed_at: CostingDatetimeInput
    source: PricingSourceSummary
    field_provenance: dict[str, ProvenanceSummary]


class SupplierPriceEvidencePublicView(_PriceEvidencePublicView):
    """供应商自由规格与真实数量档保持原值。"""

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


class ExpenseEvidencePublicView(_PriceEvidencePublicView):
    """费用依据保留actual和可空有效期，不伪造采购单位。"""

    kind: Literal["confirmed_expense"]
    item_type: NonBlank
    allocation_scope: NonBlank
    is_per_unit: bool
    quantity: Annotated[int, Field(gt=0)]
    basis: Literal["quoted", "actual"]
    observed_at: CostingDatetimeInput
    valid_until: CostingDatetimeInput | None


PriceEvidencePublicView = Annotated[
    SupplierPriceEvidencePublicView | ExpenseEvidencePublicView,
    Field(discriminator="kind"),
]


class QuoteFxPublicView(_PublicDTO):
    """人工直连报价汇率及原方向，不重新计算。"""

    fx_id: NonBlank
    content_hash: ContentHash
    base_currency: CurrencyInput
    quote_currency: CurrencyInput
    rate: CostingDecimalInput
    observed_at: CostingDatetimeInput
    confirmed_by: NonBlank
    confirmed_at: CostingDatetimeInput
    source: PricingSourceSummary
    field_provenance: dict[str, ProvenanceSummary]


class CostCoveragePublicView(_PublicDTO):
    """完整确认身份与全部22类决策，不包含来源原文。"""

    coverage_id: NonBlank
    cost_sheet_id: NonBlank
    content_hash: ContentHash
    expected_sheet_hash: ContentHash
    decisions: Annotated[tuple[CostCoverageDecision, ...], BeforeValidator(_http_tuple)]
    acquisition_mode: Literal["summary", "detail"]
    confirmed_by: NonBlank
    confirmed_at: CostingDatetimeInput
    field_provenance: dict[str, ProvenanceSummary]


class CostScopePublicView(_FreezeDTO):
    """历史适用性确认摘要，不能凭此宣称当前需求仍适用。"""

    confirmation_id: str
    opportunity_id: OpportunityId
    need_id: ValidatedNeedId
    cost_sheet_id: CostSheetId
    sheet_hash: ContentHash
    coverage_id: str
    coverage_hash: ContentHash
    need_facts_hash: ContentHash
    specification: QuoteText
    specification_hash: ContentHash
    terms: tuple[QuoteTerm, ...]
    terms_hash: ContentHash
    valid_until: QuoteTime
    evidence_bindings: tuple[CostScopeEvidenceBinding, ...]
    content_hash: ContentHash
    provenance: ProvenanceSummary


class CostCalculationCommand(_PublicDTO):
    """HTTP只能选择持久FX引用，不能提交客户端FxRate或计算结果。"""

    mode: Literal["target", "manual"]
    unit_price: Money | None
    rounding: RoundingPolicy
    quote_fx_ref: str | None
    algorithm_version: Literal["costing-v1"]

    @model_validator(mode="after")
    def _mode(self) -> Self:
        """复用原计价模式校验，不复制金额规则。"""
        PricingOptions(
            mode=self.mode,
            unit_price=self.unit_price,
            rounding=self.rounding,
            quote_fx=None,
            algorithm_version=self.algorithm_version,
        )
        return self
