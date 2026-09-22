"""会话域对外 DTO。

回复分类枚举是本域最重要的公共资产（见本目录 AGENTS.md）：跨层（agent_runtime/
workflows）只能经 schemas 使用分类契约；models 是内部实现。动作映射
``REPLY_ACTIONS`` 仍是域内部数据（确定性动作由域/工作流消费，不随 DTO 外泄）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domains.conversations.models import (
    MAX_REPLY_FIELD_QUOTE_CODEPOINTS,
    ReplyCategory,
    ReplyFieldEvidence,
    ReplySuppressScope,
    ReplyWorkAction,
    ReplyWorkQueue,
    ReplyWorkStatus,
)
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


@dataclass(frozen=True)
class ReplyWorkActionRequest:
    """创建 metadata-only 回复工作项；不接受正文、地址或自由文本。"""

    message_id: MessageId
    outbound_message_id: OutboundMessageId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    action: ReplyWorkAction
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        values = (
            self.message_id,
            self.outbound_message_id,
            self.enrollment_id,
            self.account_id,
            self.contact_point_id,
            self.idempotency_key,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValidationError("回复工作动作关联无效")
        if not isinstance(self.action, ReplyWorkAction):
            raise ValidationError("回复工作动作类型无效")


@dataclass(frozen=True)
class ReplyWorkActionView:
    """Owner queue 只读项；内容通过 message/artifact 授权链另行读取。"""

    action_id: str
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


@dataclass(frozen=True)
class ClassificationCorrectionView:
    """人工纠正视图；原判不覆盖，纠正按时间顺序追加。"""

    corrected_category: ReplyCategory
    corrected_by: str
    corrected_at: datetime


@dataclass(frozen=True)
class InboxMessageView:
    """Inbox 消息审计视图。

    只返回元数据与 artifact 公共引用，不复制邮件主题或正文。分类原判与
    当前有效分类分离，避免人工纠正破坏模型版本评估样本。
    """

    message_id: MessageId
    direction: str
    sent_at: datetime
    raw_artifact_ref: str
    original_category: ReplyCategory | None
    effective_category: ReplyCategory | None
    classified_by: str | None
    classified_at: datetime | None
    corrections: tuple[ClassificationCorrectionView, ...]
    required_actions: tuple[str, ...]
    outbound_message_id: OutboundMessageId | None = None


@dataclass(frozen=True)
class ConversationInboxItem:
    """Smart Inbox 列表项；动作是规则要求，不表示动作已经执行成功。"""

    conversation_id: ConversationId
    account_id: ProspectAccountId
    channel: str
    last_activity_at: datetime
    latest_message_id: MessageId | None
    latest_message_at: datetime | None
    raw_artifact_ref: str | None
    original_category: ReplyCategory | None
    effective_category: ReplyCategory | None
    classified_by: str | None
    classified_at: datetime | None
    correction_count: int
    required_actions: tuple[str, ...]


@dataclass(frozen=True)
class ConversationInboxDetail:
    """Smart Inbox 会话详情；消息证据逐条保留原件引用。"""

    conversation_id: ConversationId
    account_id: ProspectAccountId
    channel: str
    created_at: datetime
    last_inbound_at: datetime | None
    last_outbound_at: datetime | None
    messages: tuple[InboxMessageView, ...]


__all__ = (
    "MAX_REPLY_FIELD_QUOTE_CODEPOINTS",
    "ClassificationCorrectionView",
    "ConversationInboxDetail",
    "ConversationInboxItem",
    "InboxMessageView",
    "ReplyCategory",
    "ReplyFieldEvidence",
    "ReplySuppressScope",
    "ReplyWorkAction",
    "ReplyWorkActionRequest",
    "ReplyWorkActionView",
    "ReplyWorkQueue",
    "ReplyWorkStatus",
)


class AccountReplyStatus(BaseModel):
    """当前账户入站快照；unknown 禁止解释为没有回复。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    account_id: ProspectAccountId
    state: Literal["unknown", "replied", "no_reply"]
    replied_at: datetime | None = None


class ReplyNextQuestionsView(BaseModel):
    """受权只读建议；真实引用和队列状态不被英文措辞替代。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    conversation_id: str
    source_message_id: str
    need_id: str | None
    state: Literal["suggested", "need_unavailable", "no_missing_fields"]
    completeness: int | None = Field(ge=0, le=5)
    topics: tuple[str, ...] = Field(max_length=2)
    suggestions: tuple[str, ...] = Field(max_length=2)


class InboxEvidenceRef(BaseModel):
    """仅服务端Message授权后使用的不可变原件关联。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: str
    message_id: str
    conversation_id: str
    account_id: str
    artifact_id: str
