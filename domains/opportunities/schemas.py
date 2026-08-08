"""贸易机会域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from domains.opportunities.models import SortKey
from shared.schemas.money import Money
from shared.schemas.provenance import Provenance


@dataclass(frozen=True)
class OpportunityCreateRequest:
    """从已验证需求创建机会的入参。

    刻意用扁平字段而不是接收 ``ValidatedNeed`` 实体：本域不 import
    需求域，由上层把需要的字段传进来（域间零依赖）。打分门槛输入
    （``category_allowed``/``minimum_order_value``/``supply_available``）
    也由上层按 Playbook/寻源状态填好。

    字段：
        need_id, account_id, account_name, country
        product_category, quantity, spec_summary, application,
        destination, required_by, target_price
        current_supply_solution, current_supply_problem
        evidence_tier:        证据档位（字符串，来自 shared 的枚举值）
        has_verified_contact
        category_allowed / minimum_order_value / supply_available / is_repeat_buyer_likely
        field_provenance:     关键字段（CRITICAL_FIELDS）的来源；present 必须各有、
                              且不得为 AGENT_INFERENCE（机会只存事实）
        estimated_order_value
    """

    need_id: str
    account_id: str
    account_name: str
    country: str
    product_category: str
    evidence_tier: str
    has_verified_contact: bool
    category_allowed: bool
    minimum_order_value: Money
    supply_available: bool | None = None
    is_repeat_buyer_likely: bool = False
    field_provenance: dict[str, Provenance] = field(default_factory=dict)
    quantity: int | None = None
    spec_summary: str | None = None
    application: str | None = None
    destination: str | None = None
    required_by: date | None = None
    target_price: Money | None = None
    current_supply_solution: str | None = None
    current_supply_problem: str | None = None
    estimated_order_value: Money | None = None


@dataclass(frozen=True)
class HandoffCreateRequest:
    """请求人工接管的完整素材（由上层/对话识别填充）。

    ``customer_verbatim`` 必须是客户原话；``customer_verbatim_provenance``
    必须指向原话来源（conversation/upload/employee_input）——
    ``evidence_links`` 不能替代 Provenance。
    """

    opportunity_id: str
    trigger: str
    account_name: str
    country: str
    why_valuable: str
    customer_verbatim: str
    customer_verbatim_provenance: Provenance
    how_we_found_them: str | None = None
    validated_need_summary: str | None = None
    missing_information: list[str] = field(default_factory=list)
    conversation_summary: str | None = None
    already_sent: list[str] = field(default_factory=list)
    commitments_made: list[str] = field(default_factory=list)
    suggested_next_step: str | None = None
    evidence_links: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreExplanation:
    """打分说明，供界面展开。

    老板点「为什么是高意向」时看到的东西。必须能回答这个问题，
    否则分数不会被信任。

    字段：
        sort_key:       字典序排序键（evidence_rank / value_band / supply_rank）
        rank_bucket:    高/中/低
        passed_gates
        failed_gates
        gate_reasons:   每个未通过门槛的具体原因
        scored_at, scorer_version
    """

    sort_key: SortKey
    rank_bucket: str
    passed_gates: list[str]
    failed_gates: list[str]
    gate_reasons: dict[str, str]
    scored_at: datetime
    scorer_version: str


@dataclass(frozen=True)
class OpportunityView:
    """机会视图。

    字段：
        opportunity_id, account_id, account_name, country
        need_id, product_category
        state
        quantity, spec_summary, destination, required_by, target_price
        current_supply_problem:  为什么我们有机会
        can_source
        estimated_cost, estimated_profit
        owner, owner_name
        next_action, next_action_due
        score:          打分说明
        loss_reason, died_at_state
        created_at
        has_pending_handoff
    """

    opportunity_id: str
    account_id: str
    account_name: str
    country: str
    need_id: str
    product_category: str
    state: str
    created_at: datetime
    quantity: int | None = None
    spec_summary: str | None = None
    destination: str | None = None
    required_by: date | None = None
    target_price: Money | None = None
    current_supply_problem: str | None = None
    can_source: bool | None = None
    estimated_cost: Money | None = None
    estimated_profit: Money | None = None
    owner: str | None = None
    owner_name: str | None = None
    next_action: str | None = None
    next_action_due: datetime | None = None
    score: ScoreExplanation | None = None
    loss_reason: str | None = None
    died_at_state: str | None = None
    has_pending_handoff: bool = False


@dataclass(frozen=True)
class HandoffPacketView:
    """接管包视图 —— 员工看到的接管内容。

    这个 View 的字段清单就是「不能只发一句话」的落地。员工读完
    应当不用再翻别的页面就能开始工作。

    字段：
        handoff_id, opportunity_id, trigger
        account_name, country
        how_we_found_them
        why_valuable
        customer_verbatim:      客户原话
        validated_need_summary
        missing_information
        conversation_summary
        already_sent
        commitments_made
        suggested_next_step
        evidence_links
        requested_at, wait_seconds
        state, assigned_to_name
    """

    handoff_id: str
    opportunity_id: str
    trigger: str
    account_name: str
    country: str
    why_valuable: str
    customer_verbatim: str
    requested_at: datetime
    state: str
    how_we_found_them: str | None = None
    validated_need_summary: str | None = None
    missing_information: list[str] = field(default_factory=list)
    conversation_summary: str | None = None
    already_sent: list[str] = field(default_factory=list)
    commitments_made: list[str] = field(default_factory=list)
    suggested_next_step: str | None = None
    evidence_links: list[str] = field(default_factory=list)
    wait_seconds: int | None = None
    assigned_to_name: str | None = None


@dataclass(frozen=True)
class HandoffQueueStats:
    """待接管队列统计 —— 接管 SLA 的度量出口。

    字段：
        queue_depth:            待接管总数
        oldest_wait_seconds:    最久等待
        by_employee:            按员工的积压数
        breached_count:         已超 SLA 的数量
        is_backlogged:          是否超过阈值
    """

    queue_depth: int
    oldest_wait_seconds: int
    by_employee: dict[str, int]
    breached_count: int
    is_backlogged: bool
