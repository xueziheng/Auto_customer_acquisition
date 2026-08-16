"""conversations 域仓储实现（tenant-bound；分类留痕）。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.conversations.models import (
    ClassificationCorrection,
    Conversation,
    Message,
    MessageClassification,
    MessageDirection,
    ReplyCategory,
)
from domains.conversations.repository import (
    ClassificationRepository,
    ConversationRepository,
    MessageRepository,
)
from infra.db.tables import (
    ConversationClassificationCorrectionRow,
    ConversationClassificationRow,
    ConversationRow,
    MessageRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
)

_tenant_logger = __import__("logging").getLogger("infra.db.repositories.conversations")


class _ConversationsRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _tenant_matches(self, tenant_id: TenantId, action: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if not self._tenant_matches(tenant_id, action):
            raise TenantIsolationViolation("跨租户数据隔离违规")


def _classification_to_row(
    classification: MessageClassification,
) -> ConversationClassificationRow:
    return ConversationClassificationRow(
        tenant_id=str(classification.tenant_id),
        message_id=str(classification.message_id),
        category=classification.category.value,
        classified_by=classification.classified_by,
        classified_at=classification.classified_at,
    )


def _row_to_classification(
    row: ConversationClassificationRow,
) -> MessageClassification:
    return MessageClassification(
        tenant_id=TenantId(row.tenant_id),
        message_id=MessageId(row.message_id),
        category=ReplyCategory(row.category),
        classified_by=row.classified_by,
        classified_at=row.classified_at,
    )


def _row_to_correction(
    row: ConversationClassificationCorrectionRow,
) -> ClassificationCorrection:
    return ClassificationCorrection(
        correction_id=row.correction_id,
        tenant_id=TenantId(row.tenant_id),
        message_id=MessageId(row.message_id),
        corrected_category=ReplyCategory(row.corrected_category),
        corrected_by=row.corrected_by,
        corrected_at=row.corrected_at,
    )


class ClassificationRepositoryImpl(_ConversationsRepository, ClassificationRepository):
    """分类留痕：同 (tenant, message, classified_by) 唯一；查询强制租户过滤。"""

    async def add(self, classification: MessageClassification) -> None:
        self._require_tenant(
            classification.tenant_id, "conversation_classification_add"
        )
        self._session.add(_classification_to_row(classification))

    async def get(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
    ) -> MessageClassification | None:
        """该 message 的分类记录（每 message 至多一条；跨租户 fail closed）。"""
        if not self._tenant_matches(tenant_id, "conversation_classification_get"):
            raise TenantIsolationViolation("跨租户数据隔离违规")
        row = (
            await self._session.execute(
                select(ConversationClassificationRow).where(
                    ConversationClassificationRow.tenant_id == str(self._tenant_id),
                    ConversationClassificationRow.message_id == str(message_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_classification(row) if row is not None else None

    async def add_correction(
        self, correction: ClassificationCorrection
    ) -> bool:
        """tenant-bound append；UNIQUE(tenant,message,corrected_by,corrected_category)
        ON CONFLICT DO NOTHING → True=新插入，False=幂等冲突。"""
        self._require_tenant(correction.tenant_id, "conversation_classification_correction_add")
        result = await self._session.execute(
            pg_insert(ConversationClassificationCorrectionRow)
            .values(
                tenant_id=str(correction.tenant_id),
                correction_id=str(correction.correction_id),
                message_id=str(correction.message_id),
                corrected_category=correction.corrected_category.value,
                corrected_by=correction.corrected_by,
                corrected_at=correction.corrected_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "tenant_id",
                    "message_id",
                    "corrected_by",
                    "corrected_category",
                ]
            )
        )
        return cast(CursorResult, result).rowcount > 0

    async def list_corrections(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
    ) -> list[ClassificationCorrection]:
        """该 message 全部纠正，ORDER BY corrected_at ASC, correction_id ASC。"""
        if not self._tenant_matches(tenant_id, "conversation_classification_correction_list"):
            raise TenantIsolationViolation("跨租户数据隔离违规")
        rows = (
            await self._session.execute(
                select(ConversationClassificationCorrectionRow)
                .where(
                    ConversationClassificationCorrectionRow.tenant_id
                    == str(self._tenant_id),
                    ConversationClassificationCorrectionRow.message_id
                    == str(message_id),
                )
                .order_by(
                    ConversationClassificationCorrectionRow.corrected_at,
                    ConversationClassificationCorrectionRow.correction_id,
                )
            )
        ).scalars().all()
        return [_row_to_correction(row) for row in rows]


def _conversation_to_row(
    conversation: Conversation,
) -> ConversationRow:
    return ConversationRow(
        tenant_id=str(conversation.tenant_id),
        conversation_id=str(conversation.conversation_id),
        account_id=str(conversation.account_id),
        channel=conversation.channel,
        created_at=conversation.created_at,
        last_inbound_at=conversation.last_inbound_at,
        last_outbound_at=conversation.last_outbound_at,
    )


def _row_to_conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        conversation_id=ConversationId(row.conversation_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        channel=row.channel,
        created_at=row.created_at,
        last_inbound_at=row.last_inbound_at,
        last_outbound_at=row.last_outbound_at,
    )


class ConversationRepositoryImpl(_ConversationsRepository, ConversationRepository):
    """tenant-bound 会话仓储：get-or-create 的冲突由服务层 ON CONFLICT 兜底。"""

    async def add(self, conversation: Conversation) -> None:
        """get-or-create 的插入侧：UNIQUE(tenant, account, channel) 冲突
        DO NOTHING（并发胜者不报错），调用方随后重读既有行。"""
        self._require_tenant(conversation.tenant_id, "conversation.add")
        await self._session.execute(
            pg_insert(ConversationRow)
            .values(
                tenant_id=str(conversation.tenant_id),
                conversation_id=str(conversation.conversation_id),
                account_id=str(conversation.account_id),
                channel=conversation.channel,
                created_at=conversation.created_at,
                last_inbound_at=conversation.last_inbound_at,
                last_outbound_at=conversation.last_outbound_at,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "account_id", "channel"]
            )
        )

    async def update(self, conversation: Conversation) -> None:
        self._require_tenant(conversation.tenant_id, "conversation.update")
        await self._session.merge(_conversation_to_row(conversation))

    async def advance_last_inbound_at(
        self,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        sent_at: datetime,
    ) -> None:
        """单条原子 UPDATE：last_inbound_at = GREATEST(COALESCE(既有, sent_at),
        sent_at)。NULL 首写与并发不同 sent_at 均单调不回退；不覆盖其他字段。"""
        self._require_tenant(tenant_id, "conversation.advance_last_inbound_at")
        result = await self._session.execute(
            update(ConversationRow)
            .where(
                ConversationRow.tenant_id == str(tenant_id),
                ConversationRow.conversation_id == str(conversation_id),
            )
            .values(
                last_inbound_at=func.greatest(
                    func.coalesce(ConversationRow.last_inbound_at, sent_at),
                    sent_at,
                )
            )
        )
        if cast(CursorResult, result).rowcount != 1:
            raise ValidationError("会话不存在")

    async def get(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> Conversation | None:
        self._require_tenant(tenant_id, "conversation.get")
        row = await self._session.get(
            ConversationRow, (str(tenant_id), str(conversation_id))
        )
        if row is None:
            return None
        self._require_tenant(TenantId(row.tenant_id), "conversation.get")
        return _row_to_conversation(row)

    async def find_by_account_channel(
        self, tenant_id: TenantId, account_id: ProspectAccountId, channel: str
    ) -> Conversation | None:
        self._require_tenant(tenant_id, "conversation.find_by_account_channel")
        row = (
            await self._session.execute(
                select(ConversationRow).where(
                    ConversationRow.tenant_id == str(tenant_id),
                    ConversationRow.account_id == str(account_id),
                    ConversationRow.channel == channel,
                )
            )
        ).scalars().first()
        if row is None:
            return None
        self._require_tenant(TenantId(row.tenant_id), "conversation.find")
        return _row_to_conversation(row)


def _require_valid_external_id(message: Message) -> str:
    """仓储边界显式 fail-closed（固定摘要，不回显值）：None/空白若以 "" 落库
    会撞 ck_messages_external_id_nonblank 产生未分类 IntegrityError。"""
    if (
        not isinstance(message.external_message_id, str)
        or not message.external_message_id.strip()
    ):
        raise ValidationError("消息 external Message-ID 无效")
    return message.external_message_id


def _message_to_row(message: Message) -> MessageRow:
    _require_valid_external_id(message)
    return MessageRow(
        tenant_id=str(message.tenant_id),
        message_id=str(message.message_id),
        conversation_id=str(message.conversation_id),
        direction=message.direction.value,
        sent_at=message.sent_at,
        language=message.language,
        raw_artifact_ref=message.raw_artifact_ref,
        external_message_id=message.external_message_id,
        outbound_message_id=(
            str(message.outbound_message_id)
            if message.outbound_message_id is not None
            else None
        ),
    )


def _row_to_message(row: MessageRow) -> Message:
    return Message(
        message_id=MessageId(row.message_id),
        tenant_id=TenantId(row.tenant_id),
        conversation_id=ConversationId(row.conversation_id),
        direction=MessageDirection(row.direction),
        sent_at=row.sent_at,
        raw_artifact_ref=row.raw_artifact_ref,
        external_message_id=row.external_message_id or None,
        outbound_message_id=(
            OutboundMessageId(row.outbound_message_id)
            if row.outbound_message_id is not None
            else None
        ),
        language=row.language,
    )


class MessageRepositoryImpl(_ConversationsRepository, MessageRepository):
    """tenant-bound 消息仓储；external_message_id 唯一约束由 DB 强制。"""

    async def add(self, message: Message) -> None:
        """幂等插入侧：UNIQUE(tenant, external_message_id) 冲突 DO NOTHING
        （并发胜者不报错），调用方随后重读胜者行做语义比对。"""
        self._require_tenant(message.tenant_id, "message.add")
        await self._session.execute(
            pg_insert(MessageRow)
            .values(
                tenant_id=str(message.tenant_id),
                message_id=str(message.message_id),
                conversation_id=str(message.conversation_id),
                direction=message.direction.value,
                sent_at=message.sent_at,
                language=message.language,
                raw_artifact_ref=message.raw_artifact_ref,
                external_message_id=_require_valid_external_id(message),
                outbound_message_id=(
                    str(message.outbound_message_id)
                    if message.outbound_message_id is not None
                    else None
                ),
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "external_message_id"]
            )
        )

    async def get(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> Message | None:
        self._require_tenant(tenant_id, "message.get")
        row = await self._session.get(
            MessageRow, (str(tenant_id), str(message_id))
        )
        if row is None:
            return None
        self._require_tenant(TenantId(row.tenant_id), "message.get")
        return _row_to_message(row)

    async def update(self, message: Message) -> None:
        self._require_tenant(message.tenant_id, "message.update")
        await self._session.merge(_message_to_row(message))

    async def find_by_external_id(
        self, tenant_id: TenantId, external_message_id: str
    ) -> Message | None:
        self._require_tenant(tenant_id, "message.find_by_external_id")
        row = (
            await self._session.execute(
                select(MessageRow).where(
                    MessageRow.tenant_id == str(tenant_id),
                    MessageRow.external_message_id == external_message_id,
                )
            )
        ).scalars().first()
        if row is None:
            return None
        self._require_tenant(TenantId(row.tenant_id), "message.find")
        return _row_to_message(row)

    async def list_for_conversation(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> list[Message]:
        self._require_tenant(tenant_id, "message.list_for_conversation")
        rows = (
            await self._session.execute(
                select(MessageRow)
                .where(
                    MessageRow.tenant_id == str(tenant_id),
                    MessageRow.conversation_id == str(conversation_id),
                )
                .order_by(MessageRow.sent_at)
            )
        ).scalars().all()
        return [_row_to_message(row) for row in rows]

    async def has_inbound_since(
        self, tenant_id: TenantId, conversation_id: ConversationId, since: str
    ) -> bool:
        self._require_tenant(tenant_id, "message.has_inbound_since")
        row = (
            await self._session.execute(
                select(MessageRow.message_id)
                .where(
                    MessageRow.tenant_id == str(tenant_id),
                    MessageRow.conversation_id == str(conversation_id),
                    MessageRow.direction == "inbound",
                    MessageRow.sent_at > since,
                )
                .limit(1)
            )
        ).scalars().first()
        return row is not None
