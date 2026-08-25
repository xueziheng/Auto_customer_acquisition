"""会话域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from domains.conversations.models import (
    Conversation,
    Message,
    MessageClassification,
    NextQuestionSuggestion,
    ReplyCategory,
    ReplyFieldEvidence,
    ReplySuppressScope,
)
from domains.conversations.repository import (
    ConversationsUnitOfWork as _ConversationsUnitOfWork,
)
from domains.conversations.schemas import (
    ConversationInboxDetail,
    ConversationInboxItem,
    ReplyWorkActionRequest,
    ReplyWorkActionView,
    ReplyWorkStatus,
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
        candidate_fields: tuple[ReplyFieldEvidence, ...] = (),
        suppress_scope: ReplySuppressScope | None = None,
    ) -> tuple[str, ...]:
        """落分类结果，返回 ``REPLY_ACTIONS`` 对应的动作序列。

        动作的**执行**在工作流（reply_qualification），本域只返回
        「该做什么」。发布 ``ReplyReceived``（AUTO_REPLY 除外——
        自动回复不算回复）。``outbound_message_id`` 是被回复出站消息的
        RFC Message-ID（In-Reply-To/References 关联）；无关联传 None，
        订阅方 fail-closed。
        """
        ...

    async def get_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
    ) -> MessageClassification | None:
        """读取 tenant-bound 分类事实，供动作重试复用其耐久发生时间。"""
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

    async def enqueue_reply_work_action(
        self,
        tenant_id: TenantId,
        request: ReplyWorkActionRequest,
    ) -> ReplyWorkActionView:
        """按 message+action 创建 owner-facing metadata-only 工作项；重试幂等。"""
        ...

    async def list_reply_work_queue(
        self,
        tenant_id: TenantId,
        *,
        status: ReplyWorkStatus,
        limit: int,
    ) -> tuple[ReplyWorkActionView, ...]:
        """读取本租户 owner queue；不返回客户正文、主题或地址。"""
        ...

    async def suggest_next_questions(
        self,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        missing_fields: list[str],
        completeness: int,
    ) -> NextQuestionSuggestion:
        """下一问建议（确定性选择，只读）。

        - ``missing_fields`` 顺序是**上游优先级契约**（最关键的在前）；
          本域不做 topic 白名单、不发明业务优先级，仅稳定去重
          （保留首次出现）后取前 2
        - 空列表合法：无缺失字段即无追问（``topics`` 为空）
        - ``completeness`` 是 0–5 确定性等级（非模型置信度），仅用于
          reason 说明；输入由上层从 demand 域查得传入
        - 会话必须存在于本租户（不存在/跨租户不可见 → fail-closed）
        - 只读：不发布事件、不写 outbox、不写日志
        """
        ...

    async def get_conversation(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> Conversation: ...

    async def list_messages(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> list[Message]: ...

    async def list_inbox(
        self,
        tenant_id: TenantId,
        *,
        category: ReplyCategory | None,
        limit: int,
    ) -> list[ConversationInboxItem]:
        """列出最近会话，可按人工纠正后的有效分类过滤。"""
        ...

    async def get_inbox_detail(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> ConversationInboxDetail:
        """读取会话消息、模型原判、人工纠正与 artifact 公共引用。"""
        ...
