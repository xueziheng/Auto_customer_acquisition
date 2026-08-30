"""领域事件目录 —— **事件即契约**。

改事件字段等于改公共 API，会影响所有订阅方，**必须留 ADR**。
加新事件不影响现有订阅方，不需要 ADR。

命名用**过去式**：事件描述已经发生的事实，不是命令。
``NeedValidated`` 对，``ValidateNeed`` 错——后者是命令，命令应该走
服务接口调用。

每个事件都带 ``tenant_id``（硬边界 8）与 ``occurred_at``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.errors import ValidationError
from shared.schemas.evidence import ConfidenceTier, EvidenceLevel
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    CampaignId,
    ContactPointId,
    ConversationId,
    CountryPolicyVersionId,
    DemandSignalId,
    EmployeeId,
    HandoffId,
    MessageId,
    NeedHypothesisId,
    OpportunityId,
    OutboundMessageId,
    ProspectAccountId,
    QuoteId,
    RunId,
    SendingIdentityId,
    SourcingCaseId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)


@dataclass(frozen=True)
class DomainEvent:
    """所有领域事件的基类。

    子类一律用 ``@dataclass(frozen=True)``——事件是已发生的事实，
    不可变。
    """

    tenant_id: TenantId
    occurred_at: datetime
    run_id: RunId | None = None
    """触发这个事件的 Agent Run。人工操作触发时为 ``None``。"""


# --- 需求发现 -----------------------------------------------------------


@dataclass(frozen=True)
class DemandSignalCaptured(DomainEvent):
    """捕获到一条需求信号。

    订阅方：``domains/demand``（尝试生成假设）。
    完整来源 URL 只保存在 tenant-bound demand provenance；订阅方用 signal_id 回查。
    """

    signal_id: DemandSignalId = None  # type: ignore[assignment]
    entity_name: str = ""
    signal_type: str = ""


@dataclass(frozen=True)
class NeedHypothesisCreated(DomainEvent):
    """生成了需求假设。

    订阅方：``domains/prospecting``（验证企业与联系人）。
    """

    hypothesis_id: NeedHypothesisId = None  # type: ignore[assignment]
    account_id: ProspectAccountId | None = None
    category: str = ""
    confidence_tier: ConfidenceTier | None = None


@dataclass(frozen=True)
class NeedHypothesisRejected(DomainEvent):
    """需求假设被证伪或判定不值得跟进。

    ``reason`` 用 ``domains/opportunities`` 的 Loss Reason 枚举，
    进入反馈闭环——哪类假设最容易被证伪，直接改进下一轮探索策略。
    """

    hypothesis_id: NeedHypothesisId = None  # type: ignore[assignment]
    reason: str = ""


@dataclass(frozen=True)
class NeedValidated(DomainEvent):
    """需求已验证 —— **系统里最重要的事件**。

    只有客户回复、提交表单、发送规格表或经员工确认后才能发布。
    Agent 的推断再有道理也不能触发这个事件。

    订阅方：``domains/opportunities``（评估是否创建机会）、
    ``domains/demand``（尝试归入需求簇）。
    """

    need_id: ValidatedNeedId = None  # type: ignore[assignment]
    account_id: ProspectAccountId | None = None
    category: str = ""
    evidence_level: EvidenceLevel | None = None
    completeness: int = 0
    """需求完整度 0–5，见 ``docs/architecture/01-domain-model.md``。"""


@dataclass(frozen=True)
class NeedBecameSourcingReady(DomainEvent):
    """已验证需求首次从未达门槛变为可寻源。"""

    need_id: ValidatedNeedId = None  # type: ignore[assignment]
    completeness: int = 0


@dataclass(frozen=True)
class NeedClusterFormed(DomainEvent):
    """多条需求归为一簇。

    Phase 1 只记录；Phase 2 用它驱动寻源优先级——八个客户都要同一种
    产品时，一次寻源服务多个买家，还有谈价筹码。
    """

    cluster_id: str = ""
    category: str = ""
    member_count: int = 0


# --- 客户开发与触达 -----------------------------------------------------


@dataclass(frozen=True)
class ProspectAccountQualified(DomainEvent):
    """潜在企业通过硬门槛，可进入触达队列。"""

    account_id: ProspectAccountId = None  # type: ignore[assignment]
    assigned_to: EmployeeId | None = None


@dataclass(frozen=True)
class ContactPointVerified(DomainEvent):
    """联系方式可达性验证通过（硬边界 6 的前置条件）。

    未发布此事件的联系方式不得进入序列。
    """

    contact_point_id: ContactPointId = None  # type: ignore[assignment]
    verification_result: str = ""


@dataclass(frozen=True)
class InboundMessageStored(DomainEvent):
    """入站消息已持久化（metadata-only，最小披露）。

    由 ``ConversationService.ingest_inbound`` 在消息行落库的同事务发布。
    与 ``ReplyReceived`` 区分：后者是分类落库**之后**的结果事件；本事件是
    触发 reply_qualification 的前置信号（「入站原文已存 artifact + Message
    已持久化」），订阅方按 ``outbound_message_id`` 精确匹配
    ``attempt.deterministic_message_id`` 解析被回复出站消息。

    字段最小披露（按消费者需要逐一保留）：
    - ``message_id``：入站消息 id（run 的 subject_ref + 幂等键）
    - ``outbound_message_id``：被回复出站消息的 RFC Message-ID（In-Reply-To/
      References 关联）；无关联为 None，消费者必须 fail-closed

    不含正文/主题/地址/raw_artifact_ref/对象键（artifact_store 边界 4）。
    """

    message_id: MessageId = None  # type: ignore[assignment]
    outbound_message_id: OutboundMessageId | None = None


@dataclass(frozen=True)
class MessageSent(DomainEvent):
    """已向客户发出一条消息。

    订阅方：``domains/sending_identity``（累计发量与配额）。
    """

    message_id: MessageId = None  # type: ignore[assignment]
    campaign_id: CampaignId | None = None
    sending_identity_id: SendingIdentityId | None = None


@dataclass(frozen=True)
class CampaignStateChanged(DomainEvent):
    """Campaign 持久状态或版本已改变，供等待中的账户发现流程精确收束。"""

    campaign_id: CampaignId = None  # type: ignore[assignment]
    campaign_version: int = 0
    state: str = ""


@dataclass(frozen=True)
class ReplyReceived(DomainEvent):
    """收到客户回复。

    订阅方：``domains/outreach``（停止序列）、
    ``outreach_campaign``（唤醒等待步骤）。**不是** reply_qualification 的
    前置触发——它由 ``ConversationService.record_classification`` 在分类落库
    **之后**发布（结果事件），分类动作由 InboundMessageStored 触发的 reply
    workflow 完成。

    ``message_id`` 是**入站**回复消息 id；``outbound_message_id`` 是被回复的
    **出站**消息 RFC Message-ID（= outreach attempt 的 deterministic_message_id，
    投递关联键）——两者命名空间不同，禁止混用；无出站关联时为 None，
    消费者必须 fail-closed。
    """

    message_id: MessageId = None  # type: ignore[assignment]
    conversation_id: ConversationId | None = None
    reply_category: str = ""
    outbound_message_id: OutboundMessageId | None = None


@dataclass(frozen=True)
class SuppressionAdded(DomainEvent):
    """联系人或企业进入抑制名单。

    退订、投诉、硬退信、人工拉黑都会触发。**全局生效**，
    跨 Campaign、跨发件身份、跨员工。
    """

    scope: str = ""
    """``contact`` 或 ``account``——客户说"不要再联系我们公司"时
    必须抑制整个企业，不只是发言那个人。"""
    target_id: str = ""
    reason: str = ""


# --- 发件身份 -----------------------------------------------------------


@dataclass(frozen=True)
class SendingIdentityActivated(DomainEvent):
    """发件身份完成预热进入 active。订阅方：``domains/outreach``。"""

    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]


@dataclass(frozen=True)
class AuthenticationCheckRequested(DomainEvent):
    """已创建发件身份 DNS 认证检查请求。"""

    request_id: AuthenticationCheckRequestId = None  # type: ignore[assignment]
    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]


@dataclass(frozen=True)
class SendingIdentityThrottled(DomainEvent):
    """发件身份被降额或停发（自动熔断，不等人工）。

    订阅方：``domains/outreach``（调整发送计划）、
    ``notification_gateway``（通知负责人）。
    """

    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    new_state: str = ""
    trigger_metric: str = ""
    metric_value: str = ""


@dataclass(frozen=True)
class SendingIdentitySuspended(DomainEvent):
    """发件身份被自动停用（超 suspend 阈值 / 垃圾陷阱 / 黑名单）。

    订阅方：``domains/outreach``（立刻停用该身份下所有序列发送）、
    ``notification_gateway``（告警，需人工排查后才能恢复）。

    熔断必须先落状态再发事件——事件投递失败时状态也要已生效，
    否则告警发了但发送还在继续。
    """

    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    reason: str = ""


@dataclass(frozen=True)
class ReputationThresholdBreached(DomainEvent):
    """信誉指标越过阈值（含 watch 级预警，不只熔断级）。

    订阅方：``notification_gateway``。watch 级预警的价值在于给人
    留出调整时间——等到熔断才知道就晚了。
    """

    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    metric: str = ""
    value: str = ""
    threshold: str = ""
    severity: str = ""


# --- 投递事件（由 connectors 经 tool_gateway 写入后发布） ----------------


@dataclass(frozen=True)
class MessageDelivered(DomainEvent):
    """消息确认送达。订阅方：``domains/sending_identity``（信誉计数）。"""

    message_attempt_id: str = ""
    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    dedup_key: str = ""


@dataclass(frozen=True)
class MessageBounced(DomainEvent):
    """消息退信。

    ``is_hard`` 区分硬退信（地址不存在，计入信誉惩罚并触发抑制）与
    软退信（临时性，只计数）。混为一谈会让正常的临时退信拖垮身份状态。

    订阅方：``domains/sending_identity``（信誉）、``domains/outreach``
    （硬退信 → 抑制名单，软退信连续 3 次按硬退信处理）。
    """

    message_attempt_id: str = ""
    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    is_hard: bool = False
    dedup_key: str = ""


@dataclass(frozen=True)
class ComplaintReceived(DomainEvent):
    """收到垃圾邮件投诉。

    订阅方：``domains/sending_identity``（信誉，投诉杀伤力比退信大
    一个量级）、``domains/outreach``（立即抑制该联系人）。
    """

    message_attempt_id: str = ""
    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    dedup_key: str = ""


@dataclass(frozen=True)
class UnsubscribeReceived(DomainEvent):
    """收到退订请求。

    订阅方：``domains/outreach``（抑制联系人，必要时抑制整个企业）、
    ``domains/sending_identity``（计数）。
    """

    contact_point_id: ContactPointId = None  # type: ignore[assignment]
    sending_identity_id: SendingIdentityId = None  # type: ignore[assignment]
    dedup_key: str = ""


# --- 机会 ---------------------------------------------------------------


@dataclass(frozen=True)
class OpportunityQualified(DomainEvent):
    """机会通过硬门槛成为合格贸易机会。

    这是商业北极星指标的计数事件。
    """

    opportunity_id: OpportunityId = None  # type: ignore[assignment]
    rank_bucket: str = ""


@dataclass(frozen=True)
class OpportunityLost(DomainEvent):
    """机会终结。

    ``loss_reason`` 必填，用 ``domains/opportunities`` 的枚举。
    没有结构化归因，"根据结果改进策略"只能靠感觉。
    """

    opportunity_id: OpportunityId = None  # type: ignore[assignment]
    loss_reason: str = ""
    died_at_state: str = ""


@dataclass(frozen=True, kw_only=True)
class OpportunityWon(DomainEvent):
    """机会成交（**人工确认终态**）。

    由 ``domains/opportunities`` 的 ``mark_won`` 发布，必须带 ``closed_by``
    （确认人）。Agent 不得自动标记成交——这是人工确认动作
    （AGENTS.md §六）。``closed_by`` 为空即抛错，防事件被伪造为空。
    """

    opportunity_id: OpportunityId
    closed_by: EmployeeId | None = None

    def __post_init__(self) -> None:
        if self.closed_by is None:
            raise ValidationError("OpportunityWon 必须有 closed_by（人工确认不可伪造为空）")


@dataclass(frozen=True)
class HandoffRequested(DomainEvent):
    """请求人工接管。

    订阅方：``notification_gateway``（通知负责人与经理）。
    接管 SLA 从这个事件的时间开始计。
    """

    handoff_id: HandoffId = None  # type: ignore[assignment]
    opportunity_id: OpportunityId | None = None
    assigned_to: EmployeeId | None = None
    trigger: str = ""


@dataclass(frozen=True)
class HandoffAccepted(DomainEvent):
    """员工接受接管。用于计算等待时长。"""

    handoff_id: HandoffId = None  # type: ignore[assignment]
    accepted_by: EmployeeId | None = None


@dataclass(frozen=True)
class HandoffQueueBacklogged(DomainEvent):
    """待接管队列积压超阈值。

    Phase 1 只发通知。Phase 2 的反压挂载点：接入配额后，据此自动
    降低探索类任务的预算——队列里积压 40 个高意向机会时，继续花钱
    找新客户是在毁灭价值。
    """

    queue_depth: int = 0
    oldest_wait_seconds: int = 0


# --- 寻源与报价 ---------------------------------------------------------


@dataclass(frozen=True)
class SourcingCaseOpened(DomainEvent):
    """启动目录外寻源。"""

    case_id: SourcingCaseId = None  # type: ignore[assignment]
    need_id: ValidatedNeedId | None = None


@dataclass(frozen=True)
class SourcingCaseCompleted(DomainEvent):
    """寻源完成，产出合格候选（最多三个）。"""

    case_id: SourcingCaseId = None  # type: ignore[assignment]
    candidate_count: int = 0


@dataclass(frozen=True)
class SourcingCandidatesVerified(DomainEvent):
    """供应商候选已经核验，可由产品域开始幂等生成候选产品卡。"""

    case_id: SourcingCaseId = None  # type: ignore[assignment]
    candidate_ids: tuple[SupplierCandidateId, ...] = ()
    case_version: int = 0
    candidate_set_hash: str = ""

    def __post_init__(self) -> None:
        if (
            not self.candidate_ids
            or tuple(sorted(self.candidate_ids, key=str)) != self.candidate_ids
            or len(set(self.candidate_ids)) != len(self.candidate_ids)
            or isinstance(self.case_version, bool)
            or self.case_version < 1
            or len(self.candidate_set_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.candidate_set_hash
            )
        ):
            raise ValidationError("SourcingCandidatesVerified generation 无效")


@dataclass(frozen=True)
class SourcingCandidatesReady(DomainEvent):
    """寻源案例的候选供应选项已完成核验。"""

    case_id: SourcingCaseId = None  # type: ignore[assignment]
    option_ids: tuple[SourcingSupplyOptionId, ...] = ()
    candidate_ids: tuple[SupplierCandidateId, ...] = ()


@dataclass(frozen=True)
class SourcingCaseHandedToCosting(DomainEvent):
    """寻源案例已交给成本核算。"""

    case_id: SourcingCaseId = None  # type: ignore[assignment]
    need_id: ValidatedNeedId = None  # type: ignore[assignment]
    opportunity_id: OpportunityId = None  # type: ignore[assignment]
    review_id: SourcingReviewId = None  # type: ignore[assignment]


@dataclass(frozen=True)
class QuoteApproved(DomainEvent):
    """报价通过审批，可发送给客户。

    **未发布此事件的报价不得对外发送。**
    """

    quote_id: QuoteId = None  # type: ignore[assignment]
    approved_by: EmployeeId | None = None


# --- 协作 ---------------------------------------------------------------


@dataclass(frozen=True)
class CommitmentCreated(DomainEvent):
    """记录一条承诺（员工的或客户的）。"""

    commitment_id: str = ""
    commitment_type: str = ""
    due_at: datetime | None = None


@dataclass(frozen=True)
class CommitmentOverdue(DomainEvent):
    """承诺逾期。订阅方：``notification_gateway``（升级给经理）。"""

    commitment_id: str = ""
    overdue_seconds: int = 0


@dataclass(frozen=True)
class DirectiveActivated(DomainEvent):
    """老板指令生效。

    订阅方：所有受指令影响的域（探索配比、触达边界、分配规则）。
    """

    directive_id: str = ""
    version: int = 0


@dataclass(frozen=True)
class ApprovalDecided(DomainEvent):
    """审批有结果。

    订阅方：``agent_runtime``（应用或丢弃对应 Change Set）。
    """

    approval_id: str = ""
    decision: str = ""
    decided_by: EmployeeId | None = None


@dataclass(frozen=True, kw_only=True)
class CountryPolicyVersionProposed(DomainEvent):
    """已持久化一个待独立审批的国家政策候选版本。

    事件只携带工作流关联所需的 metadata，不含政策正文或来源内容。
    """

    country_policy_version_id: CountryPolicyVersionId
    country_key: str
    content_hash: str
    proposed_by: EmployeeId
