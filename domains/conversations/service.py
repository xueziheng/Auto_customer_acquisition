"""会话域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from domains.conversations.models import (
    Conversation,
    Message,
    NextQuestionSuggestion,
    ReplyCategory,
)
from domains.conversations.repository import (
    ConversationsUnitOfWork as _ConversationsUnitOfWork,
)

#: 域公共 API 复出口（outreach/sending_identity 同款先例）：apps 侧
#: 组合只能经 service 引用事务边界类型，不得直接 import repository。
ConversationsUnitOfWork = _ConversationsUnitOfWork

from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class ConversationService(Protocol):
    """会话服务。"""

    async def ingest_inbound(
        self,
        tenant_id: TenantId,
        conversation_id: ConversationId | None,
        account_id: ProspectAccountId,
        raw_artifact_ref: str,
        external_message_id: str,
        sent_at: datetime,
        *,
        outbound_message_id: OutboundMessageId | None = None,
    ) -> MessageId:
        """写入入站消息并发布 ``InboundMessageStored``（metadata-only）。

        - 原文先落 artifact_store（上游契约），这里只存引用——摘要不替代原文
        - ``external_message_id`` 是入站邮件 Message-ID 头值（精确匹配，
          含尖括号形态，禁止 normalize）；必填非空——Phase 1 服务契约的
          fail-closed 接入限制，缺失该头的合法邮件会被拒绝需人工处理
        - 幂等/冲突：同 (tenant, external_message_id) 且语义完全一致
          （conversation/account、raw_artifact_ref、sent_at、
          outbound_message_id）→ 返回既有 MessageId 且不再发事件；任一
          不一致 → ``ReingestConflictError``（固定安全摘要）
        - ``conversation_id`` 非 None：必须存在且 tenant/account/channel=email
          匹配，否则 fail-closed；None 才按 (tenant, account, channel)
          并发安全 get-or-create
        - ``last_inbound_at`` = max(既有值, sent_at)——迟到旧消息不回退
        """
        ...

    async def record_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        category: ReplyCategory,
        classified_by: str,
        *,
        outbound_message_id: OutboundMessageId | None = None,
    ) -> tuple[str, ...]:
        """落分类结果，返回 ``REPLY_ACTIONS`` 对应的动作序列。

        动作的**执行**在工作流（reply_qualification），本域只返回
        「该做什么」。发布 ``ReplyReceived``（AUTO_REPLY 除外——
        自动回复不算回复）。``outbound_message_id`` 是被回复出站消息的
        RFC Message-ID（In-Reply-To/References 关联）；无关联传 None，
        订阅方 fail-closed。
        """
        ...

    async def correct_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        corrected_category: ReplyCategory,
        corrected_by: str,
    ) -> None:
        """人工纠正分类。

        原分类保留（``classified_by`` 不变，另记纠正人）——
        纠正样本是评估集的直接来源，覆盖掉就丢了。
        """
        ...

    async def suggest_next_questions(
        self,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        missing_fields: list[str],
        completeness: int,
    ) -> NextQuestionSuggestion:
        """下一问建议。输入是需求完整度的缺失字段（由上层从 demand
        域查得传入），输出最多两个主题。"""
        ...

    async def get_conversation(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> Conversation: ...

    async def list_messages(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> list[Message]: ...
