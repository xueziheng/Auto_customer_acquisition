"""贸易机会域实体。

**内部实现，其他域不得导入。** 跨域用 ``schemas.py``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    LossRecordId,
    OpportunityId,
    ProspectAccountId,
    ScoreSnapshotId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money


class OpportunityState(str, Enum):
    QUALIFIED = "qualified"
    ASSIGNED = "assigned"
    CONTACTED = "contacted"
    SOURCING = "sourcing"
    QUOTED = "quoted"
    NEGOTIATING = "negotiating"
    WON = "won"
    LOST = "lost"


ALLOWED_TRANSITIONS: dict[OpportunityState, set[OpportunityState]] = {
    OpportunityState.QUALIFIED: {OpportunityState.ASSIGNED, OpportunityState.LOST},
    OpportunityState.ASSIGNED: {OpportunityState.CONTACTED, OpportunityState.LOST},
    OpportunityState.CONTACTED: {
        OpportunityState.SOURCING,
        OpportunityState.QUOTED,
        OpportunityState.LOST,
    },
    OpportunityState.SOURCING: {OpportunityState.QUOTED, OpportunityState.LOST},
    OpportunityState.QUOTED: {OpportunityState.NEGOTIATING, OpportunityState.LOST},
    OpportunityState.NEGOTIATING: {OpportunityState.WON, OpportunityState.LOST},
    OpportunityState.WON: set(),
    OpportunityState.LOST: set(),
}
"""允许的状态转换。

写成表而不是 if 链：这张表会被反复查阅和修改，散在代码里的转换判断
迟早会出现两处不一致。非法转换抛 ``InvalidStateTransition``，
消息里带当前态、目标态和允许列表。

``CONTACTED`` 可以直接跳到 ``QUOTED``：公司现有产品完全匹配时不需要
寻源。这不是漏洞，是正常路径。
"""

# 影响商业判断且本实体存储的关键字段：present 时必须各有 Provenance（硬边界 4）。
# 推断（AGENT_INFERENCE）一律拒绝——机会只持久化事实（硬边界 5）。
CRITICAL_FIELDS = frozenset(
    {
        "account_name",
        "country",
        "quantity",
        "spec_summary",
        "application",
        "destination",
        "required_by",
        "target_price",
        "decision_maker",
        "current_supply_solution",
        "current_supply_problem",
        "can_source",
        "estimated_cost",
        "estimated_profit",
    }
)


class LossReason(str, Enum):
    """机会终结原因。**反馈闭环的骨架。**

    设计稿没有这个结构（我补的）。没有它，「根据结果改进下一轮策略」
    只能靠感觉。现在加几乎零成本，事后补要重跑历史数据——而历史数据
    已经变了。

    每个值配一句「它提示你该改什么」，因为归因的目的是改进，不是记账。
    """

    UNREACHABLE = "unreachable"
    """找不到可用联系方式。→ 改进联系人数据源或验证流程。"""

    NO_REPLY = "no_reply"
    """序列跑完无回复。→ 改开发信内容或目标筛选；量大时先怀疑筛选。"""

    NEED_NOT_REAL = "need_not_real"
    """接触后发现需求不存在。→ **最该关注的一类。** 说明假设生成
    环节的信号质量有问题，直接反馈给探索策略。"""

    NO_SUPPLY_FOUND = "no_supply_found"
    """需求真实但找不到供应。→ 该品类不该继续探索，或要补供应商网络。"""

    PRICE_TOO_HIGH = "price_too_high"
    """价格谈不下来。→ 看 ``died_at_state``：quoted 阶段是报价能力问题，
    contacted 阶段说明客户一开始就没预算，该改筛选门槛。"""

    LOST_TO_COMPETITOR = "lost_to_competitor"
    """输给竞争对手。→ 尽量记下输在哪（价格/交期/规格/信任）。"""

    CUSTOMER_WENT_SILENT = "customer_went_silent"
    """客户中途失联。→ 检查是否跟进太慢；常与接管 SLA 超时相关。"""

    TIMING_MISMATCH = "timing_mismatch"
    """客户要得太急或采购期已过。→ 可安排未来重启，不是真正的失败。"""

    COMPLIANCE_BLOCKED = "compliance_blocked"
    """合规或认证不可行。→ 该品类应进 Playbook 排除清单，
    避免反复投入。"""

    MARGIN_TOO_LOW = "margin_too_low"
    """算完成本利润不可接受。→ 若同一品类反复出现，说明供应端没优势。"""

    INTERNAL_NO_CAPACITY = "internal_no_capacity"
    """内部没人能跟。→ 这是**团队瓶颈信号**，不是客户问题。
    大量出现时应减少探索、增加人力或收窄市场。"""

    DUPLICATE = "duplicate"
    """与已有机会重复。→ 检查企业消歧和 Ownership Lock。"""


@dataclass(frozen=True)
class HandoffPolicy:
    """接管 SLA 策略（上层注入，无默认业务数字）。"""

    sla_seconds: int
    """超过此秒数无人接受算 breached。"""

    backlog_threshold: int
    """待接管队列深度超过此值判 is_backlogged。"""

    def __post_init__(self) -> None:
        if self.sla_seconds <= 0:
            raise ValidationError("sla_seconds 必须 > 0")
        if self.backlog_threshold <= 0:
            raise ValidationError("backlog_threshold 必须 > 0")


class HandoffTrigger(str, Enum):
    """触发人工接管的条件。

    这些情况下 Agent 继续自动处理的风险大于收益——多数涉及承诺，
    而承诺一旦发出就难以撤回。
    """

    QUANTITY_PROVIDED = "quantity_provided"
    TARGET_PRICE_PROVIDED = "target_price_provided"
    SAMPLE_REQUESTED = "sample_requested"
    QUOTE_REQUESTED = "quote_requested"
    SPECIFICATION_FILE_RECEIVED = "specification_file_received"
    MEETING_REQUESTED = "meeting_requested"
    CUSTOM_PRODUCT = "custom_product"
    CERTIFICATION_QUESTION = "certification_question"
    PAYMENT_OR_CONTRACT_TERMS = "payment_or_contract_terms"
    COMPLAINT = "complaint"
    EXCLUSIVE_DISTRIBUTION = "exclusive_distribution"
    LARGE_ACCOUNT = "large_account"
    HIGH_RISK_PRODUCT = "high_risk_product"
    AGENT_LOW_CONFIDENCE = "agent_low_confidence"
    """Agent 自己判断处理不了。**保留这个触发条件很重要**——
    它给了 Agent 一条体面的退出路径，比硬撑着回复错信息好。"""

    REPEATED_COMPLEX_QUESTIONS = "repeated_complex_questions"


@dataclass
class Opportunity:
    """贸易机会。

    字段分三组：客户与需求、商业判断、执行归属。

    注意 ``estimated_cost`` 与 ``estimated_profit`` 是 ``Money`` 且
    **可能为 None**——寻源前算不出来。不要用 0 代替 None，那会让
    「利润为零」和「还不知道」混为一谈。

    字段：
        opportunity_id, tenant_id, account_id
        account_name, country:  客户名/国家（扁平事实快照，来自上层，带 Provenance）
        need_id:            关联的已验证需求
        state, created_at
        product_category
        quantity, spec_summary, application, destination, required_by
        target_price:       客户目标价
        decision_maker:     决策人描述
        current_supply_solution:  客户现在怎么解决的
        current_supply_problem:   现有方案有什么问题
                            —— **最有价值的字段**，直接说明我们凭什么能赢
        can_source:         是否找到供应（None 表示还没查）
        estimated_cost, estimated_profit
        owner:              负责员工
        next_action:        下一步动作（自由文本，员工可改）
        next_action_due:    下一步截止
        loss_reason, died_at_state, closed_at
    """

    opportunity_id: OpportunityId
    tenant_id: TenantId
    account_id: ProspectAccountId
    need_id: ValidatedNeedId
    product_category: str
    created_at: datetime
    account_name: str
    country: str
    state: OpportunityState = OpportunityState.QUALIFIED
    quantity: int | None = None
    spec_summary: str | None = None
    application: str | None = None
    destination: str | None = None
    required_by: date | None = None
    target_price: Money | None = None
    decision_maker: str | None = None
    current_supply_solution: str | None = None
    current_supply_problem: str | None = None
    can_source: bool | None = None
    estimated_cost: Money | None = None
    estimated_profit: Money | None = None
    owner: EmployeeId | None = None
    next_action: str | None = None
    next_action_due: datetime | None = None
    loss_reason: LossReason | None = None
    died_at_state: OpportunityState | None = None
    closed_by: EmployeeId | None = None
    closed_at: datetime | None = None
    assigned_by: EmployeeId | None = None
    assigned_at: datetime | None = None

    def can_transition_to(self, target: OpportunityState) -> bool:
        """查 ``ALLOWED_TRANSITIONS``（C1：以本模块状态表为准）。"""
        return target in ALLOWED_TRANSITIONS[self.state]

    def mark_lost(
        self, reason: LossReason, at: datetime
    ) -> None:
        """终结机会。

        实现要求：``died_at_state`` 记录**转入 lost 之前**的状态，
        不是 lost 本身。这个字段是归因分析的另一半——同一个
        ``PRICE_TOO_HIGH`` 在不同阶段意味着完全不同的改进方向。

        终态（won/lost）重复终结抛 ``InvalidStateTransition``，防止改写历史。
        """
        if self.state in (OpportunityState.WON, OpportunityState.LOST):
            raise InvalidStateTransition(
                f"机会已处于终态 {self.state.value}，不能重复终结（不得改写历史）"
            )
        self.died_at_state = self.state
        self.loss_reason = reason
        self.state = OpportunityState.LOST
        self.closed_at = at


@dataclass(frozen=True, order=True)
class SortKey:
    """打分排序键：09 文档「字典序、证据主导」的落地。

    evidence_rank（1–7，由 ConfidenceTier 稳定映射）、value_band（由
    ScoringPolicy.value_band_boundaries 划分）、supply_rank（0/1/2：
    False/None/True）。直接 tuple 比较，绝不加权/对数/float。
    """

    evidence_rank: int
    value_band: int
    supply_rank: int


@dataclass(frozen=True)
class ScoreSnapshot:
    """打分输入快照。

    **必须存。** 等有几十条成交数据后要回测权重、验证哪些因子真的
    预测成交——没有快照就只能重跑历史数据，而那时数据已经变了
    （客户状态改了、价格变了、联系人换了）。

    字段：
        tenant_id, snapshot_id, opportunity_id, scored_at, scorer_version
        passed_gates:       通过的硬门槛
        failed_gates:       未通过的硬门槛（有值则未进入打分）
        evidence_tier:      证据档位
        estimated_value:    预计订单额
        supply_available:   供应是否可得
        sort_key:           字典序排序键（evidence_rank / value_band / supply_rank）
        rank_bucket:        高/中/低
        gate_reasons:       每个未通过门槛的具体原因
    """

    tenant_id: TenantId
    snapshot_id: ScoreSnapshotId
    opportunity_id: OpportunityId
    scored_at: datetime
    scorer_version: str
    passed_gates: list[str]
    failed_gates: list[str]
    evidence_tier: ConfidenceTier | None
    estimated_value: Money | None
    supply_available: bool | None
    sort_key: SortKey
    rank_bucket: str
    gate_reasons: dict[str, str]


@dataclass(frozen=True)
class LossRecord:
    """机会终结归因记录（只增；confirmed_by/confirmed_at/recorded_at 必填，人工确认必留痕）。"""

    loss_record_id: LossRecordId
    tenant_id: TenantId
    opportunity_id: OpportunityId
    loss_reason: LossReason
    died_at_state: OpportunityState
    confirmed_by: EmployeeId
    confirmed_at: datetime
    recorded_at: datetime
    detail: str | None = None
    evidence_tier: ConfidenceTier | None = None


class HandoffState(str, Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    REASSIGNED = "reassigned"
    EXPIRED = "expired"
    """超时未接受。要能查出来——这是 SLA 违约，也常是
    ``CUSTOMER_WENT_SILENT`` 的真正原因。"""


def _elapsed_seconds(start: datetime, end: datetime) -> int:
    """两个 datetime 之间的秒数（end - start）。

    naive 与 aware 混用、或结果为负（end 早于 start）均抛 ``ValidationError``——
    时间口径不一致或负等待都是调用方错误，宁可显式报错也不静默截断。
    """
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise ValidationError("naive 与 aware datetime 不可混用：时间必须一致带时区或一致不带")
    elapsed = (end - start).total_seconds()
    if elapsed < 0:
        raise ValidationError("等待时长不能为负：结束时间早于开始时间")
    return int(elapsed)


@dataclass
class HandoffPacket:
    """人工接管包。

    **禁止只发「有个高意向客户，请处理」。** 员工看到通知后应当不用
    再翻五个页面就能开始工作——否则接管会被拖延，而拖延会让客户失联，
    前面所有自动化投入归零。

    字段：
        handoff_id, tenant_id, opportunity_id
        trigger:              触发原因
        assigned_to, manager
        requested_at, accepted_at, accepted_by
        state
        account_name, country
        how_we_found_them:    怎么找到这家公司的
        why_valuable:         为什么判断有价值（要能追到证据）
        customer_verbatim:    **客户原话摘录**，不要模型改写过的版本
        validated_need_summary
        missing_information:  还缺什么
        conversation_summary
        already_sent:         已经发过什么（避免重复或矛盾）
        commitments_made:     已做出的承诺（员工的和 Agent 的）
        suggested_next_step
        evidence_links:       原始证据链接
    """

    handoff_id: HandoffId
    tenant_id: TenantId
    opportunity_id: OpportunityId
    trigger: HandoffTrigger
    requested_at: datetime
    account_name: str
    country: str
    why_valuable: str
    customer_verbatim: str
    state: HandoffState = HandoffState.REQUESTED
    assigned_to: EmployeeId | None = None
    manager: EmployeeId | None = None
    accepted_at: datetime | None = None
    accepted_by: EmployeeId | None = None
    how_we_found_them: str | None = None
    validated_need_summary: str | None = None
    missing_information: list[str] = field(default_factory=list)
    conversation_summary: str | None = None
    already_sent: list[str] = field(default_factory=list)
    commitments_made: list[str] = field(default_factory=list)
    suggested_next_step: str | None = None
    evidence_links: list[str] = field(default_factory=list)

    def wait_seconds(self, now: datetime) -> int | None:
        """等待时长（秒）。**以 ``accepted_at`` 是否存在为准**：非空即按它（无论
        当前 state 是 ACCEPTED 还是 COMPLETED/REASSIGNED，历史 SLA 固定不随 now 增长）；
        为空且非 ACCEPTED 才按传入 now。显式时钟参数便于测试。

        负等待或 naive/aware 混用抛 ``ValidationError``；state 已是 ACCEPTED 却缺
        ``accepted_at`` 属数据不一致，返回 None。
        """
        if self.accepted_at is not None:
            end = self.accepted_at
        elif self.state == HandoffState.ACCEPTED:
            return None
        else:
            end = now
        return _elapsed_seconds(self.requested_at, end)
