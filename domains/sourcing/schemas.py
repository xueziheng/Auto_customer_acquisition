"""寻源域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from shared.events.catalog import SourcingCandidatesVerified
from shared.schemas.identifiers import (
    ArtifactId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money, WireDecimal
from shared.schemas.provenance import ProvenanceSummary, SourceType


def _bounded_text(value: str, *, field_name: str, maximum: int = 2_000) -> str:
    """校验公共合同中的非空、无首尾空白文本。"""

    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} 必须是非空且无首尾空白的文本")
    return value


def _validate_provenance_summary(value: ProvenanceSummary) -> None:
    """在业务 wrapper 边界拒绝不可定位的安全来源摘要。"""

    _bounded_text(value.source_id, field_name="source_id", maximum=200)
    _bounded_text(value.extracted_by, field_name="extracted_by", maximum=200)


class NeedFact(BaseModel):
    """客户已确认需求中的单项事实；推断不得伪装成事实。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    value: str | int | date
    provenance: ProvenanceSummary

    @model_validator(mode="after")
    def validate_fact(self) -> Self:
        """事实必须有可追溯来源，且不得来自 Agent 推断。"""

        _validate_provenance_summary(self.provenance)
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
    model: NeedFact | None = None
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


class PublicSourcingQuery(BaseModel):
    """老板确认的单条公开寻源查询及其精确国家边界。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    query_text: str = Field(min_length=1, max_length=400)
    target_country: str = Field(pattern=r"^[A-Z]{2}$")

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        """查询正文不得依赖隐式修剪或控制字符。"""

        _bounded_text(self.query_text, field_name="query_text", maximum=400)
        if len(self.query_text.split()) > 50:
            raise ValueError("query_text 不得超过 50 个词")
        return self


class PublicCandidateDraftSpec(BaseModel):
    """公开页面中可安全持久化的单项观察值。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    spec_name: str = Field(min_length=1, max_length=100)
    required: str = Field(min_length=1, max_length=2_000)
    observed: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def validate_spec(self) -> Self:
        _bounded_text(self.spec_name, field_name="spec_name", maximum=100)
        _bounded_text(self.required, field_name="required")
        if self.observed is not None:
            _bounded_text(self.observed, field_name="observed", maximum=4_000)
        return self


class PublicCandidateDraftPriceTier(BaseModel):
    """仅保存已完整解析的参考价格档，不保存原文。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    minimum_quantity: int = Field(ge=1)
    amount: WireDecimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_tier(self) -> Self:
        if not self.amount.is_finite() or self.amount <= Decimal(0):
            raise ValueError("amount 必须是有限正 Decimal")
        _bounded_text(self.unit, field_name="unit", maximum=50)
        return self


class PublicCandidateDraft(BaseModel):
    """搜索步产生的 tenant-bound 校准草稿；尚不是供应商候选。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    draft_id: str = Field(min_length=1, max_length=40)
    tenant_id: TenantId = Field(min_length=1, max_length=40)
    case_id: SourcingCaseId
    run_id: RunId
    plan_id: SourcingPlanId
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_index: int = Field(ge=0)
    result_index: int = Field(ge=0)
    source_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    supplier_name: str | None = Field(default=None, max_length=300)
    product_title: str | None = Field(default=None, max_length=500)
    specs: tuple[PublicCandidateDraftSpec, ...]
    moq: int | None = Field(default=None, ge=1)
    indicative_price_tiers: tuple[PublicCandidateDraftPriceTier, ...]
    rejection_codes: tuple[
        Literal[
            "supplier_identity_missing",
            "product_identity_missing",
            "quantity_tier_missing",
            "unit_unclear",
            "currency_unclear",
            "vague_range",
        ],
        ...,
    ]
    evidence_url: str = Field(min_length=1, max_length=2_000)
    evidence_observed_at: AwareDatetime
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_ref: ArtifactId
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        for field_name in ("draft_id", "tenant_id", "evidence_url"):
            _bounded_text(str(getattr(self, field_name)), field_name=field_name)
        for field_name in ("supplier_name", "product_title"):
            value = getattr(self, field_name)
            if value is not None:
                _bounded_text(value, field_name=field_name, maximum=500)
        names = tuple(item.spec_name.casefold() for item in self.specs)
        if len(names) != len(set(names)):
            raise ValueError("specs 不得重复")
        if len(self.rejection_codes) != len(set(self.rejection_codes)):
            raise ValueError("rejection_codes 不得重复")
        if (self.supplier_name is None) != (
            "supplier_identity_missing" in self.rejection_codes
        ):
            raise ValueError("供应商身份拒绝码不一致")
        if (self.product_title is None) != (
            "product_identity_missing" in self.rejection_codes
        ):
            raise ValueError("产品身份拒绝码不一致")
        return self

    @property
    def is_verification_complete(self) -> bool:
        """草稿只有完整身份和价格维度时才能进入 Task 10B 核验。"""

        return (
            self.supplier_name is not None
            and self.product_title is not None
            and bool(self.indicative_price_tiers)
            and not self.rejection_codes
        )


class VerifyPublicCandidateDraftsCommand(BaseModel):
    """把公开搜索输出的精确有序草稿 generation 绑定到一次核验。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    run_id: RunId
    plan_id: SourcingPlanId
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draft_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_draft_ids(self) -> Self:
        """草稿顺序属于命令内容，重复或不可定位 ID 一律拒绝。"""

        for draft_id in self.draft_ids:
            _bounded_text(draft_id, field_name="draft_id", maximum=40)
        if len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError("draft_ids 不得重复")
        return self


class VerifyPublicCandidateDraftsResult(BaseModel):
    """公开草稿核验结果；校准草稿、候选与封存 generation 分栏。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    calibration_draft_ids: tuple[str, ...]
    converted_candidate_ids: tuple[SupplierCandidateId, ...]
    rejected_candidate_ids: tuple[SupplierCandidateId, ...]
    qualified_candidate_ids: tuple[SupplierCandidateId, ...]
    verified_event: SourcingCandidatesVerified | None

    @model_validator(mode="after")
    def validate_sets(self) -> Self:
        """结果集合必须去重；拒绝分类只引用本次 canonical 转换结果。"""

        collections = (
            self.calibration_draft_ids,
            self.converted_candidate_ids,
            self.rejected_candidate_ids,
            self.qualified_candidate_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("公开草稿核验结果 ID 不得重复")
        converted = set(self.converted_candidate_ids)
        if not set(self.rejected_candidate_ids) <= converted:
            raise ValueError("rejected_candidate_ids 必须属于转换候选")
        if self.verified_event is None:
            if self.qualified_candidate_ids:
                raise ValueError("合格候选必须携带封存事件")
        elif self.verified_event.candidate_ids != self.qualified_candidate_ids:
            raise ValueError("封存事件与合格候选集合不一致")
        return self


class PublicPageAttemptStatus(StrEnum):
    """公开页面槽的持久执行状态。"""

    CLAIMED = "claimed"
    COMPLETED = "completed"


class PublicPageAttemptOutcome(StrEnum):
    """页面槽可安全重放的固定完成结果。"""

    DRAFT_SAVED = "draft_saved"
    PAGE_ACCESS_FORBIDDEN = "page_access_forbidden"
    LOGIN_OR_CAPTCHA = "login_or_captcha"
    UNSAFE_REDIRECT = "unsafe_redirect"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_TIMEOUT = "provider_timeout"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class PublicPageAttempt(BaseModel):
    """绑定已授权计划的 canonical 页面槽；不包含页面正文或 locator。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: TenantId
    case_id: SourcingCaseId
    run_id: RunId
    plan_id: SourcingPlanId
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_index: int = Field(ge=0)
    result_index: int = Field(ge=0)
    status: PublicPageAttemptStatus
    outcome: PublicPageAttemptOutcome | None = None
    draft_id: str | None = Field(default=None, min_length=1, max_length=40)
    has_supplier_identity: bool | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status is PublicPageAttemptStatus.CLAIMED:
            if any(
                value is not None
                for value in (
                    self.outcome,
                    self.draft_id,
                    self.has_supplier_identity,
                )
            ):
                raise ValueError("claimed 页面槽不得携带完成结果")
            return self
        if self.outcome is None:
            raise ValueError("completed 页面槽必须携带固定结果")
        if self.outcome is PublicPageAttemptOutcome.DRAFT_SAVED:
            if self.draft_id is None or self.has_supplier_identity is None:
                raise ValueError("draft_saved 页面槽必须绑定已核验草稿")
        elif self.draft_id is not None or self.has_supplier_identity is not None:
            raise ValueError("页面拒绝结果不得绑定草稿")
        return self


class PublicPageAttemptClaim(BaseModel):
    """原子 claim 的结果；冲突时仍返回 canonical 页面槽。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    claimed_new: bool
    slot: PublicPageAttempt


class PublicSourcingPlanCommand(BaseModel):
    """待老板确认的公开寻源精确范围，不包含请求身份或凭证。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    plan_id: SourcingPlanId
    case_id: SourcingCaseId
    target_countries: tuple[str, ...] = Field(min_length=1)
    product_category: str = Field(min_length=1, max_length=100)
    queries: tuple[PublicSourcingQuery, ...] = Field(min_length=1)
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

        _bounded_text(self.product_category, field_name="product_category", maximum=100)
        for country in self.target_countries:
            if len(country) != 2 or not country.isascii() or not country.isupper():
                raise ValueError("target_countries 必须使用两位大写国家代码")
        if len(set(self.target_countries)) != len(self.target_countries):
            raise ValueError("target_countries 不得重复")
        if any(
            query.target_country not in self.target_countries for query in self.queries
        ):
            raise ValueError("queries 的 target_country 必须属于 target_countries")
        if any(
            len(query.query_text) > 400 or len(query.query_text.split()) > 50
            for query in self.queries
        ):
            raise ValueError("queries 超出 Gateway 查询边界")
        if {query.target_country for query in self.queries} != set(
            self.target_countries
        ):
            raise ValueError("每个 target_country 必须至少有一条 query")
        if len(set(self.queries)) != len(self.queries):
            raise ValueError("queries 的文本与国家组合不得重复")
        if len(self.queries) > self.max_search_queries:
            raise ValueError("queries 数量不得超过 max_search_queries")
        return self


class SourcingUncertainReconciliationCommand(BaseModel):
    """人工确认一次不确定搜索已经消耗额度；不接受 Provider 原始响应。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    reconciliation_id: str = Field(min_length=1, max_length=40)
    run_id: RunId
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution: Literal["count_as_consumed"]
    reason: str = Field(min_length=1, max_length=2_000)
    provider_usage_artifact_ref: ArtifactId

    @model_validator(mode="after")
    def validate_reconciliation(self) -> Self:
        """操作 ID、理由与证据引用必须是可安全审计的有界标识。"""

        _bounded_text(
            self.reconciliation_id,
            field_name="reconciliation_id",
            maximum=40,
        )
        _bounded_text(str(self.run_id), field_name="run_id", maximum=40)
        _bounded_text(self.reason, field_name="reason")
        _bounded_text(
            str(self.provider_usage_artifact_ref),
            field_name="provider_usage_artifact_ref",
            maximum=32,
        )
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

        _bounded_text(
            str(self.primary_option_id), field_name="primary_option_id", maximum=200
        )
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
    unit_amount: WireDecimal
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
        expected_source = (
            "supplier_candidate"
            if self.supplier_candidate_id is not None
            else "existing_product"
        )
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

        _validate_provenance_summary(self.provenance)
        _bounded_text(str(self.evidence_ref), field_name="evidence_ref", maximum=200)
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

        _validate_provenance_summary(self.provenance)
        _bounded_text(str(self.evidence_ref), field_name="evidence_ref", maximum=200)
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
        for evidence_ref in self.based_on:
            _bounded_text(str(evidence_ref), field_name="based_on", maximum=200)
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
    customer_confirmation: ProvenanceSummary | None = None

    @model_validator(mode="after")
    def validate_customer_confirmation(self) -> Self:
        """客户确认必须来自可定位的客户会话，不能由网页或模型自证。"""

        if self.customer_confirmation is not None:
            _validate_provenance_summary(self.customer_confirmation)
            if self.customer_confirmation.source_type is not SourceType.CONVERSATION:
                raise ValueError("customer_confirmation 必须来自客户会话")
        return self


class IndicativePriceTier(BaseModel):
    """带逐档来源的参考价；每一项都必须可回到可信 Artifact。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    minimum_quantity: int = Field(ge=1)
    amount: WireDecimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: str = Field(min_length=1, max_length=50)
    provenance: ProvenanceSummary
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_tier(self) -> Self:
        """参考价必须为正且来源不得伪装成 Agent 事实。"""

        if self.amount <= Decimal(0):
            raise ValueError("amount 必须是有限正 Decimal")
        _bounded_text(self.unit, field_name="unit", maximum=50)
        _validate_provenance_summary(self.provenance)
        if self.provenance.source_type is SourceType.AGENT_INFERENCE:
            raise ValueError("参考价不得使用 AGENT_INFERENCE 来源")
        _bounded_text(str(self.evidence_ref), field_name="evidence_ref", maximum=200)
        return self


class SourcingCandidateProductPriceInput(BaseModel):
    """候选产品卡的单个 INDICATIVE 价格档公共投影。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    minimum_quantity: int = Field(ge=1)
    unit_amount: WireDecimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: str = Field(min_length=1, max_length=50)
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_price(self) -> Self:
        """参考价必须是正 Decimal 且能回到不可变证据。"""

        if not self.unit_amount.is_finite() or self.unit_amount <= Decimal(0):
            raise ValueError("unit_amount 必须是有限正 Decimal")
        exponent = self.unit_amount.as_tuple().exponent
        if not isinstance(exponent, int):
            raise TypeError("unit_amount 必须可精确表示为 NUMERIC(28,12)")
        scale = max(-exponent, 0)
        integer_digits = max(self.unit_amount.adjusted() + 1, 0)
        if scale > 12 or integer_digits > 16:
            raise ValueError("unit_amount 必须可精确表示为 NUMERIC(28,12)")
        _bounded_text(self.unit, field_name="unit", maximum=50)
        _bounded_text(str(self.evidence_ref), field_name="evidence_ref", maximum=200)
        return self


class SourcingCandidateProductInput(BaseModel):
    """寻源域向编排层暴露的严格产品卡输入。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    sourcing_case_id: SourcingCaseId
    supplier_candidate_id: SupplierCandidateId
    name_zh: str = Field(min_length=1, max_length=300)
    name_en: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    spec_summary: str = Field(min_length=1, max_length=4_000)
    moq: int = Field(ge=1)
    evidence_refs: tuple[ArtifactId, ...] = Field(min_length=1)
    indicative_prices: tuple[SourcingCandidateProductPriceInput, ...] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        """命令与产品域强类型输入同形，但不跨域导入模型。"""

        for field_name, value, maximum in (
            ("sourcing_case_id", str(self.sourcing_case_id), 200),
            ("supplier_candidate_id", str(self.supplier_candidate_id), 200),
            ("name_zh", self.name_zh, 300),
            ("name_en", self.name_en, 300),
            ("category", self.category, 100),
            ("spec_summary", self.spec_summary, 4_000),
        ):
            _bounded_text(value, field_name=field_name, maximum=maximum)
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs 不得重复")
        evidence = set(self.evidence_refs)
        if any(item.evidence_ref not in evidence for item in self.indicative_prices):
            raise ValueError("每个参考价 Evidence 必须属于 evidence_refs")
        minimums = tuple(item.minimum_quantity for item in self.indicative_prices)
        if len(set(minimums)) != len(minimums):
            raise ValueError("indicative_prices 不得重复数量档")
        return self


class SourcingCandidateProductInputs(BaseModel):
    """封存 generation 及其全部产品卡命令的 tenant-bound 投影。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: TenantId
    case_id: SourcingCaseId
    candidate_ids: tuple[SupplierCandidateId, ...] = Field(min_length=1, max_length=3)
    case_version: int = Field(ge=1)
    candidate_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    commands: tuple[SourcingCandidateProductInput, ...] = Field(
        min_length=1, max_length=3
    )

    @model_validator(mode="after")
    def validate_generation(self) -> Self:
        """严格保证命令集与封存候选集一一对应且顺序一致。"""

        if tuple(sorted(self.candidate_ids, key=str)) != self.candidate_ids or len(
            set(self.candidate_ids)
        ) != len(self.candidate_ids):
            raise ValueError("candidate_ids 必须精确排序且不重复")
        if (
            tuple(item.supplier_candidate_id for item in self.commands)
            != self.candidate_ids
        ):
            raise ValueError("产品卡命令必须精确覆盖封存候选集")
        if any(item.sourcing_case_id != self.case_id for item in self.commands):
            raise ValueError("产品卡命令必须属于同一封存 Case")
        return self


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
    indicative_price_tiers: tuple[IndicativePriceTier, ...] = Field(min_length=1)
    moq: int | None
    price_unit: str | None
    currency: str | None
    evidence_url: str
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_artifact_ref: str

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        """候选写入口拒绝模糊数量档和缺失证据。"""

        _bounded_text(self.supplier_name, field_name="supplier_name", maximum=300)
        _bounded_text(self.product_title, field_name="product_title", maximum=500)
        _bounded_text(self.evidence_url, field_name="evidence_url")
        _bounded_text(
            self.evidence_artifact_ref, field_name="evidence_artifact_ref", maximum=200
        )
        normalized_spec_names = [
            item.spec_name.strip().casefold() for item in self.specs
        ]
        if len(set(normalized_spec_names)) != len(normalized_spec_names):
            raise ValueError("specs 规格名不得重复")
        minimums = [tier.minimum_quantity for tier in self.indicative_price_tiers]
        if len(set(minimums)) != len(minimums):
            raise ValueError("indicative_price_tiers 数量档不得重复")
        currencies = {tier.currency for tier in self.indicative_price_tiers}
        if self.currency is None or currencies != {self.currency}:
            raise ValueError("indicative_price_tiers 与 currency 必须一致")
        if self.price_unit is None or {
            tier.unit for tier in self.indicative_price_tiers
        } != {self.price_unit}:
            raise ValueError("indicative_price_tiers 与 price_unit 必须一致")
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


class SourcingArtifactSummaryView(BaseModel):
    """候选网页快照的安全索引；不包含页面正文、对象键或联系人。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    artifact_id: ArtifactId
    canonical_url: str = Field(min_length=1, max_length=2_000)
    observed_at: AwareDatetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourcingStopPublicView(BaseModel):
    """可展示的结构化停止原因；禁止透传 Provider 自由错误文本。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    stage: str | None = Field(default=None, min_length=1, max_length=40)
    query_index: int | None = Field(default=None, ge=0)
    provider_http_status: int | None = Field(default=None, ge=100, le=599)
    observed_count: int | None = Field(default=None, ge=0)
    configured_limit: int | None = Field(default=None, ge=0)


class SourcingCaseReadView(BaseModel):
    """API 读取案例的最小安全投影，保留 Need Provenance 而不暴露内部聚合。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    case_id: SourcingCaseId
    need_id: ValidatedNeedId
    state: str = Field(min_length=1, max_length=40)
    workflow_version: int = Field(ge=1)
    version: int = Field(ge=1)
    opened_at: AwareDatetime
    state_changed_at: AwareDatetime | None = None
    ladder_checked_to: int | None = Field(default=None, ge=1, le=7)
    active_search_plan_id: SourcingPlanId | None = None
    need_snapshot: SourcingNeedSnapshot | None = None
    stop: SourcingStopPublicView | None = None


class SourcingLadderCheckReadView(BaseModel):
    """单级梯子检查的解释性投影；不使用相似度或综合分。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    check_id: str = Field(min_length=1, max_length=200)
    rung: int = Field(ge=1, le=7)
    sequence_number: int = Field(ge=1)
    outcome: str = Field(min_length=1, max_length=80)
    match_object_type: str | None = Field(default=None, max_length=100)
    match_object_id: str | None = Field(default=None, max_length=200)
    spec_comparisons: tuple[SpecComparisonView, ...] = ()
    evidence_refs: tuple[ArtifactId, ...] = ()
    checked_by: str = Field(min_length=1, max_length=200)
    checked_at: AwareDatetime


class SourcingSupplyOptionReadView(BaseModel):
    """人工审核可选的供给选项索引，不含成本或供应商报价。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    option_id: SourcingSupplyOptionId
    product_id: ProductId
    supplier_candidate_id: SupplierCandidateId | None = None
    source_kind: str = Field(min_length=1, max_length=40)
    is_qualified: bool


class SourcingCandidateReadView(BaseModel):
    """候选事实、自述、推断和未知项分栏的安全 API 投影。

    ``indicative_price_tiers`` 仅代表公开页面参考价，不能成为 Quote 或客户报价。
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    candidate_id: SupplierCandidateId
    supplier_name: str = Field(min_length=1, max_length=300)
    product_title: str = Field(min_length=1, max_length=500)
    source_platform: str | None = Field(default=None, max_length=200)
    observed_facts: dict[str, SourcingObservedFact] = Field(default_factory=dict)
    supplier_claims: dict[str, SourcingSupplierClaim] = Field(default_factory=dict)
    match_inferences: dict[str, SourcingMatchInference] = Field(default_factory=dict)
    spec_comparisons: tuple[SpecComparisonView, ...] = ()
    # 被拒或未完成的候选仍是核验校准事实；没有参考价时明确为空，而不是伪造价格。
    indicative_price_tiers: tuple[IndicativePriceTier, ...] = ()
    price_basis: Literal["indicative"] = "indicative"
    moq: int | None = Field(default=None, ge=1)
    price_unit: str | None = Field(default=None, max_length=50)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    verification_status: Literal["qualified", "rejected", "incomplete"]
    verification_missing: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    # 未完成候选可明确暴露“无可用证据”这个未知状态，不能为了响应形状伪造 Artifact。
    evidence: tuple[SourcingArtifactSummaryView, ...] = ()
    supply_option: SourcingSupplyOptionReadView | None = None


class PublicSourcingQueryReadView(BaseModel):
    """公开计划中已持久化的一条查询；现有契约未记录 lane 时显式返回未知。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    query_text: str = Field(min_length=1, max_length=400)
    target_country: str = Field(pattern=r"^[A-Z]{2}$")
    lane: str | None = None


class PublicSourcingPlanReadView(BaseModel):
    """公开寻源计划的无密钥、可确认范围投影。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    plan_id: SourcingPlanId
    case_id: SourcingCaseId
    target_countries: tuple[str, ...] = Field(min_length=1)
    product_category: str = Field(min_length=1, max_length=100)
    queries: tuple[PublicSourcingQueryReadView, ...] = Field(min_length=1)
    max_search_queries: int = Field(ge=1)
    max_pages_read: int = Field(ge=1)
    provider: Literal["tavily"]
    search_depth: Literal["basic"]
    usage_credits_remaining: int = Field(ge=0)
    worst_case_credits: int = Field(ge=1)
    version: int = Field(ge=1)
    expected_case_version: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str = Field(min_length=1, max_length=40)
    confirmed_by: str | None = Field(default=None, max_length=200)
    confirmed_at: AwareDatetime | None = None
    created_at: AwareDatetime


class SourcingCurrentQuotaReadView(BaseModel):
    """当前安全额度摘要；未知保持 null/unknown，不能由计划快照推断。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    remaining: int | None = Field(default=None, ge=0)
    reservations: int | None = Field(default=None, ge=0)
    cost_status: Literal["free", "paid", "unknown"]
    paygo_enabled: bool | None = None
    checked_at: AwareDatetime | None = None


class SourcingReviewReadView(BaseModel):
    """人工审核的安全选择事实；不包含 Opportunity、成本或报价。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    review_id: SourcingReviewId
    case_id: SourcingCaseId
    primary_option_id: SourcingSupplyOptionId
    alternate_option_ids: tuple[SourcingSupplyOptionId, ...] = Field(max_length=2)
    reason: str = Field(min_length=1, max_length=2_000)
    expected_case_version: int = Field(ge=1)
    submitted_by: str = Field(min_length=1, max_length=200)
    submitted_at: AwareDatetime
    confirmed_by: str | None = Field(default=None, max_length=200)
    confirmed_at: AwareDatetime | None = None
    can_current_user_confirm: bool


class SourcingReconciliationReadView(BaseModel):
    """已保存人工核对的可审计摘要；不含 Provider 原文或搜索输入。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    reconciliation_id: str = Field(min_length=1, max_length=40)
    execution_id: str = Field(min_length=1, max_length=40)
    status: Literal["confirmed_consumed", "confirmed_not_consumed", "required"]
    reason: str = Field(min_length=1, max_length=2_000)
    provider_usage_artifact_ref: ArtifactId
    reconciled_by: str | None = Field(default=None, max_length=200)
    reconciled_at: AwareDatetime | None = None


class SourcingUncertainExecutionReadView(BaseModel):
    """可恢复不确定执行的最小操作标识，不输出查询、页面或 Provider 载荷。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    execution_id: str = Field(min_length=1, max_length=40)
    run_id: RunId
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["uncertain"]
    created_at: AwareDatetime
    reconciliation: SourcingReconciliationReadView | None = None
    can_current_user_reconcile: bool


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
