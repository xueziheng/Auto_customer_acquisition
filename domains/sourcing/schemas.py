"""寻源域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from shared.schemas.identifiers import (
    ArtifactId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    ValidatedNeedId,
)
from shared.schemas.money import Money
from shared.schemas.provenance import ProvenanceSummary, SourceType


def _bounded_text(value: str, *, field_name: str, maximum: int = 2_000) -> str:
    """校验公共合同中的非空、无首尾空白文本。"""

    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} 必须是非空且无首尾空白的文本")
    return value


class NeedFact(BaseModel):
    """客户已确认需求中的单项事实；推断不得伪装成事实。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    value: str | int | date
    provenance: ProvenanceSummary

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        """事实必须有可追溯来源，且不得来自 Agent 推断。"""

        if self.provenance.source_type is SourceType.AGENT_INFERENCE:
            raise ValueError("NeedFact 不得使用 AGENT_INFERENCE 来源")
        if isinstance(self.value, str):
            _bounded_text(self.value, field_name="需求事实")
        return self


class SourcingNeedSnapshot(BaseModel):
    """开案时冻结的已验证需求快照；关键字段逐项保留 Provenance。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    need_id: ValidatedNeedId
    completeness: int = Field(ge=0, le=5)
    derivation_version: Literal["need-completeness-v1"]
    product_category: NeedFact
    application: NeedFact | None = None
    material: NeedFact | None = None
    size_spec: NeedFact | None = None
    quantity: NeedFact
    unit: NeedFact | None = None
    destination: NeedFact | None = None
    required_by: NeedFact | None = None
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class OpenSourcingCase(BaseModel):
    """V2 开案命令；租户与 actor 只由可信服务上下文绑定。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    need: SourcingNeedSnapshot
    workflow_version: Literal[2] = 2
    trigger_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_trigger_key(self) -> Self:
        """稳定业务幂等键必须无首尾空白。"""

        _bounded_text(self.trigger_key, field_name="trigger_key", maximum=200)
        return self


class PublicSourcingPlanCommand(BaseModel):
    """待老板确认的公开寻源精确范围，不包含请求身份或凭证。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    plan_id: SourcingPlanId
    case_id: SourcingCaseId
    target_countries: tuple[str, ...] = Field(min_length=1)
    product_category: str = Field(min_length=1, max_length=200)
    queries: tuple[str, ...] = Field(min_length=1)
    max_search_queries: int = Field(ge=1)
    max_pages_read: int = Field(ge=1)
    provider: Literal["tavily"]
    search_depth: Literal["basic"] = "basic"
    usage_credits_remaining: int = Field(ge=0)
    worst_case_credits: int = Field(ge=1)
    version: int = Field(ge=1)
    expected_case_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        """计划边界必须确定、去重，并与查询上限一致。"""

        _bounded_text(self.product_category, field_name="product_category", maximum=200)
        for country in self.target_countries:
            if len(country) != 2 or not country.isascii() or not country.isupper():
                raise ValueError("target_countries 必须使用两位大写国家代码")
        if len(set(self.target_countries)) != len(self.target_countries):
            raise ValueError("target_countries 不得重复")
        for query in self.queries:
            _bounded_text(query, field_name="query", maximum=500)
        if len(set(self.queries)) != len(self.queries):
            raise ValueError("queries 不得重复")
        if len(self.queries) > self.max_search_queries:
            raise ValueError("queries 数量不得超过 max_search_queries")
        return self


class SourcingReviewCommand(BaseModel):
    """人工审核提交；仅保存选择，不接受机会或成本字段自证。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    primary_option_id: SourcingSupplyOptionId
    alternate_option_ids: tuple[SourcingSupplyOptionId, ...] = Field(max_length=2)
    reason: str = Field(min_length=1, max_length=2_000)
    expected_case_version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        """强制一个非空主选、至多两个互异且不含主选的备选。"""

        _bounded_text(str(self.primary_option_id), field_name="primary_option_id", maximum=200)
        _bounded_text(self.reason, field_name="reason")
        alternate_values = tuple(str(item) for item in self.alternate_option_ids)
        if any(not item or item != item.strip() for item in alternate_values):
            raise ValueError("alternate_option_ids 不得包含空 ID")
        if len(set(alternate_values)) != len(alternate_values):
            raise ValueError("alternate_option_ids 不得重复")
        if str(self.primary_option_id) in alternate_values:
            raise ValueError("主选不得同时出现在备选中")
        return self


class SourcingCostPriceOption(BaseModel):
    """可交给成本域的单个数量档；价格语义始终为 indicative。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    minimum_quantity: int = Field(ge=1)
    unit_amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: str = Field(min_length=1, max_length=50)
    evidence_ref: ArtifactId
    source_kind: Literal["existing_product", "supplier_candidate"]

    @model_validator(mode="after")
    def validate_price(self) -> Self:
        """金额必须有限且为正，来源与计价单位必须明确。"""

        if not self.unit_amount.is_finite() or self.unit_amount <= Decimal(0):
            raise ValueError("unit_amount 必须是有限正 Decimal")
        _bounded_text(self.unit, field_name="unit", maximum=50)
        _bounded_text(str(self.evidence_ref), field_name="evidence_ref", maximum=200)
        return self


class SourcingHandoffSnapshot(BaseModel):
    """成本交接的不可变规范化输入，兼容现有产品与供应商候选两条路径。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    case_id: SourcingCaseId
    review_id: SourcingReviewId
    need_id: ValidatedNeedId
    opportunity_id: OpportunityId
    primary_option_id: SourcingSupplyOptionId
    product_id: ProductId
    supplier_candidate_id: SupplierCandidateId | None = None
    quantity: int = Field(ge=1)
    moq: int = Field(ge=1)
    price_options: tuple[SourcingCostPriceOption, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_price_dimensions_and_path(self) -> Self:
        """拒绝数量档和计价维度歧义，并绑定候选 ID 与来源类型。"""

        minimums = [option.minimum_quantity for option in self.price_options]
        if len(set(minimums)) != len(minimums):
            raise ValueError("price_options 不得重复 minimum_quantity")
        if len({option.currency for option in self.price_options}) != 1:
            raise ValueError("price_options 必须使用同一币种")
        if len({option.unit for option in self.price_options}) != 1:
            raise ValueError("price_options 必须使用同一计价单位")
        source_kinds = {option.source_kind for option in self.price_options}
        if len(source_kinds) != 1:
            raise ValueError("price_options 不得混合 source_kind")
        expected_source = "supplier_candidate" if self.supplier_candidate_id is not None else "existing_product"
        if source_kinds != {expected_source}:
            raise ValueError("supplier_candidate_id 与 source_kind 不一致")
        return self


class SourcingObservedFact(BaseModel):
    """页面明确出现的字段事实；每项都绑定证据摘要与 Artifact。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    value: str | int | date
    provenance: ProvenanceSummary
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_observed_fact(self) -> Self:
        """观察事实不得使用 Agent 推断来源。"""

        if self.provenance.source_type is SourceType.AGENT_INFERENCE:
            raise ValueError("observed_facts 不得使用 AGENT_INFERENCE 来源")
        return self


class SourcingSupplierClaim(BaseModel):
    """供应商或目录自述；与观察事实分开保存。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    value: str | int | date
    provenance: ProvenanceSummary
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_supplier_claim(self) -> Self:
        """供应商自述必须来自可追溯来源，不能由 Agent 代写成事实。"""

        if self.provenance.source_type is SourceType.AGENT_INFERENCE:
            raise ValueError("supplier_claims 不得使用 AGENT_INFERENCE 来源")
        return self


class SourcingMatchInference(BaseModel):
    """员工或 Agent 的匹配推断；必须列出所依据的 Artifact。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    value: str
    based_on: tuple[ArtifactId, ...] = Field(min_length=1)
    inferred_by: str = Field(min_length=1, max_length=200)
    inferred_at: AwareDatetime

    @model_validator(mode="after")
    def validate_inference(self) -> Self:
        """拒绝空推断和重复证据引用。"""

        _bounded_text(self.value, field_name="match inference")
        _bounded_text(self.inferred_by, field_name="inferred_by", maximum=200)
        if len(set(self.based_on)) != len(self.based_on):
            raise ValueError("based_on 不得重复")
        return self


class SpecComparisonView(BaseModel):
    """逐项匹配结果的公共严格形状。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    spec_name: str
    required: str
    offered: str | None
    level: str
    substitutable: bool | None = None
    substitution_impact: str | None = None
    needs_customer_confirmation: bool = False


class CandidateSubmission(BaseModel):
    """V2 候选写命令；价格只接受 ``indicative_price_tiers``。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    supplier_name: str = Field(min_length=1, max_length=300)
    product_title: str = Field(min_length=1, max_length=500)
    source_platform: str | None
    specs: tuple[SpecComparisonView, ...]
    observed_facts: dict[str, SourcingObservedFact] = Field(default_factory=dict)
    supplier_claims: dict[str, SourcingSupplierClaim] = Field(default_factory=dict)
    match_inferences: dict[str, SourcingMatchInference] = Field(default_factory=dict)
    indicative_price_tiers: dict[int, Money]
    moq: int | None
    price_unit: str | None
    currency: str | None
    evidence_url: str
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_ref: str
    verified_by: str | None = None

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        """候选写入口拒绝模糊数量档和缺失证据。"""

        _bounded_text(self.supplier_name, field_name="supplier_name", maximum=300)
        _bounded_text(self.product_title, field_name="product_title", maximum=500)
        _bounded_text(self.evidence_url, field_name="evidence_url")
        _bounded_text(self.evidence_artifact_ref, field_name="evidence_artifact_ref", maximum=200)
        if not self.indicative_price_tiers:
            raise ValueError("indicative_price_tiers 不得为空")
        if any(isinstance(quantity, bool) or quantity < 1 for quantity in self.indicative_price_tiers):
            raise ValueError("indicative_price_tiers 数量档必须为正整数")
        currencies = {str(price.currency) for price in self.indicative_price_tiers.values()}
        if self.currency is None or currencies != {self.currency}:
            raise ValueError("indicative_price_tiers 与 currency 必须一致")
        return self


@dataclass(frozen=True)
class CandidateView:
    """旧读 DTO；``quoted_prices`` 仅为只读兼容投影。"""

    candidate_id: str
    supplier_name: str
    product_title: str
    source_platform: str | None
    specs: list[SpecComparisonView]
    quoted_prices: dict[int, Money]
    match_summary: str | None
    moq: int | None = None
    price_unit: str | None = None
    currency: str | None = None
    rejected: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    evidence_url: str | None = None
    price_basis: str = "indicative"


@dataclass(frozen=True)
class CaseView:
    case_id: str
    need_id: str
    state: str
    opened_at: datetime
    ladder_checked_to: int | None = None
    candidates: list[CandidateView] = field(default_factory=list)
    qualified_count: int = 0
    assigned_to_name: str | None = None
    failed_reason: str | None = None
    completed_at: datetime | None = None
