"""完整冻结成本的本域投影；不引用costing内部类型或复算其hash。"""

from copy import deepcopy
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from shared.schemas.identifiers import CostSheetId, EmployeeId, OpportunityId, TenantId, ValidatedNeedId
from shared.schemas.money import FxRate, Money, WireDecimal
from shared.schemas.provenance import Provenance
from shared.schemas.quote_creation import QuoteDTO, QuoteRoundingInput, QuoteTerm, QuoteText, QuoteTime
from shared.schemas.quote_facts import FactHash, NeedQuoteFacts, fact_identity, fact_text

Hash = FactHash
Positive = Annotated[int, Field(gt=0)]


class BasisDTO(QuoteDTO):
    """上游coverage允许64位身份；嵌套可变载荷防御拷贝。"""

    @model_validator(mode="after")
    def _identities(self) -> Self:
        """保持T3B身份形状，同时不保留调用方dict引用。"""
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is not None and (name.endswith("_id") or name in {"prepared_by", "confirmed_by"}):
                if name == "coverage_id":
                    fact_text(value)
                    if len(value) > 64:
                        raise ValueError("完整性身份过长")
                else:
                    fact_identity(value)
            object.__setattr__(self, name, deepcopy(value))
        return self


class QuoteEvidenceSource(BasisDTO):
    """来源事实不是对原件访问的授权。"""
    tenant_id: TenantId
    source_ref: QuoteText
    artifact_id: str
    content_hash: Hash
    locator: QuoteText
    observed_at: QuoteTime
    source_type: Literal["upload", "conversation", "web_page", "employee_input", "external_api"]
    source_url: QuoteText | None


class QuoteEvidenceConfirmation(BasisDTO):
    """逐字段原始确认事实，不伪造来源。"""
    source: QuoteEvidenceSource
    field_provenance: dict[str, Provenance]
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime


class QuoteSupplierEvidence(QuoteEvidenceConfirmation):
    """供应商具体规格与数量档实报价。"""
    kind: Literal["supplier_price"]
    tenant_id: TenantId
    opportunity_id: OpportunityId
    evidence_id: str
    evidence_hash: Hash
    source_ref: QuoteText
    locator: QuoteText
    amount: Money
    need_id: ValidatedNeedId
    supplier_ref: QuoteText
    specification: QuoteText
    unit: QuoteText
    destination: QuoteText
    basis: Literal["quoted", "indicative"]
    quantity_min: Positive
    quantity_max: Positive
    moq: Positive
    quoted_at: QuoteTime
    valid_until: QuoteTime


class QuoteExpenseEvidence(QuoteEvidenceConfirmation):
    """费用真实凭证不冒充供应商报价。"""
    kind: Literal["confirmed_expense"]
    tenant_id: TenantId
    opportunity_id: OpportunityId
    evidence_id: str
    evidence_hash: Hash
    source_ref: QuoteText
    locator: QuoteText
    amount: Money
    item_type: QuoteText
    allocation_scope: QuoteText
    is_per_unit: bool
    quantity: Positive
    basis: Literal["quoted", "actual"]
    observed_at: QuoteTime
    valid_until: QuoteTime | None


QuotePriceEvidence = Annotated[QuoteSupplierEvidence | QuoteExpenseEvidence, Field(discriminator="kind")]


class QuoteFxSnapshot(QuoteEvidenceConfirmation):
    """报价汇率与成本内核算汇率分开保存。"""
    fx_id: str
    content_hash: Hash
    base_currency: QuoteText
    quote_currency: QuoteText
    source_ref: QuoteText
    rate: WireDecimal
    observed_at: QuoteTime


class QuotePolicySnapshot(BasisDTO):
    """完整人工政策，不从来源文本推断权限。"""
    policy_id: str
    content_hash: Hash
    category: QuoteText | None
    minimum_margin_rate: WireDecimal
    target_margin_rate: WireDecimal
    cost_groups: dict[str, Literal["goods", "variable", "fixed"]]
    effective_from: QuoteTime
    source_ref: QuoteText
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime
    source: QuoteEvidenceSource | None
    field_provenance: dict[str, Provenance]


class QuoteCostItemBinding(BasisDTO):
    """每项成本的原文与分摊范围绑定。"""
    item_sequence: Positive
    evidence_id: str
    source_line_ref: QuoteText
    allocation_scope: QuoteText


class QuoteCoverageDecision(BasisDTO):
    """保持全部适用/不适用决定和理由。"""
    item_type: QuoteText
    applicable: bool
    reason: QuoteText
    item_bindings: tuple[QuoteCostItemBinding, ...]


class QuoteCoverageSnapshot(BasisDTO):
    """人工完整性确认原快照。"""
    expected_sheet_hash: Hash
    decisions: tuple[QuoteCoverageDecision, ...]
    acquisition_mode: Literal["summary", "detail"]
    coverage_id: str
    cost_sheet_id: CostSheetId
    content_hash: Hash
    confirmed_by: EmployeeId
    confirmed_at: QuoteTime
    field_provenance: dict[str, Provenance]


class QuoteScopeEvidenceBinding(BasisDTO):
    """人工规格语义适用性说明，不比较供应商自由文本相等。"""
    evidence_id: str
    evidence_hash: Hash
    applicability_note: QuoteText


class QuoteScopeConfirmation(BasisDTO):
    """完整Need、条款、期限和证据的人工确认。"""
    tenant_id: TenantId
    confirmation_id: str
    opportunity_id: OpportunityId
    need_id: ValidatedNeedId
    cost_sheet_id: CostSheetId
    sheet_hash: Hash
    coverage_id: str
    coverage_hash: Hash
    need_facts: NeedQuoteFacts
    need_facts_hash: Hash
    specification: QuoteText
    specification_hash: Hash
    terms: tuple[QuoteTerm, ...]
    terms_hash: Hash
    valid_until: QuoteTime
    evidence_bindings: tuple[QuoteScopeEvidenceBinding, ...]
    content_hash: Hash
    provenance: Provenance


class QuoteProfitMetrics(BasisDTO):
    """成本域确定性计算结果；报价域不重算利润。"""
    unit_full_cost: WireDecimal
    minimum_price: WireDecimal
    target_price: WireDecimal
    gross_profit: WireDecimal
    contribution_profit: WireDecimal
    full_cost_profit: WireDecimal
    margin_rate: WireDecimal
    discount_headroom: WireDecimal
    additional_acquisition_headroom: WireDecimal


class QuoteCalculationSnapshot(BasisDTO):
    """完整计算身份及展示金额。"""
    cost_sheet_id: CostSheetId
    policy_id: str
    inputs_hash: Hash
    context_hash: Hash
    version_number: Positive
    computed_at: QuoteTime
    base_currency: QuoteText
    quote_currency: QuoteText
    metrics: QuoteProfitMetrics
    effective_unit_revenue: Money
    displayed_unit_price: Money
    displayed_total: Money


class QuotePricingOptions(BasisDTO):
    """人工售价和明确舍入规则原样保存。"""
    mode: Literal["target", "manual"]
    unit_price: Money | None
    rounding: QuoteRoundingInput
    quote_fx: FxRate | None
    algorithm_version: Literal["costing-v1"]


class QuoteBasis(BasisDTO):
    """冻结全字段等值投影，真实性由冻结操作与持久绑定证明。"""
    tenant_id: TenantId
    basis_id: str
    operation_id: str
    request_hash: Hash
    opportunity_id: OpportunityId
    cost_sheet_id: CostSheetId
    context_hash: Hash
    sheet_hash: Hash
    basis_hash: Hash
    policy_id: str
    quantity: Positive
    specification: QuoteText
    unit: QuoteText
    destination: QuoteText
    need_facts: NeedQuoteFacts
    scope_confirmation: QuoteScopeConfirmation
    policy: QuotePolicySnapshot
    coverage: QuoteCoverageSnapshot
    calculation: QuoteCalculationSnapshot
    price_evidence: tuple[QuotePriceEvidence, ...]
    pricing_options: QuotePricingOptions
    cost_fx_rates: tuple[FxRate, ...]
    quote_fx: QuoteFxSnapshot | None
    valid_until: QuoteTime
    frozen_at: QuoteTime
