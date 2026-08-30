"""寻源域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from hashlib import sha256
from urllib.parse import urlsplit

from domains.sourcing.errors import SourcingPlanStaleError, SourcingReviewStaleError
from domains.sourcing.schemas import (
    PublicSourcingPlanCommand,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingReviewCommand,
    SourcingSupplierClaim,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money


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


CASE_STATE_TRANSITIONS: dict[CaseState, frozenset[CaseState]] = {
    CaseState.OPENED: frozenset({CaseState.DISCOVERING, CaseState.FAILED}),
    CaseState.DISCOVERING: frozenset({CaseState.VERIFYING, CaseState.FAILED}),
    CaseState.VERIFYING: frozenset({CaseState.CANDIDATES_READY, CaseState.FAILED}),
    CaseState.CANDIDATES_READY: frozenset(
        {CaseState.HANDED_TO_COSTING, CaseState.FAILED}
    ),
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


@dataclass(frozen=True)
class PublicSourcingPlan:
    """版本化公开寻源计划；确认永远绑定当时看到的精确哈希。"""

    tenant_id: TenantId
    plan_id: SourcingPlanId
    case_id: SourcingCaseId
    target_countries: tuple[str, ...]
    product_category: str
    queries: tuple[str, ...]
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
        return cls(
            tenant_id=tenant_id,
            **command.model_dump(mode="python"),
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
        queries: tuple[str, ...] | None = None,
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
    run_id: str
    plan_hash: str
    query_index: int
    request_key: str
    query_text: str
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
    provider_receipt: dict[str, object]
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
    indicative_price_tiers: dict[int, Money] = field(default_factory=dict)
    moq: int | None = None
    price_unit: str | None = None
    currency: str | None = None
    evidence: EvidenceSnapshot | None = None
    evidence_snapshots: tuple[EvidenceSnapshot, ...] = ()
    match: MatchExplanation | None = None
    rejected: bool = False
    rejection_reasons: list[PriceRejectionReason] = field(default_factory=list)
    verified_by: EmployeeId | None = None

    def passes_verification(self) -> tuple[bool, list[str]]:
        """核验清单：产品类型、材质、尺寸、型号、数量档、MOQ、
        计价单位、币种，八项 + 证据快照齐全。

        返回 ``(通过, 未通过项)``。检查全部项后一次返回，不短路。
        """
        missing: list[str] = []
        comparisons = {item.spec_name.strip().casefold(): item for item in self.verified_specs}
        for name in ("product_type", "material", "size", "model"):
            item = comparisons.get(name)
            if (
                item is None
                or item.level is SpecMatchLevel.UNKNOWN
                or item.offered is None
                or not item.offered.strip()
            ):
                missing.append(name)

        for name in ("product_type", "material", "size", "model"):
            observed = self.observed_facts.get(name)
            claimed = self.supplier_claims.get(name)
            if not isinstance(observed, SourcingObservedFact) and not isinstance(
                claimed, SourcingSupplierClaim
            ):
                missing.append(f"structured_spec:{name}")
        if not any(
            isinstance(item, SourcingMatchInference)
            for item in self.match_inferences.values()
        ):
            missing.append("match_inference")

        tiers_valid = bool(self.indicative_price_tiers) and all(
            not isinstance(quantity, bool)
            and isinstance(quantity, int)
            and quantity > 0
            and price.amount > 0
            for quantity, price in self.indicative_price_tiers.items()
        )
        if not tiers_valid:
            missing.append("quantity_tier")
        if isinstance(self.moq, bool) or not isinstance(self.moq, int) or self.moq < 1:
            missing.append("moq")
        if self.price_unit is None or not self.price_unit.strip():
            missing.append("price_unit")
        currencies = {
            str(price.currency) for price in self.indicative_price_tiers.values()
        }
        if (
            self.currency is None
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
            or currencies != {self.currency}
        ):
            missing.append("currency")
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
    stop_code: SourcingStopCode | None = None
    stop_detail: SourcingStopDetail | None = None
    version: int = 1
    state_changed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.stop_detail is not None and not isinstance(
            self.stop_detail, SourcingStopDetail
        ):
            raise ValidationError("stop_detail 必须是安全结构化对象")

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
            raise SourcingReviewStaleError("审核已经确认")
        _require_aware_time(confirmed_at, "confirmed_at")
        return replace(
            self,
            confirmed_by=confirmed_by,
            confirmed_at=confirmed_at,
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
