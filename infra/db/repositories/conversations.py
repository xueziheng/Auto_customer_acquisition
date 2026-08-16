"""conversations 域仓储实现（tenant-bound；分类留痕）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.conversations.models import MessageClassification, ReplyCategory
from domains.conversations.repository import ClassificationRepository
from infra.db.tables import ConversationClassificationRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import MessageId, TenantId

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
