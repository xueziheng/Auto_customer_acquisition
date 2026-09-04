"""产品域对外 DTO。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import (
    ApprovalId,
    ArtifactId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    ProductId,
    ProspectAccountId,
    RunId,
    SourcingCaseId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import WireDecimal

_LOWER_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PG_SIGNED_INT_MAX = 2_147_483_647
_CATALOG_RULE_ORDER = (
    "membership_integrity",
    "distinct_accounts",
    "recurring_accounts",
    "distinct_countries",
    "quantity_unit_coverage",
    "unified_unit",
)
CatalogRuleName = Literal[
    "membership_integrity",
    "distinct_accounts",
    "recurring_accounts",
    "distinct_countries",
    "quantity_unit_coverage",
    "unified_unit",
]
CatalogRuleStatus = Literal["passed", "failed", "unknown", "not_required"]
CatalogExplanationCode = Literal[
    "目录事实损坏，评估已阻断",
    "成员关系与品类完整一致",
    "去重客户数达到策略门槛",
    "去重客户数未达到策略门槛",
    "复购客户数达到策略门槛",
    "复购客户数未达到策略门槛",
    "复购客户事实不完整",
    "策略不要求复购客户数",
    "已知国家数达到策略门槛",
    "已知国家数未达到策略门槛",
    "客户国家事实不完整",
    "策略不要求已知国家数",
    "数量单位覆盖达到策略门槛",
    "数量单位覆盖未达到策略门槛",
    "数量单位事实不完整",
    "策略不要求数量单位覆盖",
    "有效数量单位已经统一",
    "统一单位事实不完整",
    "有效数量单位不统一",
    "统一单位事实未知",
    "策略不要求统一单位",
]
CatalogPolicyState = Literal[
    "pending_approval", "active", "superseded", "rejected", "expired", "stale"
]
CatalogProposalState = Literal[
    "awaiting_approval_submission",
    "pending_review",
    "cultivation_queued",
    "rejected",
    "expired",
    "stale",
]


def _bounded_text(value: str, field_name: str, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} 必须非空、无首尾空白且不超过 {maximum} 字符")
    return value


def _strict_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} 必须是 UTC 时间")
    return value


def _lower_hash(value: str, field_name: str) -> str:
    if _LOWER_HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} 必须是 64 位小写 SHA-256")
    return value


def _bounded_identity(value: str, field_name: str, maximum: int = 200) -> str:
    return _bounded_text(value, field_name, maximum)


def _storage_safe_count(value: int, field_name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _PG_SIGNED_INT_MAX:
        raise ValueError(
            f"{field_name} 必须是 {minimum}..{_PG_SIGNED_INT_MAX} 的整数"
        )
    return value


class _CatalogFrozenModel(BaseModel):
    """目录提案公共契约统一拒绝宽松转换、额外字段和原地修改。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )


class CatalogProposalPolicyContent(_CatalogFrozenModel):
    """必须经人工审批的目录提案策略内容；生产环境没有默认实例。"""

    minimum_distinct_accounts: int
    minimum_recurring_accounts: int | None
    minimum_distinct_countries: int | None
    minimum_quantity_unit_accounts: int | None
    require_unified_unit: bool

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        _storage_safe_count(
            self.minimum_distinct_accounts,
            "minimum_distinct_accounts",
            minimum=2,
        )
        for field_name in (
            "minimum_recurring_accounts",
            "minimum_quantity_unit_accounts",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _storage_safe_count(value, field_name, minimum=1)
                if value > self.minimum_distinct_accounts:
                    raise ValueError(f"{field_name} 不得大于 minimum_distinct_accounts")
        if self.minimum_distinct_countries is not None:
            _storage_safe_count(
                self.minimum_distinct_countries,
                "minimum_distinct_countries",
                minimum=2,
            )
        if (
            self.require_unified_unit
            and self.minimum_quantity_unit_accounts is None
        ):
            raise ValueError("统一单位要求只能在数量单位门槛启用时设置")
        return self


class CatalogEvidenceSummaryInput(_CatalogFrozenModel):
    """Products 自有的安全 Evidence 指纹；不承载事实值或原文。"""

    source_type: Literal[
        "conversation", "web_page", "upload", "employee_input", "external_api"
    ]
    source_id: str
    extracted_by: str
    confirmed_by: EmployeeId | None
    confirmed_at: datetime | None
    observed_at: datetime
    content_hash: str

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        _bounded_identity(self.source_id, "source_id")
        _bounded_identity(self.extracted_by, "extracted_by")
        if (self.confirmed_by is None) != (self.confirmed_at is None):
            raise ValueError("confirmed_by 与 confirmed_at 必须成对")
        if self.confirmed_by is not None:
            _bounded_identity(self.confirmed_by, "confirmed_by", 40)
        if self.confirmed_at is not None:
            _strict_utc(self.confirmed_at, "confirmed_at")
        _strict_utc(self.observed_at, "observed_at")
        _lower_hash(self.content_hash, "content_hash")
        return self


class CatalogClusterFactsInput(_CatalogFrozenModel):
    """workflow 显式映射给 Products 的事实快照，不依赖 Demand 类型。"""

    tenant_id: TenantId
    cluster_id: NeedClusterId
    cluster_category: str
    member_need_ids: tuple[ValidatedNeedId, ...]
    distinct_account_ids: tuple[ProspectAccountId, ...]
    member_count: int
    distinct_account_count: int
    known_country_codes: tuple[str, ...]
    unknown_country_account_count: int
    recurring_true_account_count: int
    recurring_false_account_count: int
    recurring_unknown_account_count: int
    quantity_unit_covered_account_count: int
    unified_unit: str | None
    safe_total_quantity: int | None
    evidence_summaries: tuple[CatalogEvidenceSummaryInput, ...]
    display_codes: tuple[str, ...]
    facts_observed_at: datetime
    facts_hash: str

    @model_validator(mode="after")
    def validate_facts(self) -> Self:
        _bounded_identity(self.tenant_id, "tenant_id", 40)
        _bounded_identity(self.cluster_id, "cluster_id", 40)
        _bounded_text(self.cluster_category, "cluster_category", 200)
        for field_name, values in (
            ("member_need_ids", self.member_need_ids),
            ("distinct_account_ids", self.distinct_account_ids),
        ):
            rendered = tuple(str(item) for item in values)
            if not rendered or rendered != tuple(sorted(rendered)):
                raise ValueError(f"{field_name} 必须非空且稳定排序")
            if len(set(rendered)) != len(rendered):
                raise ValueError(f"{field_name} 不得重复")
            for value in rendered:
                _bounded_identity(value, field_name, 40)
        for field_name in (
            "member_count",
            "distinct_account_count",
            "unknown_country_account_count",
            "recurring_true_account_count",
            "recurring_false_account_count",
            "recurring_unknown_account_count",
            "quantity_unit_covered_account_count",
        ):
            _storage_safe_count(getattr(self, field_name), field_name)
        if self.member_count != len(self.member_need_ids):
            raise ValueError("member_count 与 member_need_ids 不一致")
        if self.distinct_account_count != len(self.distinct_account_ids):
            raise ValueError("distinct_account_count 与 distinct_account_ids 不一致")
        if self.distinct_account_count > self.member_count:
            raise ValueError("去重客户数不得大于成员 Need 数")
        recurring_total = (
            self.recurring_true_account_count
            + self.recurring_false_account_count
            + self.recurring_unknown_account_count
        )
        if recurring_total != self.distinct_account_count:
            raise ValueError("复购账户分类必须覆盖全部去重客户")
        countries = self.known_country_codes
        if (
            countries != tuple(sorted(countries))
            or len(set(countries)) != len(countries)
            or any(re.fullmatch(r"[A-Z]{2}", item) is None for item in countries)
            or len(countries) + self.unknown_country_account_count
            > self.distinct_account_count
        ):
            raise ValueError("国家事实必须稳定、唯一且不超过去重客户数")
        if self.quantity_unit_covered_account_count > self.distinct_account_count:
            raise ValueError("数量单位覆盖不得大于去重客户数")
        if self.unified_unit is not None:
            _bounded_text(self.unified_unit, "unified_unit", 64)
        if self.safe_total_quantity is not None:
            _storage_safe_count(self.safe_total_quantity, "safe_total_quantity")
            if (
                self.unified_unit is None
                or self.member_count != self.distinct_account_count
                or self.quantity_unit_covered_account_count
                != self.distinct_account_count
            ):
                raise ValueError("安全合计必须绑定全客户覆盖的统一单位")
        if self.display_codes != tuple(sorted(self.display_codes)) or len(
            set(self.display_codes)
        ) != len(self.display_codes):
            raise ValueError("display_codes 必须稳定排序且不得重复")
        for code in self.display_codes:
            _bounded_text(code, "display_code", 100)
        _strict_utc(self.facts_observed_at, "facts_observed_at")
        _lower_hash(self.facts_hash, "facts_hash")
        return self


class CatalogProposalRuleResult(_CatalogFrozenModel):
    """单条确定性规则结果；只允许固定代码，不容纳模型解释或概率。"""

    rule: CatalogRuleName
    status: CatalogRuleStatus
    actual_value: int | str | bool | None
    required_value: int | str | bool | None
    explanation_code: CatalogExplanationCode

    @model_validator(mode="after")
    def validate_explanation_mapping(self) -> Self:
        if self.explanation_code == "目录事实损坏，评估已阻断":
            if self.status != "unknown" or self.actual_value is not None:
                raise ValueError("损坏事实只能产生无 actual_value 的 unknown 结果")
            return self
        expected: dict[tuple[CatalogRuleName, CatalogRuleStatus], str] = {
            ("membership_integrity", "passed"): "成员关系与品类完整一致",
            ("distinct_accounts", "passed"): "去重客户数达到策略门槛",
            ("distinct_accounts", "failed"): "去重客户数未达到策略门槛",
            ("recurring_accounts", "passed"): "复购客户数达到策略门槛",
            ("recurring_accounts", "failed"): "复购客户数未达到策略门槛",
            ("recurring_accounts", "unknown"): "复购客户事实不完整",
            ("recurring_accounts", "not_required"): "策略不要求复购客户数",
            ("distinct_countries", "passed"): "已知国家数达到策略门槛",
            ("distinct_countries", "failed"): "已知国家数未达到策略门槛",
            ("distinct_countries", "unknown"): "客户国家事实不完整",
            ("distinct_countries", "not_required"): "策略不要求已知国家数",
            ("quantity_unit_coverage", "passed"): "数量单位覆盖达到策略门槛",
            ("quantity_unit_coverage", "failed"): "数量单位覆盖未达到策略门槛",
            ("quantity_unit_coverage", "unknown"): "数量单位事实不完整",
            ("quantity_unit_coverage", "not_required"): "策略不要求数量单位覆盖",
            ("unified_unit", "passed"): "有效数量单位已经统一",
            ("unified_unit", "failed"): "有效数量单位不统一",
            ("unified_unit", "unknown"): (
                "统一单位事实不完整"
                if self.required_value is True
                else "统一单位事实未知"
            ),
            ("unified_unit", "not_required"): "策略不要求统一单位",
        }
        if self.explanation_code != expected.get((self.rule, self.status)):
            raise ValueError("explanation_code 与规则状态不一致")
        return self


class CatalogProposalEvaluationResult(_CatalogFrozenModel):
    """纯规则输出；blocked_reason 仅表示输入损坏，不承载底层异常。"""

    rule_results: tuple[CatalogProposalRuleResult, ...]
    overall_passed: bool
    blocked_reason: Literal["catalog_facts_invalid"] | None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if tuple(item.rule for item in self.rule_results) != _CATALOG_RULE_ORDER:
            raise ValueError("目录评估规则顺序无效")
        blocks = any(
            item.status == "failed"
            or (item.status == "unknown" and item.required_value is not None)
            for item in self.rule_results
        )
        if self.blocked_reason is not None:
            blocks = True
        if self.overall_passed == blocks:
            raise ValueError("overall_passed 与规则结果不一致")
        return self


class CatalogApprovalDecisionInput(_CatalogFrozenModel):
    """workflow 从中央审批事实逐字段映射的窄输入；HTTP 不得直接接受。"""

    approval_id: ApprovalId
    approval_type: Literal[
        "catalog_proposal_policy_change", "catalog_product_cultivation"
    ]
    contract_namespace: Literal["catalog-policy-v1", "catalog-cultivation-v1"]
    change_set_ref: str
    request_hash: str
    state: Literal["approved", "rejected", "expired"]
    proposed_by_run: RunId | None
    proposed_by_employee: EmployeeId | None
    owner_employee: EmployeeId
    decided_by_employee: EmployeeId | None
    decided_at: datetime | None
    expires_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        expected_namespace = {
            "catalog_proposal_policy_change": "catalog-policy-v1",
            "catalog_product_cultivation": "catalog-cultivation-v1",
        }[self.approval_type]
        if self.contract_namespace != expected_namespace:
            raise ValueError("审批类型与 contract_namespace 不一致")
        for field_name in (
            "approval_id",
            "change_set_ref",
            "owner_employee",
        ):
            _bounded_identity(str(getattr(self, field_name)), field_name)
        _lower_hash(self.request_hash, "request_hash")
        _strict_utc(self.expires_at, "expires_at")
        if (self.proposed_by_run is None) == (self.proposed_by_employee is None):
            raise ValueError("审批提议方必须且只能是 Run 或员工之一")
        if self.proposed_by_run is not None:
            _bounded_identity(self.proposed_by_run, "proposed_by_run", 40)
        if self.proposed_by_employee is not None:
            _bounded_identity(self.proposed_by_employee, "proposed_by_employee", 40)
        if self.state in {"approved", "rejected"}:
            if self.decided_by_employee is None or self.decided_at is None:
                raise ValueError("已决定审批必须包含决定人和决定时间")
            _bounded_identity(self.decided_by_employee, "decided_by_employee", 40)
            _strict_utc(self.decided_at, "decided_at")
            if self.decided_at > self.expires_at:
                raise ValueError("决定时间不得晚于审批有效期")
            if self.decided_by_employee in {
                self.proposed_by_employee,
                self.owner_employee,
            }:
                raise ValueError("提议人或 owner 不得自批")
        elif self.decided_by_employee is not None or self.decided_at is not None:
            raise ValueError("过期审批不得伪造决定人或决定时间")
        return self


class CatalogProposalPolicyView(_CatalogFrozenModel):
    """策略安全视图；不公开幂等键或请求 hash。"""

    policy_version_id: CatalogProposalPolicyVersionId
    content: CatalogProposalPolicyContent
    content_hash: str
    base_active_version_id: CatalogProposalPolicyVersionId | None
    proposed_by: EmployeeId
    approval_id: ApprovalId | None
    state: CatalogPolicyState
    created_at: datetime
    activated_at: datetime | None
    terminal_at: datetime | None

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        from domains.products.catalog_rules import catalog_policy_content_hash

        _bounded_identity(self.policy_version_id, "policy_version_id", 40)
        _bounded_identity(self.proposed_by, "proposed_by", 40)
        _lower_hash(self.content_hash, "content_hash")
        if self.content_hash != catalog_policy_content_hash(self.content):
            raise ValueError("content_hash 与策略内容不一致")
        _strict_utc(self.created_at, "created_at")
        for field_name in ("activated_at", "terminal_at"):
            value = getattr(self, field_name)
            if value is not None:
                _strict_utc(value, field_name)
                if value < self.created_at:
                    raise ValueError(f"{field_name} 不得早于 created_at")
        return self


class CatalogPolicyChangeSnapshot(_CatalogFrozenModel):
    """供审批 workflow 读取的 base/current/candidate 安全快照。"""

    base: CatalogProposalPolicyView | None
    current: CatalogProposalPolicyView | None
    candidate: CatalogProposalPolicyView
    base_is_current: bool


class CatalogProposalEvaluationView(_CatalogFrozenModel):
    evaluation_id: CatalogProposalEvaluationId
    cluster_id: NeedClusterId
    policy_version_id: CatalogProposalPolicyVersionId
    facts_hash: str
    facts: CatalogClusterFactsInput
    rule_results: tuple[CatalogProposalRuleResult, ...]
    overall_passed: bool
    blocked_reason: Literal["catalog_facts_invalid"] | None
    proposed_by_run: RunId
    created_at: datetime

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        _lower_hash(self.facts_hash, "facts_hash")
        if self.facts_hash != self.facts.facts_hash:
            raise ValueError("评估 facts_hash 与事实快照不一致")
        CatalogProposalEvaluationResult(
            rule_results=self.rule_results,
            overall_passed=self.overall_passed,
            blocked_reason=self.blocked_reason,
        )
        _strict_utc(self.created_at, "created_at")
        return self


class CatalogProductProposalView(_CatalogFrozenModel):
    proposal_id: CatalogProductProposalId
    evaluation_id: CatalogProposalEvaluationId
    cluster_id: NeedClusterId
    policy_version_id: CatalogProposalPolicyVersionId
    facts_hash: str
    owner_employee: EmployeeId
    proposed_by_run: RunId
    approval_id: ApprovalId | None
    state: CatalogProposalState
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        _lower_hash(self.facts_hash, "facts_hash")
        _strict_utc(self.created_at, "created_at")
        _strict_utc(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不得早于 created_at")
        return self


class CatalogCultivationCaseView(_CatalogFrozenModel):
    cultivation_case_id: CatalogCultivationCaseId
    proposal_id: CatalogProductProposalId
    approval_id: ApprovalId
    cluster_id: NeedClusterId
    policy_version_id: CatalogProposalPolicyVersionId
    facts_hash: str
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    state: Literal["queued"] = "queued"
    queued_at: datetime

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        _lower_hash(self.facts_hash, "facts_hash")
        _strict_utc(self.queued_at, "queued_at")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence_refs 不得重复")
        for evidence_ref in self.evidence_refs:
            _bounded_identity(evidence_ref, "evidence_ref")
        return self


def _require_numeric_28_12(value: Decimal) -> None:
    """拒绝数据库会舍入或溢出的值；保留调用方原始 Decimal。"""

    exponent = cast(int, value.as_tuple().exponent)
    scale = max(-exponent, 0)
    integer_digits = max(value.adjusted() + 1, 0)
    if scale > 12 or integer_digits > 16:
        raise ValueError("unit_amount 必须可精确表示为 NUMERIC(28,12)")


class CandidateIndicativePriceRef(BaseModel):
    """候选产品的公开参考价；Artifact 是原页证据而不是搜索摘要。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    minimum_quantity: int = Field(ge=1)
    unit_amount: WireDecimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: str = Field(min_length=1, max_length=50)
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        """金额、单位和证据定位必须同时可用。"""

        if not self.unit_amount.is_finite() or self.unit_amount <= Decimal(0):
            raise ValueError("unit_amount 必须是有限正 Decimal")
        _require_numeric_28_12(self.unit_amount)
        _bounded_text(self.unit, "unit", 50)
        _bounded_text(str(self.evidence_ref), "evidence_ref", 200)
        return self


class CandidateProductCreate(BaseModel):
    """由合格 Sourcing Candidate 生成 source_only 产品卡的强类型命令。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    sourcing_case_id: SourcingCaseId
    supplier_candidate_id: SupplierCandidateId
    name_zh: str = Field(min_length=1, max_length=300)
    name_en: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    spec_summary: str = Field(min_length=1, max_length=4_000)
    moq: int = Field(ge=1)
    evidence_refs: tuple[ArtifactId, ...] = Field(min_length=1)
    indicative_prices: tuple[CandidateIndicativePriceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_and_tiers(self) -> Self:
        """命令必须可回溯，且每个参考价都绑定已声明的原页 Artifact。"""

        for field_name, value, maximum in (
            ("sourcing_case_id", str(self.sourcing_case_id), 200),
            ("supplier_candidate_id", str(self.supplier_candidate_id), 200),
            ("name_zh", self.name_zh, 300),
            ("name_en", self.name_en, 300),
            ("category", self.category, 100),
            ("spec_summary", self.spec_summary, 4_000),
        ):
            _bounded_text(value, field_name, maximum)
        evidence = tuple(str(item) for item in self.evidence_refs)
        if any(not item or item != item.strip() for item in evidence):
            raise ValueError("evidence_refs 不得包含空引用")
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_refs 不得重复")
        evidence_set = set(self.evidence_refs)
        if any(
            item.evidence_ref not in evidence_set for item in self.indicative_prices
        ):
            raise ValueError("每个参考价 Evidence 必须包含在 evidence_refs 中")
        minimums = [item.minimum_quantity for item in self.indicative_prices]
        if len(set(minimums)) != len(minimums):
            raise ValueError("indicative_prices 不得重复数量档")
        return self


class ProductSupplySourceView(BaseModel):
    """source_only 产品卡的寻源来源链；没有供应商联系人、成本或客户报价。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    sourcing_case_id: SourcingCaseId
    supplier_candidate_id: SupplierCandidateId
    evidence_refs: tuple[ArtifactId, ...] = Field(min_length=1)
    indicative_prices: tuple[CandidateIndicativePriceRef, ...] = Field(min_length=1)
    price_basis: Literal["indicative"] = "indicative"


class ProductSupplyCardView(BaseModel):
    """供应中心安全卡片；没有 supplier、成本、客户报价或联系入口。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    product_id: ProductId
    pool: str = Field(min_length=1, max_length=40)
    candidate_status: str | None = Field(default=None, max_length=40)
    name_zh: str = Field(min_length=1, max_length=300)
    name_en: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    spec_summary: str | None = Field(default=None, max_length=4_000)
    moq: int | None = Field(default=None, ge=1)
    lead_time_display: str | None = Field(default=None, max_length=50)
    source_only: bool
    quote_warning: Literal["不可用于客户报价"] | None = None
    source: ProductSupplySourceView | None = None

    @model_validator(mode="after")
    def validate_source_only(self) -> Self:
        """source_only 必须有完整来源链并永远携带不可报价提示。"""

        if self.source_only != (self.candidate_status == "source_only"):
            raise ValueError("source_only 与 candidate_status 不一致")
        if self.source_only and (
            self.source is None or self.quote_warning != "不可用于客户报价"
        ):
            raise ValueError("source_only 产品卡必须有来源链和不可报价提示")
        if not self.source_only and (self.source is not None or self.quote_warning):
            raise ValueError("非 source_only 产品卡不得伪装为寻源候选")
        return self


__all__ = (
    "CandidateIndicativePriceRef",
    "CandidateProductCreate",
    "CatalogApprovalDecisionInput",
    "CatalogClusterFactsInput",
    "CatalogCultivationCaseView",
    "CatalogEvidenceSummaryInput",
    "CatalogExplanationCode",
    "CatalogPolicyChangeSnapshot",
    "CatalogPolicyState",
    "CatalogProductProposalView",
    "CatalogProposalEvaluationResult",
    "CatalogProposalEvaluationView",
    "CatalogProposalPolicyContent",
    "CatalogProposalPolicyView",
    "CatalogProposalRuleResult",
    "CatalogProposalState",
    "CatalogRuleName",
    "CatalogRuleStatus",
    "ProductSupplyCardView",
    "ProductSupplySourceView",
)
