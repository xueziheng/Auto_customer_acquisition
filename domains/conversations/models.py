"""会话域实体。（浅域：回复分类枚举写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    ConversationId,
    EnrollmentId,
    IdempotencyKey,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
)

MAX_REPLY_FIELD_QUOTE_CODEPOINTS = 500


class ReplyCategory(str, Enum):
    """回复分类 —— 设计稿第十二节的 14 类，每类对应确定的系统动作。

    分类由 ``agent_runtime/qualification_agent`` 做，**动作映射是
    确定性代码**（``REPLY_ACTIONS``）——模型判断类别，代码决定做什么。
    分类错误的代价按类别不对称：把退订误判成拒绝还会再发邮件，
    那是投诉；把兴趣误判成拒绝只是少跟一单。所以退订/投诉类的判定
    要偏保守（宁可误判为退订）。
    """

    CLEAR_INTEREST = "clear_interest"
    """明确兴趣。→ 停序列，进入需求确认。"""

    WILLING_TO_CONTINUE = "willing_to_continue"
    """愿意继续聊。→ 停序列，进入需求确认。"""

    REQUESTS_MATERIALS = "requests_materials"
    """要求发资料。→ 停序列 + 人工接管（发什么资料是承诺边缘）。"""

    REQUESTS_QUOTE = "requests_quote"
    """要求报价。→ 停序列 + 人工接管（价格永不自动承诺）。"""

    REQUESTS_SAMPLE = "requests_sample"
    """要求样品。→ 停序列 + 人工接管。"""

    PROVIDES_SPECIFICATION = "provides_specification"
    """提供规格。→ 停序列 + 提取字段进已验证需求 + 人工接管。
    这是最高价值的回复类别。"""

    NO_CURRENT_NEED = "no_current_need"
    """无当前需求。→ 停序列，标记未来可重启（timing_mismatch，
    不是拒绝）。"""

    FUTURE_NEED_POSSIBLE = "future_need_possible"
    """以后可能有需求。→ 停序列 + 建定期跟进任务。"""

    REFERS_OTHER_CONTACT = "refers_other_contact"
    """介绍其他联系人。→ 停本序列；新联系人走验证与法律依据流程，
    **不直接入组**——被介绍不等于被验证。"""

    REJECTION = "rejection"
    """拒绝。→ 停序列。不进抑制名单（拒绝≠退订，半年后可以再碰）。"""

    UNSUBSCRIBE = "unsubscribe"
    """退订。→ 停序列 + 抑制名单（判断范围：个人还是整个公司）。
    误判代价最高的类别，判定偏保守。"""

    BOUNCE = "bounce"
    """退信。→ 按硬/软分流处理（outreach 域）。"""

    AUTO_REPLY = "auto_reply"
    """自动回复（out-of-office 等）。→ **不算回复，序列继续。**
    唯一不停序列的类别——当回复处理会错停序列并污染回复率统计。"""

    COMPLAINT = "complaint"
    """投诉。→ 停序列 + 抑制 + 计入发件身份信誉 + 通知负责人。"""


class ReplySuppressScope(str, Enum):
    """客户明确退订的业务范围；只对 ``unsubscribe`` 分类有值。"""

    CONTACT = "contact"
    ACCOUNT = "account"


class ReplyWorkAction(str, Enum):
    """由回复产生、需要内部员工继续处理的耐久业务动作。"""

    START_QUALIFICATION = "start_qualification"
    MARK_FUTURE_RESTART = "mark_future_restart"
    CREATE_FOLLOW_UP = "create_follow_up"
    INTAKE_NEW_CONTACT = "intake_new_contact"


class ReplyWorkQueue(str, Enum):
    """Owner 可消费的明确队列；referral 只能进入核验准入队列。"""

    NEED_QUALIFICATION = "need_qualification"
    FUTURE_RESTART_REVIEW = "future_restart_review"
    FOLLOW_UP = "follow_up"
    VERIFIED_CONTACT_INTAKE_REVIEW = "verified_contact_intake_review"


class ReplyWorkStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


REPLY_WORK_QUEUES: dict[ReplyWorkAction, ReplyWorkQueue] = {
    ReplyWorkAction.START_QUALIFICATION: ReplyWorkQueue.NEED_QUALIFICATION,
    ReplyWorkAction.MARK_FUTURE_RESTART: ReplyWorkQueue.FUTURE_RESTART_REVIEW,
    ReplyWorkAction.CREATE_FOLLOW_UP: ReplyWorkQueue.FOLLOW_UP,
    ReplyWorkAction.INTAKE_NEW_CONTACT: (
        ReplyWorkQueue.VERIFIED_CONTACT_INTAKE_REVIEW
    ),
}


REPLY_ACTIONS: dict[ReplyCategory, tuple[str, ...]] = {
    ReplyCategory.CLEAR_INTEREST: ("stop_sequence", "start_qualification"),
    ReplyCategory.WILLING_TO_CONTINUE: ("stop_sequence", "start_qualification"),
    ReplyCategory.REQUESTS_MATERIALS: ("stop_sequence", "handoff"),
    ReplyCategory.REQUESTS_QUOTE: ("stop_sequence", "handoff"),
    ReplyCategory.REQUESTS_SAMPLE: ("stop_sequence", "handoff"),
    ReplyCategory.PROVIDES_SPECIFICATION: (
        "stop_sequence",
        "extract_need_fields",
        "handoff",
    ),
    ReplyCategory.NO_CURRENT_NEED: ("stop_sequence", "mark_future_restart"),
    ReplyCategory.FUTURE_NEED_POSSIBLE: ("stop_sequence", "create_follow_up"),
    ReplyCategory.REFERS_OTHER_CONTACT: ("stop_sequence", "intake_new_contact"),
    ReplyCategory.REJECTION: ("stop_sequence",),
    ReplyCategory.UNSUBSCRIBE: ("stop_sequence", "suppress"),
    ReplyCategory.BOUNCE: ("route_bounce",),
    ReplyCategory.AUTO_REPLY: (),
    ReplyCategory.COMPLAINT: ("stop_sequence", "suppress", "record_complaint"),
}
"""类别 → 动作映射。**确定性代码，不是模型输出。**
模型只判断类别；做什么由这张表决定，改动作等于改业务规则，走 review。
"""


@dataclass(frozen=True)
class ReplyFieldEvidence:
    """模型从单条客户回复提取的候选事实与逐字证据。

    本业务事实随分类记录 tenant-bound 持久化，供后续动作按 message_id
    重读；不得复制到 workflow context、事件或日志。quote 最多 500 个
    Unicode code point；超长必须拒绝，不能截断后伪装成原候选。
    """

    field: str
    value: str
    quote: str

    def __post_init__(self) -> None:
        for value in (self.field, self.value, self.quote):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError("回复字段证据无效")
        if len(self.quote) > MAX_REPLY_FIELD_QUOTE_CODEPOINTS:
            raise ValidationError("回复字段逐字证据超长")


@dataclass
class MessageClassification:
    """一次回复分类留痕。

    每 (tenant_id, message_id) 至多一条（DB 唯一约束在并发下强制单行）：
    跨 model_version 重评由服务层显式拒绝（未来由显式 reclassify API 承担）；
    ``classified_by`` 即分类者标识（模型版本/人工标识），用于按版本评估。
    """

    tenant_id: TenantId
    message_id: MessageId
    category: ReplyCategory
    classified_by: str
    classified_at: datetime
    candidate_fields: tuple[ReplyFieldEvidence, ...] = ()
    suppress_scope: ReplySuppressScope | None = None

    def __post_init__(self) -> None:
        if self.category is ReplyCategory.UNSUBSCRIBE:
            if not isinstance(self.suppress_scope, ReplySuppressScope):
                raise ValidationError("退订分类必须带抑制范围")
        elif self.suppress_scope is not None:
            raise ValidationError("非退订分类不得携带抑制范围")


@dataclass(frozen=True)
class ReplyWorkRecord:
    """metadata-only 回复工作事实；客户正文与地址不得进入本记录。"""

    action_id: str
    tenant_id: TenantId
    message_id: MessageId
    outbound_message_id: OutboundMessageId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    action: ReplyWorkAction
    owner_queue: ReplyWorkQueue
    status: ReplyWorkStatus
    idempotency_key: IdempotencyKey
    created_at: datetime

    def __post_init__(self) -> None:
        if self.owner_queue is not REPLY_WORK_QUEUES.get(self.action):
            raise ValidationError("回复工作动作与 owner queue 不匹配")
        if self.status is not ReplyWorkStatus.PENDING:
            raise ValidationError("新回复工作动作状态无效")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or self.created_at.utcoffset() != UTC.utcoffset(self.created_at)
        ):
            raise ValidationError("回复工作动作时间必须为 UTC")


@dataclass
class ClassificationCorrection:
    """一次人工纠正分类留痕（append-only，未来评估集摄取的耐久来源）。

    - ``correction_id``：域内生成（new_id("ccr")），PK 一部分；不落 shared 契约
    - ``corrected_category``/``corrected_by``/``corrected_at``：纠正语义；
      UNIQUE(tenant, message, corrected_by, corrected_category) 是 DB 幂等键，
      同键并发只一行；不同纠正即使同一 corrected_at 也保留多行
    - 原分类行（MessageClassification）永不修改（覆盖纠正样本会丢）
    - 长度 fail-closed：correction_id/tenant_id ≤ 32、message_id/corrected_by
      ≤ 100，与 DB 列上限对齐——超长在模型层即拒（先于 DB 报错/截断）
    """

    correction_id: str
    tenant_id: TenantId
    message_id: MessageId
    corrected_category: ReplyCategory
    corrected_by: str
    corrected_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise ValidationError("纠正租户无效")
        if len(self.tenant_id) > 32:
            raise ValidationError("纠正租户超长")
        if not isinstance(self.message_id, str) or not self.message_id.strip():
            raise ValidationError("纠正消息无效")
        if len(self.message_id) > 100:
            raise ValidationError("纠正消息超长")
        if (
            not isinstance(self.correction_id, str)
            or not self.correction_id.strip()
        ):
            raise ValidationError("纠正记录 id 无效")
        if len(self.correction_id) > 32:
            raise ValidationError("纠正记录 id 超长")
        if not isinstance(self.corrected_by, str) or not self.corrected_by.strip():
            raise ValidationError("纠正人无效")
        if len(self.corrected_by) > 100:
            raise ValidationError("纠正人超长")
        if not isinstance(self.corrected_category, ReplyCategory):
            raise ValidationError("纠正类别无效")
        if (
            not isinstance(self.corrected_at, datetime)
            or self.corrected_at.tzinfo is None
            or self.corrected_at.utcoffset() != UTC.utcoffset(self.corrected_at)
        ):
            raise ValidationError("纠正时间必须为 UTC")


class MessageDirection(str, Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


@dataclass
class Conversation:
    """会话。跨渠道（Phase 1 只有邮件）按客户聚合。"""

    conversation_id: ConversationId
    tenant_id: TenantId
    account_id: ProspectAccountId
    channel: str
    created_at: datetime
    subject: str | None = None
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None


@dataclass
class Message:
    """消息。``raw_artifact_ref`` 指向原文——**模型摘要不替代原文**
    （数据平面第一层规则）。

    字段：
        message_id, tenant_id, conversation_id
        direction, sent_at
        language
        body_preview:      截断预览（列表页用）
        raw_artifact_ref:  原文在 artifact_store 的引用
        classification:    分类结果（入站消息）
        classified_by:     分类的模型版本（分类质量按版本评估）
        classification_confirmed_by: 人工纠正记录
    """

    message_id: MessageId
    tenant_id: TenantId
    conversation_id: ConversationId
    direction: MessageDirection
    sent_at: datetime
    raw_artifact_ref: str
    external_message_id: str | None = None
    outbound_message_id: OutboundMessageId | None = None
    language: str | None = None
    body_preview: str | None = None
    classification: ReplyCategory | None = None
    classified_by: str | None = None
    classification_confirmed_by: str | None = None


@dataclass(frozen=True)
class NextQuestionSuggestion:
    """下一问建议。

    字段：
        topics:  该问的主题，**最多两个**（十几个问题的审讯式追问
                 会直接终结对话）；允许为空（无缺失字段即无追问）
        reason:  为什么问这两个（基于缺失字段与完整度级别）

    主题是「问什么」（application / size_range / quantity …），
    措辞由 ``qualification_agent`` 生成。校验契约见 ``__post_init__``。
    """

    topics: list[str]
    reason: str

    def __post_init__(self) -> None:
        """校验与防御性拷贝（契约见 plan 2026-08-16-next-question-selection）。

        - ``topics`` 必须 list（拒绝 str/tuple）、0..2 个；每项必须 str、
          strip 后非空、且 ``item == item.strip()``（拒绝元素前后空白）；
          必须无重复；无长度上限（topic 无 DB 长度定义，不自造 max）
        - ``reason`` 必须 str、strip 后非空；不拒绝自身前后空白（内部
          固定格式生成，最小语义只要求非空）
        - 校验全部通过后 ``object.__setattr__`` 换新 list：只切断「构造后
          调用方继续修改传入源列表」的别名路径；``topics`` 属性自身仍是
          可变 list（frozen 只挡属性重绑），不宣称深度不可变
        """
        if type(self.topics) is not list:
            raise ValidationError("建议主题必须是列表")
        if len(self.topics) > 2:
            raise ValidationError("建议主题最多两个")
        for topic in self.topics:
            if (
                not isinstance(topic, str)
                or not topic.strip()
                or topic != topic.strip()
            ):
                raise ValidationError("建议主题无效")
        if len(set(self.topics)) != len(self.topics):
            raise ValidationError("建议主题重复")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValidationError("建议理由无效")
        object.__setattr__(self, "topics", list(self.topics))
