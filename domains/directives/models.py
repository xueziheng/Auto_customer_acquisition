"""老板指令域实体。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import DirectiveId, EmployeeId, TenantId


class DirectiveObjective(str, Enum):
    """指令目标类型。决定后续解析出哪些配置段。"""

    DISCOVER_AND_VALIDATE_DEMAND = "discover_and_validate_demand"
    FOCUS_EXISTING_NEEDS = "focus_existing_needs"
    """「这个月减少探索，优先处理已经验证的需求。」"""

    PAUSE_MARKET = "pause_market"
    REASSIGN_TERRITORY = "reassign_territory"
    ADJUST_BUDGET = "adjust_budget"
    ADJUST_HANDOFF_RULES = "adjust_handoff_rules"


@dataclass(frozen=True)
class MarketAssignment:
    """市场分配段：国家 → 负责员工。"""

    country: str
    owner: EmployeeId


@dataclass(frozen=True)
class DiscoveryConfig:
    """探索配置段。

    字段：
        need_first_ratio:      需求优先探索占比（0–100）
        catalog_assisted_ratio: 现有能力辅助占比（0–100，两者和为 100）
        focus_categories:      重点品类
        excluded_buyer_types:  排除的客户类型（「不要再找零售店」）

    **Phase 1 默认现有能力为主**（need_first 约 30）：冷启动时
    需求优先假设未经验证，供应端确定性是仅有的优势。老板可随时调。
    Phase 2 由自适应分配器按「每单位成本产出的已验证需求」调整，
    老板仍可手动覆盖。
    """

    need_first_ratio: int
    catalog_assisted_ratio: int
    focus_categories: list[str] = field(default_factory=list)
    excluded_buyer_types: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiscoverySearchQueryConfig:
    """老板确认的一条公开网页搜索查询及其单次结果上限。"""

    query: str
    country: str
    category: str
    limit: int
    discovery_lane: str | None = None


@dataclass(frozen=True)
class DemandDiscoveryConfig:
    """需求探索工作流的完整不可变输入；所有预算必须显式确认。"""

    objective: str
    queries: list[DiscoverySearchQueryConfig]
    target_countries: list[str]
    target_categories: list[str]
    excluded_countries: list[str]
    excluded_categories: list[str]
    max_search_queries: int
    max_pages_read: int
    max_signals: int
    max_hypotheses: int
    minimum_confidence_tier: str
    strategy_group: str
    campaign_id: str
    role_hints: list[str]
    assessment_ref: str
    execution_mode: str = "outreach_preparation"


@dataclass(frozen=True)
class OutreachBounds:
    """触达边界段。落到 Campaign 校验。"""

    primary_channel: str
    max_sequence_messages: int
    stop_on_reply: bool


@dataclass(frozen=True)
class HandoffRules:
    """转人工规则段。

    字段：
        manager:    接收高意向通知的经理
        triggers:   触发条件（字符串，值来自 opportunities.HandoffTrigger）
    """

    manager: EmployeeId
    triggers: list[str]


@dataclass(frozen=True)
class SourcingAdmissionConfig:
    """老板确认的 Need Cluster 寻源准入完整配置。"""

    mode: str
    automatic_admission_enabled: bool
    batch_limit: int


@dataclass(frozen=True)
class DirectiveContent:
    """指令的结构化内容。所有段可选——一条指令通常只动其中几段。"""

    objective: DirectiveObjective
    market_assignments: list[MarketAssignment] = field(default_factory=list)
    discovery: DiscoveryConfig | None = None
    demand_discovery: DemandDiscoveryConfig | None = None
    outreach: OutreachBounds | None = None
    handoff: HandoffRules | None = None
    paused_markets: list[str] = field(default_factory=list)
    monthly_budget_credits: int | None = None
    notes: str | None = None
    sourcing_admission: SourcingAdmissionConfig | None = None


class ProposalState(str, Enum):
    PENDING_CONFIRMATION = "pending_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    """提案超时未确认自动过期。过期的提案不能再被确认——
    老板一周前没确认的理解，现在的系统状态可能已经不适用。"""


@dataclass
class DirectiveProposal:
    """指令提案 —— 解析产物，**未生效**。

    与生效的 ``Directive`` 是两个对象，这是「解析结果必须经确认」
    的结构保证：不存在从原始文本直达生效配置的代码路径。

    字段：
        proposal_id, tenant_id
        raw_text:            老板的原话，永久保留
        parsed:              解析出的结构
        interpretation_summary: 给老板看的理解摘要（人话）
        expected_behavior_changes: **预计行为变化清单**——
            「这会暂停 3 个活跃 Campaign，影响 47 个进行中的序列」。
            没有这个，老板确认的只是字段名，发现不了误解析。
        parsed_by:           解析的模型版本
        state
        created_at, decided_at, decided_by
    """

    proposal_id: str
    tenant_id: TenantId
    raw_text: str
    parsed: DirectiveContent
    interpretation_summary: str
    expected_behavior_changes: list[str]
    parsed_by: str
    created_at: datetime
    state: ProposalState = ProposalState.PENDING_CONFIRMATION
    decided_at: datetime | None = None
    decided_by: EmployeeId | None = None
    base_directive_version: int | None = None


@dataclass
class Directive:
    """生效的指令版本。**不可变**——修改就是新版本。

    字段：
        directive_id, tenant_id
        version
        content
        source_proposal_id:  来自哪个提案
        activated_at, activated_by
        superseded_at:       被更新版本替代的时间
        rollback_of:         若本版本是回滚，指向被恢复的版本号
    """

    directive_id: DirectiveId
    tenant_id: TenantId
    version: int
    content: DirectiveContent
    source_proposal_id: str
    activated_at: datetime
    activated_by: EmployeeId
    superseded_at: datetime | None = None
    rollback_of: int | None = None
