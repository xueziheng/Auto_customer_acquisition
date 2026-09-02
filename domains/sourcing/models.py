"""寻源域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from typing import Any, cast
from urllib.parse import urlsplit

from domains.sourcing._admission_facts import canonical_priority_facts_hash
from domains.sourcing.errors import SourcingPlanStaleError, SourcingReviewStaleError
from domains.sourcing.schemas import (
    IndicativePriceTier,
    ProvenanceSummary,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingReviewCommand,
    SourcingSupplierClaim,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    NeedClusterId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingPrioritySnapshotId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import SourceType


class MatchLadderRung(int, Enum):
    """匹配梯子。数值即优先顺序，**不跳级**。

    前面的梯级供货确定性高、报价快、图片全。跳到公开寻源前要先
    确认前五级都不行——每往下一级，成本和不确定性都在上升。
    """

    CATALOG_EXACT = 1
    CATALOG_MODIFIABLE = 2
    CANDIDATE_PRODUCT = 3
    EXISTING_SUPPLIER_SIMILAR = 4
    EXISTING_SUPPLIER_CUSTOM = 5
    PUBLIC_SOURCING = 6
    NEW_FACTORY = 7


class LadderOutcome(str, Enum):
    """单级匹配的确定性结果；不得从自由文本结论推断。"""

    NO_QUALIFIED_SUPPLY = "no_qualified_supply"
    QUALIFIED_SUPPLY_FOUND = "qualified_supply_found"


class SpecMatchLevel(str, Enum):
    """单项规格的匹配程度。匹配结果按项给出，不合成一个百分比。"""

    EXACT = "exact"
    DIFFERENT = "different"
    UNKNOWN = "unknown"
    """未知不等于不匹配——它意味着要么问供应商，要么问客户。
    把 UNKNOWN 乐观地当 EXACT 是错误匹配的主要来源。"""


@dataclass(frozen=True)
class SpecComparison:
    """一项规格的对比。

    字段：
        spec_name:       规格名（material / size / model / …）
        required:        客户要求
        offered:         供应商提供
        level:           匹配程度
        substitutable:   不同时是否可替代
        substitution_impact: 替代的影响（客户可懂的语言）
        needs_customer_confirmation
    """

    spec_name: str
    required: str
    offered: str | None
    level: SpecMatchLevel
    substitutable: bool | None = None
    substitution_impact: str | None = None
    needs_customer_confirmation: bool = False
    customer_confirmation: ProvenanceSummary | None = None
    product_id: ProductId | None = None
    evidence_ref: ArtifactId | None = None


@dataclass(frozen=True)
class MatchExplanation:
    """匹配结果——**可解释的结构，不是相似度分数**。

    「87% 相似」对销售毫无用处；「材质和尺寸完全一致，表面处理是
    拉丝不是抛光，可替代但外观有差异，需要客户确认」他可以直接
    拿去跟客户说。

    字段：
        rung:            命中的梯级
        comparisons:     逐项规格对比
        summary:         一句话结论（从 comparisons 生成，不独立编写）
    """

    rung: MatchLadderRung
    comparisons: list[SpecComparison]
    summary: str

    @property
    def has_unknowns(self) -> bool:
        return any(item.level is SpecMatchLevel.UNKNOWN for item in self.comparisons)

    @property
    def requires_customer_confirmation(self) -> bool:
        return any(item.needs_customer_confirmation for item in self.comparisons)


class CaseState(str, Enum):
    OPENED = "opened"
    DISCOVERING = "discovering"
    VERIFYING = "verifying"
    CANDIDATES_READY = "candidates_ready"
    HANDED_TO_COSTING = "handed_to_costing"
    FAILED = "failed"


ADMISSION_RANKING_VERSION = "need-cluster-admission-v1"
"""当前准入排序语义的不可变版本号；改变规则必须发布新版本。"""


class AdmissionState(str, Enum):
    """寻源自动准入状态；与既有 ``CaseState`` 分离。"""

    WAITING = "waiting"
    STARTING = "starting"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class AdmissionBlockedReason(str, Enum):
    """准入无法继续的固定、可修复原因；禁止存储底层异常文本。"""

    PRIORITY_FACTS_INVALID = "priority_facts_invalid"
    CASE_STATE_MISMATCH = "case_state_mismatch"


ADMISSION_STATE_TRANSITIONS: dict[AdmissionState, frozenset[AdmissionState]] = {
    AdmissionState.WAITING: frozenset(
        {AdmissionState.STARTING, AdmissionState.BLOCKED}
    ),
    AdmissionState.STARTING: frozenset(
        {AdmissionState.WAITING, AdmissionState.ADMITTED, AdmissionState.BLOCKED}
    ),
    AdmissionState.ADMITTED: frozenset(),
    AdmissionState.BLOCKED: frozenset({AdmissionState.WAITING}),
}
"""准入状态转换表；终态 ``admitted`` 永不回退或改写。"""


CASE_STATE_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.OPENED: frozenset({CaseState.DISCOVERING, CaseState.FAILED}),
    CaseState.DISCOVERING: frozenset({CaseState.VERIFYING, CaseState.FAILED}),
    CaseState.VERIFYING: frozenset({CaseState.CANDIDATES_READY, CaseState.FAILED}),
    CaseState.CANDIDATES_READY: frozenset({CaseState.HANDED_TO_COSTING}),
    CaseState.HANDED_TO_COSTING: frozenset(),
    CaseState.FAILED: frozenset(),
}
"""V2 案例合法转换表；非法路径不得靠调用方约定绕过。"""


class SupplyOptionSource(str, Enum):
    """成本交接供给选项的来源。"""

    EXISTING_PRODUCT = "existing_product"
    SUPPLIER_CANDIDATE = "supplier_candidate"


class PublicPlanStatus(str, Enum):
    """公开寻源计划状态；案例仍沿用既有粗粒度状态机。"""

    PENDING_CONFIRMATION = "pending_confirmation"
    AUTHORIZED = "authorized"
    RUNNING = "running"
    EXHAUSTED = "exhausted"
    BLOCKED = "blocked"
    COMPLETED = "completed"


PUBLIC_PLAN_TRANSITIONS: dict[PublicPlanStatus, frozenset[PublicPlanStatus]] = {
    PublicPlanStatus.PENDING_CONFIRMATION: frozenset({PublicPlanStatus.AUTHORIZED}),
    PublicPlanStatus.AUTHORIZED: frozenset(
        {PublicPlanStatus.RUNNING, PublicPlanStatus.BLOCKED}
    ),
    PublicPlanStatus.RUNNING: frozenset(
        {
            PublicPlanStatus.EXHAUSTED,
            PublicPlanStatus.BLOCKED,
            PublicPlanStatus.COMPLETED,
        }
    ),
    PublicPlanStatus.EXHAUSTED: frozenset(),
    PublicPlanStatus.BLOCKED: frozenset(),
    PublicPlanStatus.COMPLETED: frozenset(),
}


class SourcingStopCode(str, Enum):
    """寻源停止原因；细节只保存安全、结构化说明。"""

    APPROVAL_REQUIRED = "approval_required"
    QUOTA_STATUS_UNKNOWN = "quota_status_unknown"
    PAID_USAGE_ENABLED = "paid_usage_enabled"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PAGE_ACCESS_FORBIDDEN = "page_access_forbidden"
    LOGIN_OR_CAPTCHA = "login_or_captcha"
    UNSAFE_REDIRECT = "unsafe_redirect"
    NO_SEARCH_RESULTS = "no_search_results"
    NO_VERIFIABLE_SUPPLIER = "no_verifiable_supplier"
    NO_QUALIFIED_CANDIDATE = "no_qualified_candidate"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    OPPORTUNITY_REQUIRED = "opportunity_required"
    NEED_INCOMPLETE = "need_incomplete"
    PLAN_CONFIRMATION_REQUIRED = "plan_confirmation_required"
    FREE_QUOTA_UNAVAILABLE = "free_quota_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_QUALIFIED_SUPPLY = "no_qualified_supply"
    MANUAL_STOP = "manual_stop"


class SourcingStopStage(str, Enum):
    """停止位置；仅允许不会泄露请求或 Provider 原文的固定阶段。"""

    INTAKE = "intake"
    PLAN = "plan"
    QUOTA = "quota"
    PROVIDER = "provider"
    PAGE = "page"
    CANDIDATE = "candidate"
    REVIEW = "review"
    COST_HANDOFF = "cost_handoff"


@dataclass(frozen=True)
class SourcingStopDetail:
    """可持久化的安全停止上下文；禁止保存自由文本和 Provider 载荷。"""

    stage: SourcingStopStage
    query_index: int | None = None
    provider_http_status: int | None = None
    observed_count: int | None = None
    configured_limit: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, SourcingStopStage):
            raise ValidationError("stop_detail.stage 类型无效")
        for field_name in ("query_index", "observed_count", "configured_limit"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValidationError(f"stop_detail.{field_name} 必须是非负整数")
        if self.provider_http_status is not None and (
            isinstance(self.provider_http_status, bool)
            or not isinstance(self.provider_http_status, int)
            or not 100 <= self.provider_http_status <= 599
        ):
            raise ValidationError(
                "stop_detail.provider_http_status 必须是有效 HTTP 状态码"
            )


def _require_aware_time(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} 必须含时区")


def _plan_hash(scope: dict[str, object]) -> str:
    """对精确计划范围做稳定 JSON 哈希；不包含确认人等审计字段。"""

    encoded = json.dumps(
        scope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def candidate_set_hash(candidate_ids: tuple[SupplierCandidateId, ...]) -> str:
    """对已排序候选 ID 集合生成稳定哈希，作为投影 generation。"""

    encoded = json.dumps(
        [str(item) for item in candidate_ids],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _normalized_evidence_value(value: str | int | object) -> str:
    """只消除大小写与空白差异，不做可能改变商业语义的模糊匹配。"""

    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return str(value)


@dataclass(frozen=True)
class PublicSourcingPlan:
    """版本化公开寻源计划；确认永远绑定当时看到的精确哈希。"""

    tenant_id: TenantId
    plan_id: SourcingPlanId
    case_id: SourcingCaseId
    target_countries: tuple[str, ...]
    product_category: str
    queries: tuple[PublicSourcingQuery, ...]
    max_search_queries: int
    max_pages_read: int
    provider: str
    search_depth: str
    usage_credits_remaining: int
    worst_case_credits: int
    version: int
    expected_case_version: int
    plan_hash: str
    created_at: datetime
    status: PublicPlanStatus = PublicPlanStatus.PENDING_CONFIRMATION
    confirmed_by: EmployeeId | None = None
    confirmed_at: datetime | None = None
    authorized_plan_hash: str | None = None

    @classmethod
    def create(
        cls,
        tenant_id: TenantId,
        command: PublicSourcingPlanCommand,
        *,
        created_at: datetime,
    ) -> PublicSourcingPlan:
        """从已校验命令创建待确认计划，并生成规范化内容哈希。"""

        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or tenant_id != tenant_id.strip()
        ):
            raise ValidationError("公开寻源计划 tenant_id 无效")
        if not isinstance(command, PublicSourcingPlanCommand):
            raise ValidationError("公开寻源计划命令类型无效")
        _require_aware_time(created_at, "created_at")
        values = command.model_dump(mode="json")
        values["tenant_id"] = str(tenant_id)
        command_values = command.model_dump(mode="python")
        command_values["queries"] = command.queries
        return cls(
            tenant_id=tenant_id,
            **command_values,
            plan_hash=_plan_hash(values),
            created_at=created_at,
        )

    def _scope(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "plan_id": str(self.plan_id),
            "case_id": str(self.case_id),
            "target_countries": self.target_countries,
            "product_category": self.product_category,
            "queries": self.queries,
            "max_search_queries": self.max_search_queries,
            "max_pages_read": self.max_pages_read,
            "provider": self.provider,
            "search_depth": self.search_depth,
            "usage_credits_remaining": self.usage_credits_remaining,
            "worst_case_credits": self.worst_case_credits,
            "version": self.version,
            "expected_case_version": self.expected_case_version,
        }
        values.update(changes)
        return values

    def confirm(
        self, confirmed_by: EmployeeId, *, confirmed_at: datetime
    ) -> PublicSourcingPlan:
        """老板确认当前哈希；重复或过期状态一律拒绝。"""

        if self.status is not PublicPlanStatus.PENDING_CONFIRMATION:
            raise SourcingPlanStaleError("只有待确认计划可以确认")
        if not str(confirmed_by).strip():
            raise ValidationError("confirmed_by 不能为空")
        _require_aware_time(confirmed_at, "confirmed_at")
        return replace(
            self,
            status=PublicPlanStatus.AUTHORIZED,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
            authorized_plan_hash=self.plan_hash,
        )

    def replace_scope(
        self,
        *,
        target_countries: tuple[str, ...] | None = None,
        product_category: str | None = None,
        queries: tuple[PublicSourcingQuery, ...] | None = None,
        max_search_queries: int | None = None,
        max_pages_read: int | None = None,
        usage_credits_remaining: int | None = None,
        worst_case_credits: int | None = None,
    ) -> PublicSourcingPlan:
        """待确认计划改变成本/范围时创建新版本；已确认计划禁止原位改写。"""

        if self.status is not PublicPlanStatus.PENDING_CONFIRMATION:
            raise SourcingPlanStaleError("已确认计划范围不可改写，必须创建新版本")
        changes: dict[str, object] = {"version": self.version + 1}
        for key, value in {
            "target_countries": target_countries,
            "product_category": product_category,
            "queries": queries,
            "max_search_queries": max_search_queries,
            "max_pages_read": max_pages_read,
            "usage_credits_remaining": usage_credits_remaining,
            "worst_case_credits": worst_case_credits,
        }.items():
            if value is not None:
                changes[key] = value
        command = PublicSourcingPlanCommand.model_validate(self._scope(**changes))
        return PublicSourcingPlan.create(
            self.tenant_id,
            command,
            created_at=self.created_at,
        )

    def transition_to(self, target: PublicPlanStatus) -> PublicSourcingPlan:
        """依据显式状态表返回下一状态的不可变计划。"""

        if not isinstance(target, PublicPlanStatus) or target not in PUBLIC_PLAN_TRANSITIONS[self.status]:
            allowed = ",".join(
                sorted(item.value for item in PUBLIC_PLAN_TRANSITIONS[self.status])
            ) or "none"
            raise InvalidStateTransition(
                f"公开寻源计划不能从 {self.status.value} 转为 "
                f"{getattr(target, 'value', target)}；允许：{allowed}"
            )
        if self.authorized_plan_hash != self.plan_hash:
            raise SourcingPlanStaleError("计划哈希与授权哈希不一致")
        return replace(self, status=target)


class PriceRejectionReason(str, Enum):
    """候选价格被拒的原因。核验清单的输出之一。"""

    BAIT_PRICE = "bait_price"
    """诱导性最低价：远低于市场且无对应数量档。用它算成本，
    真实下单时利润直接消失——亏本报价最常见的来源。"""

    VAGUE_RANGE = "vague_range"
    """模糊区间（"$1–$10"）。区间两端差数倍的价格没有信息量。"""

    UNIT_UNCLEAR = "unit_unclear"
    """计价单位不明：件、套、公斤还是箱。"""

    QUANTITY_TIER_MISSING = "quantity_tier_missing"
    """没说这个价对应什么起订量。"""

    CURRENCY_UNCLEAR = "currency_unclear"
    VERIFICATION_INCOMPLETE = "verification_incomplete"
    MOQ_NOT_MET = "moq_not_met"
    QUALIFIED_LIMIT_REACHED = "qualified_limit_reached"


@dataclass(frozen=True)
class EvidenceSnapshot:
    """候选证据快照。

    每个候选**必须有**。没有哈希，页面改版后无法证明报价时看到的
    是什么——对内无法复盘，对供应商无法对质。

    字段：
        url, observed_at
        content_hash:    页面内容哈希
        artifact_ref:    网页快照与截图在 artifact_store 的引用
    """

    url: str
    observed_at: datetime
    content_hash: str
    artifact_ref: str


@dataclass(frozen=True)
class LadderCheck:
    """不可变的单级匹配检查事实。"""

    check_id: str
    tenant_id: TenantId
    case_id: SourcingCaseId
    sequence_number: int
    rung: MatchLadderRung
    outcome: LadderOutcome
    input_snapshot: dict[str, object]
    input_snapshot_hash: str
    conclusion: str
    match_object_type: str | None
    match_object_id: str | None
    spec_comparisons: tuple[SpecComparison, ...]
    evidence_refs: tuple[str, ...]
    checked_by: EmployeeId
    checked_at: datetime


class SourcingSearchExecutionStatus(str, Enum):
    """单次 Provider 请求的持久回执状态。"""

    SUCCEEDED = "succeeded"
    NO_RESULTS = "no_results"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


@dataclass(frozen=True)
class SourcingSearchExecution:
    """搜索定位回执；locator 不是 Evidence。"""

    execution_id: str
    tenant_id: TenantId
    case_id: SourcingCaseId
    plan_id: SourcingPlanId
    run_id: RunId
    plan_hash: str
    query_index: int
    request_key: str
    query_hash: str
    locator_results: tuple[dict[str, object], ...]
    provider_status: SourcingSearchExecutionStatus
    created_at: datetime
    completed_at: datetime | None = None


class SourcingReconciliationStatus(str, Enum):
    """不确定搜索请求的人工核对状态。"""

    REQUIRED = "required"
    CONFIRMED_CONSUMED = "confirmed_consumed"
    CONFIRMED_NOT_CONSUMED = "confirmed_not_consumed"


@dataclass(frozen=True)
class SourcingSearchReconciliation:
    """只增的人工核对事实，不保存 Provider 原始敏感响应。"""

    reconciliation_id: str
    tenant_id: TenantId
    execution_id: str
    status: SourcingReconciliationStatus
    reason: str
    provider_usage_artifact_ref: ArtifactId
    created_at: datetime
    reconciled_by: EmployeeId | None = None
    reconciled_at: datetime | None = None


@dataclass
class SupplierCandidate:
    """候选供应商。

    字段：
        candidate_id, tenant_id, case_id
        supplier_name, source_platform
        product_title
        verified_specs:      逐项核验结果
        indicative_price_tiers: 各数量档参考价（**全部 INDICATIVE**——
                             硬边界 7 的系统入口就在这里）
        moq, price_unit, currency
        evidence:            证据快照
        match:               匹配解释
        rejected, rejection_reasons
        verified_by:         核验人（Phase 1 是员工）
        created_at
    """

    candidate_id: SupplierCandidateId
    tenant_id: TenantId
    case_id: SourcingCaseId
    supplier_name: str
    product_title: str
    created_at: datetime
    source_platform: str | None = None
    observed_facts: dict[str, SourcingObservedFact] = field(default_factory=dict)
    supplier_claims: dict[str, SourcingSupplierClaim] = field(default_factory=dict)
    match_inferences: dict[str, SourcingMatchInference] = field(default_factory=dict)
    verified_specs: list[SpecComparison] = field(default_factory=list)
    indicative_price_tiers: tuple[IndicativePriceTier, ...] = ()
    moq: int | None = None
    price_unit: str | None = None
    currency: str | None = None
    evidence: EvidenceSnapshot | None = None
    evidence_snapshots: tuple[EvidenceSnapshot, ...] = ()
    match: MatchExplanation | None = None
    rejected: bool = False
    rejection_reasons: list[PriceRejectionReason] = field(default_factory=list)
    verified_by: EmployeeId | None = None
    public_draft_source_key: str | None = None

    def passes_verification(self) -> tuple[bool, list[str]]:
        """核验清单：产品类型、材质、尺寸、型号、数量档、MOQ、
        计价单位、币种，八项 + 证据快照齐全。

        返回 ``(通过, 未通过项)``。检查全部项后一次返回，不短路。
        """
        missing: list[str] = []
        comparisons: dict[str, list[SpecComparison]] = {}
        normalized_comparisons: list[tuple[str, SpecComparison]] = []
        for item in self.verified_specs:
            name = item.spec_name.strip().casefold()
            normalized_comparisons.append((name, item))
            comparisons.setdefault(name, []).append(item)

        def add_missing(value: str) -> None:
            if value not in missing:
                missing.append(value)

        for name, items in comparisons.items():
            if len(items) > 1:
                add_missing(f"duplicate_spec:{name}")
        trusted_refs = {
            snapshot.artifact_ref
            for snapshot in self.evidence_snapshots
            if _valid_evidence_snapshot(snapshot)
        }
        facts = {
            key.strip().casefold(): value for key, value in self.observed_facts.items()
        }
        claims = {
            key.strip().casefold(): value for key, value in self.supplier_claims.items()
        }

        def has_bound_value(name: str, expected: str | int) -> bool:
            expected_value = _normalized_evidence_value(expected)
            return any(
                isinstance(item, (SourcingObservedFact, SourcingSupplierClaim))
                and str(item.evidence_ref) in trusted_refs
                and _normalized_evidence_value(item.value) == expected_value
                for item in (facts.get(name), claims.get(name))
            )

        required_spec_names = ["product_type", "material", "size"]
        # V2 Need 的型号是可选事实；只有冻结需求声明过它、因而候选带有
        # 同名比较项时才把它列为核验门槛，不能凭空制造客户需求。
        if "model" in comparisons:
            required_spec_names.append("model")
        for name in required_spec_names:
            items = comparisons.get(name, [])
            if not items:
                add_missing(name)
                continue
            for item in items:
                if (
                    item.level is SpecMatchLevel.UNKNOWN
                    or item.offered is None
                    or not item.offered.strip()
                ):
                    add_missing(name)
                if (
                    item.level is SpecMatchLevel.DIFFERENT
                    and item.substitutable is not True
                ):
                    add_missing(f"incompatible_spec:{name}")
                if item.needs_customer_confirmation and (
                    item.customer_confirmation is None
                    or item.customer_confirmation.source_type
                    is not SourceType.CONVERSATION
                ):
                    add_missing(f"customer_confirmation:{name}")

        for name, item in normalized_comparisons:
            if item.offered is not None and not has_bound_value(name, item.offered):
                add_missing(f"structured_spec:{name}")
        if not any(
            isinstance(item, SourcingMatchInference)
            for item in self.match_inferences.values()
        ):
            missing.append("match_inference")

        tiers_valid = bool(self.indicative_price_tiers) and all(
            tier.minimum_quantity > 0
            and tier.amount > 0
            and str(tier.evidence_ref) in trusted_refs
            for tier in self.indicative_price_tiers
        ) and len(
            {tier.minimum_quantity for tier in self.indicative_price_tiers}
        ) == len(self.indicative_price_tiers)
        if not tiers_valid:
            missing.append("quantity_tier")
        if isinstance(self.moq, bool) or not isinstance(self.moq, int) or self.moq < 1:
            missing.append("moq")
        elif not has_bound_value("moq", self.moq):
            missing.append("structured_moq")
        if self.price_unit is None or not self.price_unit.strip() or (
            {tier.unit for tier in self.indicative_price_tiers}
            != {self.price_unit}
        ):
            missing.append("price_unit")
        elif not has_bound_value("price_unit", self.price_unit):
            missing.append("structured_price_unit")
        currencies = {tier.currency for tier in self.indicative_price_tiers}
        if (
            self.currency is None
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
            or currencies != {self.currency}
        ):
            missing.append("currency")
        elif not has_bound_value("currency", self.currency):
            missing.append("structured_currency")
        if not _valid_evidence_snapshot(self.evidence):
            missing.append("evidence_snapshot")
        if self.match is None or self.match.has_unknowns:
            missing.append("match_explanation")
        missing.extend(
            f"price_rejection:{reason.value}" for reason in self.rejection_reasons
        )
        return not missing, missing


MAX_QUALIFIED_CANDIDATES = 3
"""合格候选上限。

更多候选不改善决策，只拖延决策并抬高核验成本。第四个"看起来也不错"
的候选是拖延症，不是尽职调查。
"""


@dataclass
class SourcingCase:
    """寻源案例。

    字段：
        case_id, tenant_id, need_id
        state
        ladder_checked_to:  匹配梯子查到了第几级（跳级检查用）
        candidates
        opened_at, completed_at
        failed_reason:      失败原因，回流为机会域的 NO_SUPPLY_FOUND
        assigned_to
    """

    case_id: SourcingCaseId
    tenant_id: TenantId
    need_id: ValidatedNeedId
    opened_at: datetime
    opportunity_id: OpportunityId | None = None
    state: CaseState = CaseState.OPENED
    ladder_checked_to: MatchLadderRung | None = None
    candidates: list[SupplierCandidate] = field(default_factory=list)
    completed_at: datetime | None = None
    failed_reason: str | None = None
    assigned_to: EmployeeId | None = None
    workflow_version: int = 1
    trigger_key: str | None = None
    need_snapshot: SourcingNeedSnapshot | None = None
    need_snapshot_hash: str | None = None
    active_search_plan_id: SourcingPlanId | None = None
    sealed_candidate_ids: tuple[SupplierCandidateId, ...] = ()
    candidate_set_hash: str | None = None
    candidates_verified_at: datetime | None = None
    stop_code: SourcingStopCode | None = None
    stop_detail: SourcingStopDetail | None = None
    version: int = 1
    state_changed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.stop_detail is not None and not isinstance(
            self.stop_detail, SourcingStopDetail
        ):
            raise ValidationError("stop_detail 必须是安全结构化对象")
        sealed = bool(self.sealed_candidate_ids)
        seal_metadata = (
            self.candidate_set_hash is not None
            or self.candidates_verified_at is not None
        )
        if sealed != seal_metadata or sealed and (
            self.candidate_set_hash is None
            or self.candidates_verified_at is None
        ):
            raise ValidationError("候选封存字段必须成组存在")
        if sealed:
            verified_at = self.candidates_verified_at
            if verified_at is None:
                raise ValidationError("候选封存缺少核验时间")
            if (
                tuple(sorted(self.sealed_candidate_ids, key=str))
                != self.sealed_candidate_ids
            ):
                raise ValidationError("封存候选 ID 必须精确排序")
            if len(set(self.sealed_candidate_ids)) != len(self.sealed_candidate_ids):
                raise ValidationError("封存候选 ID 不得重复")
            if self.candidate_set_hash != candidate_set_hash(self.sealed_candidate_ids):
                raise ValidationError("封存候选集合哈希不匹配")
            _require_aware_time(verified_at, "candidates_verified_at")

    def transition_to(self, target: CaseState, *, changed_at: datetime) -> None:
        """按显式转换表推进案例并递增乐观并发版本。"""

        if not isinstance(target, CaseState) or target not in CASE_STATE_TRANSITIONS[self.state]:
            allowed = ",".join(
                sorted(item.value for item in CASE_STATE_TRANSITIONS[self.state])
            ) or "none"
            raise InvalidStateTransition(
                f"寻源案例不能从 {self.state.value} 转为 "
                f"{getattr(target, 'value', target)}；允许：{allowed}"
            )
        _require_aware_time(changed_at, "changed_at")
        self.state = target
        self.state_changed_at = changed_at
        self.version += 1

    def qualified_candidates(self) -> list[SupplierCandidate]:
        """通过核验且未被拒的候选，上限 ``MAX_QUALIFIED_CANDIDATES``。"""
        qualified = [
            candidate
            for candidate in self.candidates
            if not candidate.rejected and candidate.passes_verification()[0]
        ]
        qualified.sort(key=lambda item: (item.created_at, str(item.candidate_id)))
        return qualified[:MAX_QUALIFIED_CANDIDATES]


@dataclass(frozen=True)
class SourcingSupplyOption:
    """供人工审核的规范化供给选项；来源路径必须与候选 ID 一致。"""

    option_id: SourcingSupplyOptionId
    tenant_id: TenantId
    case_id: SourcingCaseId
    source: SupplyOptionSource
    product_id: ProductId
    supplier_candidate_id: SupplierCandidateId | None
    is_qualified: bool
    created_at: datetime

    def __post_init__(self) -> None:
        expected_candidate = self.source is SupplyOptionSource.SUPPLIER_CANDIDATE
        if expected_candidate != (self.supplier_candidate_id is not None):
            raise ValidationError("供给选项来源与 supplier_candidate_id 不一致")
        _require_aware_time(self.created_at, "created_at")


@dataclass(frozen=True)
class SourcingReview:
    """人工审核事实；主选唯一，备选至多两个，提交版本不可过期。"""

    review_id: SourcingReviewId
    tenant_id: TenantId
    case_id: SourcingCaseId
    primary_option_id: SourcingSupplyOptionId
    alternate_option_ids: tuple[SourcingSupplyOptionId, ...]
    reason: str
    expected_case_version: int
    submitted_by: EmployeeId
    submitted_at: datetime
    confirmed_by: EmployeeId | None = None
    confirmed_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        review_id: SourcingReviewId,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingReviewCommand,
        submitted_by: EmployeeId,
        submitted_at: datetime,
        actual_case_version: int,
    ) -> SourcingReview:
        """从严格命令创建审核；Case 版本不一致时拒绝过期写入。"""

        if command.expected_case_version != actual_case_version:
            raise SourcingReviewStaleError("审核提交使用了过期的 Case 版本")
        _require_aware_time(submitted_at, "submitted_at")
        return cls(
            review_id=review_id,
            tenant_id=tenant_id,
            case_id=case_id,
            primary_option_id=command.primary_option_id,
            alternate_option_ids=command.alternate_option_ids,
            reason=command.reason,
            expected_case_version=command.expected_case_version,
            submitted_by=submitted_by,
            submitted_at=submitted_at,
        )

    def confirm(
        self, confirmed_by: EmployeeId, *, confirmed_at: datetime
    ) -> SourcingReview:
        """记录老板确认；历史审核不可重复确认或改写。"""

        if self.confirmed_by is not None:
            if self.confirmed_at is None:
                raise SourcingReviewStaleError("审核确认事实不完整")
            return self
        _require_aware_time(confirmed_at, "confirmed_at")
        return replace(
            self,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
        )


def _require_utc_time(value: datetime, field_name: str) -> None:
    """拒绝非 UTC 或无时区时间，避免同一等待事实得到不同排序或哈希。"""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError(f"{field_name} 必须是 UTC 时间")


def _require_nonempty_identifier(value: object, field_name: str) -> None:
    """在领域边界拒绝空白业务标识，避免不可审计的准入记录。"""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")


@dataclass(frozen=True)
class SourcingPrioritySnapshot:
    """不可变的准入排序事实。

    仅保存簇规模、等待时间和稳定 Need 标识所需事实；数量、国家、规格、
    Provenance、置信度和利润均不得混入，防止将不同需求错误合并或改变 v1 排序。
    """

    tenant_id: TenantId
    snapshot_id: SourcingPrioritySnapshotId
    admission_id: SourcingAdmissionId
    case_id: SourcingCaseId
    need_id: ValidatedNeedId
    cluster_id: NeedClusterId | None
    cluster_member_count: int
    ready_at: datetime
    ranking_version: str
    facts_observed_at: datetime
    facts_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "tenant_id",
            "snapshot_id",
            "admission_id",
            "case_id",
            "need_id",
        ):
            _require_nonempty_identifier(getattr(self, field_name), field_name)
        if self.cluster_id is not None:
            _require_nonempty_identifier(self.cluster_id, "cluster_id")
        if (
            isinstance(self.cluster_member_count, bool)
            or not isinstance(self.cluster_member_count, int)
            or self.cluster_member_count < 1
        ):
            raise ValidationError("cluster_member_count 必须是正整数")
        if self.cluster_id is None and self.cluster_member_count != 1:
            raise ValidationError("未归簇需求的 cluster_member_count 必须为 1")
        if self.ranking_version != ADMISSION_RANKING_VERSION:
            raise ValidationError("ranking_version 不受支持")
        if (
            not isinstance(self.facts_hash, str)
            or len(self.facts_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.facts_hash)
        ):
            raise ValidationError("facts_hash 必须是小写 SHA-256")
        for field_name in ("ready_at", "facts_observed_at", "created_at"):
            _require_utc_time(getattr(self, field_name), field_name)
        if self.ready_at > self.created_at:
            raise ValidationError("ready_at 不得晚于 created_at")
        if self.facts_observed_at > self.created_at:
            raise ValidationError("facts_observed_at 不得晚于 created_at")
        if self.facts_hash != canonical_priority_facts_hash(
            need_id=self.need_id,
            cluster_id=self.cluster_id,
            cluster_member_count=self.cluster_member_count,
            ready_at=self.ready_at,
            facts_observed_at=self.facts_observed_at,
            ranking_version=self.ranking_version,
        ):
            raise ValidationError("facts_hash 与排序事实不一致")


@dataclass(frozen=True)
class SourcingAdmission:
    """一个 Validated Need 对应一个、tenant-bound 的寻源准入记录。

    Case 业务状态继续由 ``SourcingCase`` 管理；本实体只表达自动流程是否获准
    启动。所有状态事实被冻结在返回的新对象中，调用方必须经 Repository 持久化。
    ``waiting`` 必须指向排序快照；无快照的 ``blocked`` 仅表示排序事实永久无效，
    因此 ``case_state_mismatch`` 必须保留它所核对的既有快照。
    ``admission_requested_by`` 仅保存已鉴权人工 claim 的内部审计意图：租约释放与
    重领必须保留它；进入 ``admitted`` 时复制到 ``admitted_by``，进入任一终态后清除。
    """

    tenant_id: TenantId
    admission_id: SourcingAdmissionId
    case_id: SourcingCaseId
    need_id: ValidatedNeedId
    state: AdmissionState
    ready_at: datetime
    current_snapshot_id: SourcingPrioritySnapshotId | None
    claim_token: str | None
    claim_expires_at: datetime | None
    workflow_run_id: RunId | None
    blocked_reason: AdmissionBlockedReason | None
    admitted_at: datetime | None
    admitted_by: str | None
    created_at: datetime
    updated_at: datetime
    admission_requested_by: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "admission_id", "case_id", "need_id"):
            _require_nonempty_identifier(getattr(self, field_name), field_name)
        if self.current_snapshot_id is not None:
            _require_nonempty_identifier(self.current_snapshot_id, "current_snapshot_id")
        for field_name in ("ready_at", "created_at", "updated_at"):
            _require_utc_time(getattr(self, field_name), field_name)
        if self.ready_at > self.created_at:
            raise ValidationError("ready_at 不得晚于 created_at")
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at 不得早于 created_at")
        if not isinstance(self.state, AdmissionState):
            raise ValidationError("admission state 无效")
        self._validate_state_fields()

    def _validate_state_fields(self) -> None:
        claim_present = self.claim_token is not None or self.claim_expires_at is not None
        admitted_values = (self.workflow_run_id, self.admitted_at, self.admitted_by)
        admitted_present = any(value is not None for value in admitted_values)
        if self.claim_token is not None:
            _require_nonempty_identifier(self.claim_token, "claim_token")
        if self.claim_expires_at is not None:
            _require_utc_time(self.claim_expires_at, "claim_expires_at")
        if self.admitted_by is not None:
            _require_nonempty_identifier(self.admitted_by, "admitted_by")
        if self.admission_requested_by is not None:
            _require_nonempty_identifier(
                self.admission_requested_by, "admission_requested_by"
            )
        if self.admitted_at is not None:
            _require_utc_time(self.admitted_at, "admitted_at")
        if self.workflow_run_id is not None:
            _require_nonempty_identifier(self.workflow_run_id, "workflow_run_id")

        if self.state is AdmissionState.WAITING:
            valid = (
                self.current_snapshot_id is not None
                and not claim_present
                and not admitted_present
                and self.blocked_reason is None
            )
        elif self.state is AdmissionState.STARTING:
            valid = (
                self.current_snapshot_id is not None
                and self.claim_token is not None
                and self.claim_expires_at is not None
                and self.claim_expires_at > self.updated_at
                and not admitted_present
                and self.blocked_reason is None
            )
        elif self.state is AdmissionState.ADMITTED:
            valid = (
                self.current_snapshot_id is not None
                and not claim_present
                and self.workflow_run_id is not None
                and self.admitted_at is not None
                and self.admitted_by is not None
                and self.admitted_at == self.updated_at
                and self.blocked_reason is None
                and self.admission_requested_by is None
            )
        else:
            valid = (
                not claim_present
                and not admitted_present
                and isinstance(self.blocked_reason, AdmissionBlockedReason)
                and (
                    self.current_snapshot_id is not None
                    or self.blocked_reason
                    is AdmissionBlockedReason.PRIORITY_FACTS_INVALID
                )
                and self.admission_requested_by is None
            )
        if not valid:
            raise ValidationError("准入状态与字段组合不一致")

    def _transition(self, target: AdmissionState, *, changed_at: datetime, **changes: object) -> SourcingAdmission:
        if target not in ADMISSION_STATE_TRANSITIONS[self.state]:
            allowed = ",".join(
                item.value for item in sorted(ADMISSION_STATE_TRANSITIONS[self.state], key=lambda item: item.value)
            ) or "无"
            raise InvalidStateTransition(
                f"寻源准入不能从 {self.state.value} 转为 {target.value}；允许：{allowed}"
            )
        _require_utc_time(changed_at, "changed_at")
        if changed_at < self.updated_at:
            raise ValidationError("changed_at 不得早于 updated_at")
        return cast(
            SourcingAdmission,
            replace(cast(Any, self), state=target, updated_at=changed_at, **changes),
        )

    def claim(
        self, claim_token: str, *, claim_expires_at: datetime, claimed_at: datetime
    ) -> SourcingAdmission:
        """从等待态取得短租约；不能绕过状态机重复启动 Workflow。"""

        return self._transition(
            AdmissionState.STARTING,
            changed_at=claimed_at,
            claim_token=claim_token,
            claim_expires_at=claim_expires_at,
        )

    def release_expired_claim(self, *, now: datetime) -> SourcingAdmission:
        """仅到期租约可释放回等待态；未知执行结果必须保留到到期。"""

        _require_utc_time(now, "now")
        if self.state is not AdmissionState.STARTING:
            return self
        if self.claim_expires_at is None or now < self.claim_expires_at:
            return self
        return self._transition(
            AdmissionState.WAITING,
            changed_at=now,
            claim_token=None,
            claim_expires_at=None,
        )

    def complete(
        self,
        claim_token: str,
        *,
        workflow_run_id: RunId,
        system_actor_id: str,
        admitted_at: datetime,
    ) -> SourcingAdmission:
        """以同一租约绑定 canonical Workflow Run，之后准入事实不可改写。"""

        if self.state is not AdmissionState.STARTING or claim_token != self.claim_token:
            raise InvalidStateTransition("寻源准入 claim token 与当前 starting 状态不匹配")
        _require_nonempty_identifier(system_actor_id, "system_actor_id")
        return self._transition(
            AdmissionState.ADMITTED,
            changed_at=admitted_at,
            claim_token=None,
            claim_expires_at=None,
            workflow_run_id=workflow_run_id,
            admitted_by=self.admission_requested_by or system_actor_id,
            admitted_at=admitted_at,
            admission_requested_by=None,
        )

    def block(
        self,
        reason: AdmissionBlockedReason,
        *,
        blocked_at: datetime,
        claim_token: str | None = None,
    ) -> SourcingAdmission:
        """以固定原因阻断可修复事实；不保存自由文本或异常内容。"""

        if not isinstance(reason, AdmissionBlockedReason):
            raise ValidationError("blocked_reason 必须是 AdmissionBlockedReason")
        if self.state is AdmissionState.STARTING and claim_token != self.claim_token:
            raise InvalidStateTransition("寻源准入 block claim token 与当前 starting 状态不匹配")
        if self.state is AdmissionState.WAITING and claim_token is not None:
            raise ValidationError("waiting 准入不得提供 claim_token")
        return self._transition(
            AdmissionState.BLOCKED,
            changed_at=blocked_at,
            claim_token=None,
            claim_expires_at=None,
            blocked_reason=reason,
            admission_requested_by=None,
        )

    def retry(self, *, retried_at: datetime) -> SourcingAdmission:
        """修复固定阻断原因后回到等待态；无快照时仍失败关闭。"""

        return self._transition(
            AdmissionState.WAITING,
            changed_at=retried_at,
            blocked_reason=None,
        )

    def with_current_snapshot(
        self, snapshot: SourcingPrioritySnapshot
    ) -> SourcingAdmission:
        """更新等待/阻断项的当前不可变快照；已启动或已准入记录禁止改写。"""

        if self.state not in {AdmissionState.WAITING, AdmissionState.BLOCKED}:
            raise InvalidStateTransition(
                f"寻源准入处于 {self.state.value} 时不能更新优先级快照；允许：waiting,blocked"
            )
        if not isinstance(snapshot, SourcingPrioritySnapshot) or (
            snapshot.tenant_id != self.tenant_id
            or snapshot.admission_id != self.admission_id
            or snapshot.case_id != self.case_id
            or snapshot.need_id != self.need_id
        ):
            raise ValidationError("优先级快照与准入记录不一致")
        if snapshot.created_at < self.updated_at:
            raise ValidationError("snapshot.created_at 不得早于 updated_at")
        if snapshot.ready_at != self.ready_at:
            raise ValidationError("snapshot.ready_at 必须等于 admission.ready_at")
        recovered_state = (
            AdmissionState.WAITING
            if self.state is AdmissionState.BLOCKED
            and self.blocked_reason is AdmissionBlockedReason.PRIORITY_FACTS_INVALID
            else self.state
        )
        return replace(
            self,
            state=recovered_state,
            current_snapshot_id=snapshot.snapshot_id,
            blocked_reason=(None if recovered_state is AdmissionState.WAITING else self.blocked_reason),
            updated_at=snapshot.created_at,
        )


def _valid_evidence_snapshot(snapshot: EvidenceSnapshot | None) -> bool:
    """核对可追溯快照的最小确定性字段，不在域内执行网络请求。"""

    if snapshot is None:
        return False
    try:
        parsed = urlsplit(snapshot.url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65_535)
        and snapshot.observed_at.tzinfo is not None
        and snapshot.observed_at.utcoffset() is not None
        and len(snapshot.content_hash) == 64
        and all(character in "0123456789abcdef" for character in snapshot.content_hash)
        and snapshot.artifact_ref.startswith("art_")
        and snapshot.artifact_ref == snapshot.artifact_ref.strip()
    )
