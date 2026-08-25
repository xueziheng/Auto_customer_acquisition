"""会话域对外 DTO。

回复分类枚举是本域最重要的公共资产（见本目录 AGENTS.md）：跨层（agent_runtime/
workflows）只能经 schemas 使用分类契约；models 是内部实现。动作映射
``REPLY_ACTIONS`` 仍是域内部数据（确定性动作由域/工作流消费，不随 DTO 外泄）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domains.conversations.models import ReplyCategory, ReplyFieldEvidence
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    ProspectAccountId,
)


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
    "ClassificationCorrectionView",
    "ConversationInboxDetail",
    "ConversationInboxItem",
    "InboxMessageView",
    "ReplyCategory",
    "ReplyFieldEvidence",
)
