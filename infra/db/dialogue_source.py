"""同企业精确对话关联；不根据主题、地址或时间猜测客户身份。"""
from dataclasses import asdict
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.dialogue_source import DialogueSource
from infra.db.dialogue_execution_tables import (
    DialogueBindingRow,
    DialogueExecutionRow,
    DialogueMessageRow,
)
from infra.db.tables import ConversationRow, MessageRow
from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId

_SOURCE_FIELDS = tuple(DialogueSource.__dataclass_fields__)


def source_from_row(row: DialogueBindingRow) -> DialogueSource:
    """固定字段白名单，不把运行计数或内容当来源证明。"""
    return DialogueSource(**{name: getattr(row, name) for name in _SOURCE_FIELDS})


class SqlDialogueSources:
    """供事件、分类资格、业务事实与证据verifier共享的读端口。"""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: TenantId) -> None:
        self.sessions, self.tenant_id = sessions, tenant_id

    def _tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self.tenant_id:
            raise ValidationError("对话来源租户不匹配")

    async def admit(self, db: AsyncSession, source: DialogueSource) -> None:
        """调用者持当前准入授权事务；这里只验证不可变关联并持久化映射。"""
        self._tenant(source.tenant_id)
        await db.execute(insert(DialogueBindingRow).values(**asdict(source),
            state="active", rounds_sent=0, model_calls=0, no_progress_rounds=0,
            latest_message_id=source.admitted_message_id, created_at=datetime.now(UTC),
        ).on_conflict_do_nothing(index_elements=["tenant_id", "binding_id"]))
        row = await db.get(DialogueBindingRow, (str(self.tenant_id), source.binding_id))
        if row is None or source_from_row(row) != source:
            raise ValidationError("对话准入来源不可改写")
        await self.bind_message(db, source.binding_id, source.admitted_message_id)

    async def bind_message(self, db: AsyncSession, binding_id: str, message_id: MessageId) -> None:
        """同入站事务绑定真实账户消息；新的客户消息使旧计划即时失效。"""
        row = await db.get(DialogueBindingRow, (str(self.tenant_id), binding_id), with_for_update=True)
        message = await db.get(MessageRow, (str(self.tenant_id), str(message_id)))
        if row is None or message is None or message.direction != "inbound":
            raise ValidationError("对话消息来源不存在")
        conversation = await db.get(ConversationRow, (str(self.tenant_id), message.conversation_id))
        if conversation is None or conversation.account_id != row.account_id:
            raise ValidationError("对话消息账户关联冲突")
        inserted = await db.scalar(insert(DialogueMessageRow).values(
            tenant_id=str(self.tenant_id), message_id=str(message_id), binding_id=binding_id,
        ).on_conflict_do_nothing(index_elements=["tenant_id", "message_id"]).returning(DialogueMessageRow.message_id))
        mapped = await db.get(DialogueMessageRow, (str(self.tenant_id), str(message_id)))
        if mapped is None or mapped.binding_id != binding_id:
            raise ValidationError("对话消息来源不可改写")
        # 只在首次接收入站映射时推进；邮件Date由发送者控制，不能决定计划新鲜性。
        if inserted is not None:
            row.latest_message_id = str(message_id)

    async def for_message(self, tenant_id: TenantId, message_id: MessageId) -> DialogueSource | None:
        """仅返回真实canonical消息及同账户映射，无映射保持旧冷路径。"""
        self._tenant(tenant_id)
        async with self.sessions() as db:
            return await self.for_message_in(db, tenant_id, message_id)

    async def for_message_in(self, db: AsyncSession, tenant_id: TenantId, message_id: MessageId) -> DialogueSource | None:
        """共享原事务快照，供分类与入站同时校验。"""
        self._tenant(tenant_id)
        return await read_dialogue_source(db, tenant_id, message_id)

    async def for_sent_header(self, tenant_id: TenantId, header: str) -> DialogueSource | None:
        """只有SENT精确Message-ID可延续原来源；unknown绝不猜成功。"""
        self._tenant(tenant_id)
        async with self.sessions() as db:
            return await self.for_sent_header_in(db, tenant_id, header)

    async def for_sent_header_in(self, db: AsyncSession, tenant_id: TenantId, header: str) -> DialogueSource | None:
        """入站与来源绑定共享原整页事务。"""
        self._tenant(tenant_id)
        row = await db.scalar(select(DialogueBindingRow).join(DialogueExecutionRow,
            (DialogueExecutionRow.tenant_id == DialogueBindingRow.tenant_id) &
            (DialogueExecutionRow.binding_id == DialogueBindingRow.binding_id),
        ).where(DialogueBindingRow.tenant_id == str(tenant_id),
            DialogueExecutionRow.tenant_id == str(tenant_id),
            DialogueExecutionRow.deterministic_message_id == header,
            DialogueExecutionRow.state == "sent"))
        return source_from_row(cast(DialogueBindingRow, row)) if row is not None else None


async def read_dialogue_source(db: AsyncSession, tenant_id: TenantId, message_id: MessageId) -> DialogueSource | None:
    """复用外层事务的显式租户查询，不拥有或创建session工厂。"""
    row = await db.scalar(select(DialogueBindingRow).join(
        DialogueMessageRow,
        (DialogueMessageRow.tenant_id == DialogueBindingRow.tenant_id) &
        (DialogueMessageRow.binding_id == DialogueBindingRow.binding_id),
    ).join(MessageRow,
        (MessageRow.tenant_id == DialogueMessageRow.tenant_id) &
        (MessageRow.message_id == DialogueMessageRow.message_id),
    ).join(ConversationRow,
        (ConversationRow.tenant_id == MessageRow.tenant_id) &
        (ConversationRow.conversation_id == MessageRow.conversation_id),
    ).where(DialogueBindingRow.tenant_id == str(tenant_id),
        DialogueMessageRow.tenant_id == str(tenant_id),
        MessageRow.tenant_id == str(tenant_id), ConversationRow.tenant_id == str(tenant_id),
        DialogueMessageRow.message_id == str(message_id),
        MessageRow.direction == "inbound", ConversationRow.account_id == DialogueBindingRow.account_id))
    return source_from_row(row) if row is not None else None
