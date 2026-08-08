"""会话域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.conversations.models import (
    Conversation,
    Message,
    NextQuestionSuggestion,
    ReplyCategory,
)
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
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
        sent_at: str,
    ) -> MessageId:
        """写入入站消息。

        - 原文先落 artifact_store，这里只存引用——摘要不替代原文
        - 无既有会话则新建
        - 幂等：同一邮件（Message-ID 头）重复投递不重复入库
        """
        ...

    async def record_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        category: ReplyCategory,
        classified_by: str,
    ) -> tuple[str, ...]:
        """落分类结果，返回 ``REPLY_ACTIONS`` 对应的动作序列。

        动作的**执行**在工作流（reply_qualification），本域只返回
        「该做什么」。发布 ``ReplyReceived``（AUTO_REPLY 除外——
        自动回复不算回复）。
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
