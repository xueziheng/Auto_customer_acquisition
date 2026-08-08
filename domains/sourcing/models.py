"""寻源域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    EmployeeId,
    SourcingCaseId,
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
        raise NotImplementedError

    @property
    def requires_customer_confirmation(self) -> bool:
        raise NotImplementedError


class CaseState(str, Enum):
    OPENED = "opened"
    DISCOVERING = "discovering"
    VERIFYING = "verifying"
    CANDIDATES_READY = "candidates_ready"
    HANDED_TO_COSTING = "handed_to_costing"
    FAILED = "failed"


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


@dataclass
class SupplierCandidate:
    """候选供应商。

    字段：
        candidate_id, tenant_id, case_id
        supplier_name, source_platform
        product_title
        verified_specs:      逐项核验结果
        quoted_prices:       各数量档价格（**全部 INDICATIVE**——
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
    verified_specs: list[SpecComparison] = field(default_factory=list)
    quoted_prices: dict[int, Money] = field(default_factory=dict)
    moq: int | None = None
    price_unit: str | None = None
    currency: str | None = None
    evidence: EvidenceSnapshot | None = None
    match: MatchExplanation | None = None
    rejected: bool = False
    rejection_reasons: list[PriceRejectionReason] = field(default_factory=list)
    verified_by: EmployeeId | None = None

    def passes_verification(self) -> tuple[bool, list[str]]:
        """核验清单：产品类型、材质、尺寸、型号、数量档、MOQ、
        计价单位、币种，八项 + 证据快照齐全。

        返回 ``(通过, 未通过项)``。检查全部项后一次返回，不短路。
        """
        raise NotImplementedError


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

    def qualified_candidates(self) -> list[SupplierCandidate]:
        """通过核验且未被拒的候选，上限 ``MAX_QUALIFIED_CANDIDATES``。"""
        raise NotImplementedError
