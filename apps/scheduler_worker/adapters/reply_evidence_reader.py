"""生产 ReplyEvidenceReader：按 tenant/message 重读分类与原件引用。"""

from __future__ import annotations

from collections.abc import Callable

from domains.conversations.service import ConversationsUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId

from ..reply_actions import (
    ReplyEvidenceSnapshot,
    ReplyFieldSnapshot,
)


class ConversationReplyEvidenceReader:
    """组合分类记录与消息元数据；正文仍只存在 artifact store。"""

    def __init__(
        self,
        conversations_uow_factory: Callable[[TenantId], ConversationsUnitOfWork],
    ) -> None:
        if not callable(conversations_uow_factory):
            raise ValidationError("回复证据读取器依赖无效")
        self._uow_factory = conversations_uow_factory

    async def load(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
    ) -> ReplyEvidenceSnapshot | None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValidationError("回复证据租户无效")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("回复证据 message_id 无效")
        async with self._uow_factory(tenant_id) as uow:
            classification = await uow.classifications.get(tenant_id, message_id)
            if classification is None:
                return None
            message = await uow.messages.get(tenant_id, message_id)
        if message is None:
            raise ValidationError("回复证据消息不存在")
        if (
            classification.tenant_id != tenant_id
            or classification.message_id != message_id
            or message.tenant_id != tenant_id
            or message.message_id != message_id
            or getattr(message.direction, "value", None) != "inbound"
            or not isinstance(message.raw_artifact_ref, str)
            or not message.raw_artifact_ref
        ):
            raise ValidationError("回复证据关联损坏")
        return ReplyEvidenceSnapshot(
            message_id=message_id,
            category=classification.category.value,
            classified_by=classification.classified_by,
            classified_at=classification.classified_at,
            raw_artifact_ref=message.raw_artifact_ref,
            candidate_fields=tuple(
                ReplyFieldSnapshot(item.field, item.value, item.quote)
                for item in classification.candidate_fields
            ),
        )
