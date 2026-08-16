"""会话域实体。（浅域：回复分类枚举写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
)


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
                 会直接终结对话）
        reason:  为什么问这两个（基于缺失字段与完整度级别）

    主题是「问什么」（application / size_range / quantity …），
    措辞由 ``qualification_agent`` 生成。
    """

    topics: list[str]
    reason: str

    def __post_init__(self) -> None:
        """校验 ``len(topics) <= 2``。"""
        raise NotImplementedError
