"""贸易机会域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from domains.opportunities.models import SortKey
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import ProspectAccountId
from shared.schemas.money import Money
from shared.schemas.provenance import Provenance


@dataclass(frozen=True)
class ValidatedNeedEvidence:
    """已验证需求的证据契约（S3-6 R5/F6）。

    从 ``NeedValidated`` / 人工录入创建机会时，必须带上这份证据——它证明
    这条需求**真的是客户本人表达过的**（硬边界 5 的「客户明确说过」），
    而不是 Agent 推断、公开企业事件或员工猜测。

    刻意复用 ``Provenance``（含 source_type/source_id/confirmed_by/confirmed_at
    与其不变量），不在本 DTO 重复来源字段——两处字段会互相打架。

    服务层强制（``service_impl.validate_validated_need_evidence``）：
    - ``level`` 必须 ≥ ``EvidenceLevel.CUSTOMER_INTEREST_REPLY``
    - ``provenance.source_type`` 仅允许 conversation/upload/employee_input
    - ``EMPLOYEE_INPUT`` 必须带真实人工确认对（confirmed_by/confirmed_at）

    字段：
        level:      证据等级（typed，来自 shared 的 EvidenceLevel）
        provenance: 指向具体客户消息/上传/员工确认的 Provenance
    """

    level: EvidenceLevel
    provenance: Provenance


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
        evidence_tier:        兼容一致性声明；只可重申 evidence.level 或
                              确定性推导后的 ConfidenceTier，不直接决定分数
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
class ProvenanceSummary:
    """字段来源的公共稳定摘要，不暴露内部 Enum、强类型 ID 或 ORM。"""

    field_name: str
    source_type: str
    source_id: str
    extracted_by: str
    extracted_at: datetime
    confirmed_by: str | None
    confirmed_at: datetime | None
    source_url: str | None
    page_hash: str | None


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
    provenance: list[ProvenanceSummary] = field(default_factory=list)

    def __post_init__(self) -> None:
        """复制来源列表，避免 frozen DTO 仍与调用方共享可变容器。"""
        object.__setattr__(self, "provenance", list(self.provenance))


@dataclass(frozen=True)
class HandoffQueueItemView:
    """待接管队列的最小 packet 摘要；等待时长对 pending 行始终为整数。"""

    handoff_id: str
    opportunity_id: str
    trigger: str
    account_name: str
    country: str
    why_valuable: str
    customer_verbatim: str
    requested_at: datetime
    wait_seconds: int
    state: str
    assigned_to: str | None = None
    suggested_next_step: str | None = None
    missing_information: list[str] = field(default_factory=list)
    evidence_links: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """复制两个列表字段，防止内部 packet 后续修改污染已返回 DTO。"""
        object.__setattr__(self, "missing_information", list(self.missing_information))
        object.__setattr__(self, "evidence_links", list(self.evidence_links))


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


@dataclass(frozen=True)
class NotificationAudienceTarget:
    """通知受众所需唯一关联；不暴露机会详情或自由文本。"""

    account_id: ProspectAccountId
