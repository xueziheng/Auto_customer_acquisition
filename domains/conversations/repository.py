"""会话域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self, runtime_checkable

from domains.conversations.models import Conversation, Message, MessageClassification
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class ConversationRepository(Protocol):
    async def add(self, conversation: Conversation) -> None: ...

    async def update(self, conversation: Conversation) -> None: ...

    async def advance_last_inbound_at(
        self,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        sent_at: datetime,
    ) -> None:
        """单调推进 last_inbound_at = max(既有, sent_at)。

        实现必须是单条原子 UPDATE（GREATEST/COALESCE），不得整实体
        read-compare + merge——并发不同 sent_at 下旧值后提交会回退。
        """
        ...

    async def get(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> Conversation | None: ...

    async def find_by_account_channel(
        self, tenant_id: TenantId, account_id: ProspectAccountId, channel: str
    ) -> Conversation | None: ...


@runtime_checkable
class ClassificationRepository(Protocol):
    """分类留痕存储。每 (tenant, message) 至多一条（跨版本重评由服务层显式拒绝）。"""

    async def add(self, classification: MessageClassification) -> None: ...

    async def get(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
    ) -> MessageClassification | None:
        """该 message 的分类记录（每 message 至多一条）。"""
        ...


@runtime_checkable
class ConversationsUnitOfWork(Protocol):
    """conversations 域事务边界（域级接口；实现为 SqlAlchemyConversationsUnitOfWork）。

    服务层只依赖本 Protocol——仓储、消息锁与事件总线都在事务内串行化。
    """

    classifications: ClassificationRepository
    conversations: ConversationRepository
    messages: MessageRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...

    async def lock_message(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> None: ...

    async def has_published_reply(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> bool: ...


class MessageRepository(Protocol):
    async def add(self, message: Message) -> None: ...

    async def get(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> Message | None: ...

    async def update(self, message: Message) -> None: ...

    async def find_by_external_id(
        self, tenant_id: TenantId, external_message_id: str
    ) -> Message | None:
        """按邮件 Message-ID 头查重（入站幂等）。"""
        ...

    async def list_for_conversation(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> list[Message]: ...

    async def has_inbound_since(
        self, tenant_id: TenantId, conversation_id: ConversationId, since: str
    ) -> bool:
        """某时刻后有无入站消息。outreach 的 ``prepare_send``
        用它关闭 stop_on_reply 竞态——发送前现查，不信缓存。"""
        ...
