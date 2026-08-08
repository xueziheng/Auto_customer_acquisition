"""会话域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.conversations.models import Conversation, Message
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class ConversationRepository(Protocol):
    async def add(self, conversation: Conversation) -> None: ...

    async def get(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> Conversation | None: ...

    async def find_by_account_channel(
        self, tenant_id: TenantId, account_id: ProspectAccountId, channel: str
    ) -> Conversation | None: ...


@runtime_checkable
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
