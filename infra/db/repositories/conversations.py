"""conversations 域仓储实现（tenant-bound；分类留痕）。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.conversations.inbox_access import InboxActor
from domains.conversations.models import (
    ClassificationCorrection,
    Conversation,
    Message,
    MessageClassification,
    MessageDirection,
    ReplyCategory,
    ReplyFieldEvidence,
    ReplySuppressScope,
    ReplyWorkAction,
    ReplyWorkQueue,
    ReplyWorkRecord,
    ReplyWorkStatus,
)
from domains.conversations.repository import (
    ClassificationRepository,
    ConversationRepository,
    MessageRepository,
    ReplyWorkRepository,
)
from infra.db.inbox_access import inbox_predicate
from infra.db.tables import (
    ConversationClassificationCorrectionRow,
    ConversationClassificationRow,
    ConversationReplyWorkRow,
    ConversationRow,
    MessageRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
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
        candidate_fields=[
            {"field": item.field, "value": item.value, "quote": item.quote}
            for item in classification.candidate_fields
        ],
        suppress_scope=(
            classification.suppress_scope.value
            if classification.suppress_scope is not None
            else None
        ),
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
        candidate_fields=tuple(
            ReplyFieldEvidence(
                field=str(item["field"]),
                value=str(item["value"]),
                quote=str(item["quote"]),
            )
            for item in row.candidate_fields
        ),
        suppress_scope=(
            ReplySuppressScope(row.suppress_scope)
            if row.suppress_scope is not None
            else None
        ),
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

    async def add_correction(self, correction: ClassificationCorrection) -> bool:
        """tenant-bound append；UNIQUE(tenant,message,corrected_by,corrected_category)
        ON CONFLICT DO NOTHING → True=新插入，False=幂等冲突。"""
        self._require_tenant(
            correction.tenant_id, "conversation_classification_correction_add"
        )
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
        if not self._tenant_matches(
            tenant_id, "conversation_classification_correction_list"
        ):
            raise TenantIsolationViolation("跨租户数据隔离违规")
        rows = (
            (
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
            )
            .scalars()
            .all()
        )
        return [_row_to_correction(row) for row in rows]


def _row_to_reply_work(row: ConversationReplyWorkRow) -> ReplyWorkRecord:
    return ReplyWorkRecord(
        action_id=row.action_id,
        tenant_id=TenantId(row.tenant_id),
        message_id=MessageId(row.message_id),
        outbound_message_id=OutboundMessageId(row.outbound_message_id),
        enrollment_id=EnrollmentId(row.enrollment_id),
        account_id=ProspectAccountId(row.account_id),
        contact_point_id=ContactPointId(row.contact_point_id),
        action=ReplyWorkAction(row.action),
        owner_queue=ReplyWorkQueue(row.owner_queue),
        status=ReplyWorkStatus(row.status),
        idempotency_key=IdempotencyKey(row.idempotency_key),
        created_at=row.created_at,
    )


class ReplyWorkRepositoryImpl(_ConversationsRepository, ReplyWorkRepository):
    """回复 owner queue；所有唯一冲突先 no-op，再由服务层比对语义。"""

    async def add_if_absent(self, record: ReplyWorkRecord) -> None:
        self._require_tenant(record.tenant_id, "conversation_reply_work_add")
        await self._session.execute(
            pg_insert(ConversationReplyWorkRow)
            .values(
                tenant_id=str(record.tenant_id),
                action_id=record.action_id,
                message_id=str(record.message_id),
                outbound_message_id=str(record.outbound_message_id),
                enrollment_id=str(record.enrollment_id),
                account_id=str(record.account_id),
                contact_point_id=str(record.contact_point_id),
                action=record.action.value,
                owner_queue=record.owner_queue.value,
                status=record.status.value,
                idempotency_key=str(record.idempotency_key),
                created_at=record.created_at,
            )
            .on_conflict_do_nothing()
        )

    async def get_by_message_action(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        action: ReplyWorkAction,
    ) -> ReplyWorkRecord | None:
        self._require_tenant(tenant_id, "conversation_reply_work_get")
        row = (
            await self._session.execute(
                select(ConversationReplyWorkRow).where(
                    ConversationReplyWorkRow.tenant_id == str(tenant_id),
                    ConversationReplyWorkRow.message_id == str(message_id),
                    ConversationReplyWorkRow.action == action.value,
                )
            )
        ).scalar_one_or_none()
        return _row_to_reply_work(row) if row is not None else None

    async def list_by_status(
        self,
        tenant_id: TenantId,
        status: ReplyWorkStatus,
        *,
        limit: int,
    ) -> list[ReplyWorkRecord]:
        self._require_tenant(tenant_id, "conversation_reply_work_list")
        rows = (
            (
                await self._session.execute(
                    select(ConversationReplyWorkRow)
                    .where(
                        ConversationReplyWorkRow.tenant_id == str(tenant_id),
                        ConversationReplyWorkRow.status == status.value,
                    )
                    .order_by(
                        ConversationReplyWorkRow.created_at,
                        ConversationReplyWorkRow.action_id,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_reply_work(row) for row in rows]


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
            (
                await self._session.execute(
                    select(ConversationRow).where(
                        ConversationRow.tenant_id == str(tenant_id),
                        ConversationRow.account_id == str(account_id),
                        ConversationRow.channel == channel,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        self._require_tenant(TenantId(row.tenant_id), "conversation.find")
        return _row_to_conversation(row)

    async def get_inbox(
        self, tenant_id: TenantId, conversation_id: ConversationId, *, actor: InboxActor
    ) -> Conversation | None:
        """仓储独立拒绝缺actor，并在单SQL语句限定当前员工与归属。"""
        self._require_tenant(tenant_id, "conversation.get_inbox")
        row = (
            await self._session.execute(
                select(ConversationRow).where(
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.conversation_id == conversation_id,
                    inbox_predicate(tenant_id, actor),
                )
            )
        ).scalar_one_or_none()
        return _row_to_conversation(row) if row else None

    async def list_recent(
        self, tenant_id: TenantId, *, actor: InboxActor, limit: int
    ) -> list[Conversation]:
        """tenant-bound 最近活动列表；稳定次序便于分页前的 Phase 1 展示。"""
        self._require_tenant(tenant_id, "conversation.list_recent")
        rows = (
            (
                await self._session.execute(
                    select(ConversationRow)
                    .where(
                        ConversationRow.tenant_id == str(tenant_id),
                        inbox_predicate(tenant_id, actor),
                    )
                    .order_by(
                        func.greatest(
                            ConversationRow.last_inbound_at,
                            ConversationRow.last_outbound_at,
                            ConversationRow.created_at,
                        ).desc(),
                        ConversationRow.conversation_id,
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_conversation(row) for row in rows]


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
            .on_conflict_do_nothing(index_elements=["tenant_id", "external_message_id"])
        )

    async def get(self, tenant_id: TenantId, message_id: MessageId) -> Message | None:
        self._require_tenant(tenant_id, "message.get")
        row = await self._session.get(MessageRow, (str(tenant_id), str(message_id)))
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
            (
                await self._session.execute(
                    select(MessageRow).where(
                        MessageRow.tenant_id == str(tenant_id),
                        MessageRow.external_message_id == external_message_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        self._require_tenant(TenantId(row.tenant_id), "message.find")
        return _row_to_message(row)

    async def list_for_conversation(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> list[Message]:
        self._require_tenant(tenant_id, "message.list_for_conversation")
        rows = (
            (
                await self._session.execute(
                    select(MessageRow)
                    .where(
                        MessageRow.tenant_id == str(tenant_id),
                        MessageRow.conversation_id == str(conversation_id),
                    )
                    .order_by(MessageRow.sent_at)
                )
            )
            .scalars()
            .all()
        )
        return [_row_to_message(row) for row in rows]

    async def account_reply_summary(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
    ) -> tuple[bool, datetime | None]:
        self._require_tenant(tenant_id, "message.account_reply_summary")
        correction = (
            select(ConversationClassificationCorrectionRow.corrected_category)
            .where(
                ConversationClassificationCorrectionRow.tenant_id == str(tenant_id),
                ConversationClassificationCorrectionRow.message_id
                == MessageRow.message_id,
            )
            .order_by(
                ConversationClassificationCorrectionRow.corrected_at.desc(),
                ConversationClassificationCorrectionRow.correction_id.desc(),
            )
            .limit(1)
            .correlate(MessageRow)
            .scalar_subquery()
        )
        category = func.coalesce(correction, ConversationClassificationRow.category)
        statement = (
            select(
                func.bool_or(category.is_(None)),
                func.max(MessageRow.sent_at).filter(category != "auto_reply"),
            )
            .select_from(MessageRow)
            .join(
                ConversationRow,
                (ConversationRow.tenant_id == MessageRow.tenant_id)
                & (ConversationRow.conversation_id == MessageRow.conversation_id),
            )
            .outerjoin(
                ConversationClassificationRow,
                (ConversationClassificationRow.tenant_id == MessageRow.tenant_id)
                & (ConversationClassificationRow.message_id == MessageRow.message_id),
            )
            .where(
                MessageRow.tenant_id == str(tenant_id),
                ConversationRow.account_id == str(account_id),
                ConversationRow.channel == "email",
                MessageRow.direction == "inbound",
            )
        )
        row = (await self._session.execute(statement)).one()
        return bool(row[0]), row[1]

    async def has_inbound_since(
        self, tenant_id: TenantId, conversation_id: ConversationId, since: str
    ) -> bool:
        self._require_tenant(tenant_id, "message.has_inbound_since")
        row = (
            (
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
            )
            .scalars()
            .first()
        )
        return row is not None
