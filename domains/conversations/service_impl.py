"""会话域服务实现（浅域）：分类留痕与动作决定。

动作**决定**在域（``REPLY_ACTIONS``），**执行**在 reply_qualification 工作流。
事件只用共享 ``ReplyReceived`` 契约（AUTO_REPLY 除外，永不发布）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from domains.conversations.events import PUBLISHES as _CONVERSATIONS_PUBLISHES
from domains.conversations.models import (
    REPLY_ACTIONS,
    MessageClassification,
    ReplyCategory,
)
from domains.conversations.repository import ClassificationRepository
from shared.errors import ValidationError
from shared.events.catalog import ReplyReceived
from shared.schemas.identifiers import MessageId, TenantId

del _CONVERSATIONS_PUBLISHES  # 事件契约声明仅供文档；发布经 uow.bus


class ConversationsServiceImpl:
    """``ConversationsService`` 的 Postgres 实现。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], Any],
        *,
        now: Callable[[], datetime],
    ) -> None:
        if not callable(uow_factory) or not callable(now):
            raise ValidationError("会话服务依赖无效")
        self._uow_factory = uow_factory
        self._now = now

    @staticmethod
    def _validate_now(value: datetime) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValidationError("服务时钟必须为 UTC")
        return value

    async def record_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        category: ReplyCategory,
        classified_by: str,
    ) -> tuple[str, ...]:
        """落分类留痕并返回 ``REPLY_ACTIONS`` 动作序列（幂等契约见 docstring）。

        幂等/冲突契约（显式，不静默）：
        - 同 (tenant, message, classified_by) 同类别 → 幂等 no-op，返回既有动作；
        - 同 (tenant, message, classified_by) 不同类别 → 冲突 ValidationError
          （拒绝覆盖）；
        - 同 (tenant, message) 不同 classified_by（跨模型版本重评）→ 显式拒绝
          ValidationError（HANDBOOK/现有 schema 无跨版本重评要求；未来由显式
          reclassify API 承担）。
        发布 ``ReplyReceived``（共享契约，不含正文）：同一 inbound message
        至多一次；AUTO_REPLY 永不发布。
        """
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValidationError("会话租户无效")
        if not isinstance(message_id, str) or not message_id:
            raise ValidationError("会话消息无效")
        if not isinstance(category, ReplyCategory):
            raise ValidationError("分类类别无效")
        if not isinstance(classified_by, str) or not classified_by.strip():
            raise ValidationError("分类者标识无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            # 同 message 事务级串行化：并发双写/双发布由锁 + 唯一约束兜底
            await uow.lock_message(message_id)
            classifications: ClassificationRepository = uow.classifications
            existing = await classifications.get(tenant_id, message_id)
            if existing is not None:
                if existing.classified_by != classified_by:
                    raise ValidationError(
                        "同 message 不允许跨模型版本重评（未来由显式 reclassify API 承担）"
                    )
                if existing.category is not category:
                    raise ValidationError(
                        "同 message+分类者分类冲突，拒绝覆盖"
                    )
                return REPLY_ACTIONS[category]
            await classifications.add(
                MessageClassification(
                    tenant_id=tenant_id,
                    message_id=message_id,
                    category=category,
                    classified_by=classified_by,
                    classified_at=now,
                )
            )
            if (
                category is not ReplyCategory.AUTO_REPLY
                and not await uow.has_published_reply(tenant_id, message_id)
            ):
                await uow.bus.publish(
                    ReplyReceived(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        message_id=message_id,
                        conversation_id=None,
                        reply_category=category.value,
                    )
                )
        return REPLY_ACTIONS[category]
