"""会话域服务实现（浅域）：入站消息持久化、分类留痕、动作决定与人工纠正。

动作**决定**在域（``REPLY_ACTIONS``），**执行**在 reply_qualification 工作流。
事件只用共享契约（``ReplyReceived`` AUTO_REPLY 除外永不发布；
``InboundMessageStored`` 仅 metadata-only typed ID，无正文/对象键）。
人工纠正（``correct_classification``）append-only 留痕：不发布事件、不改原
分类行——纠正样本是未来评估集摄取的耐久来源，覆盖掉就丢了。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from domains.conversations.errors import ReingestConflictError
from domains.conversations.models import (
    REPLY_ACTIONS,
    ClassificationCorrection,
    Conversation,
    Message,
    MessageClassification,
    MessageDirection,
    NextQuestionSuggestion,
    ReplyCategory,
)
from domains.conversations.repository import (
    ClassificationRepository,
    ConversationsUnitOfWork,
)
from shared.errors import ValidationError
from shared.events.catalog import InboundMessageStored, ReplyReceived
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)


class ConversationServiceImpl:
    """``ConversationService`` 的 Postgres 实现。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], ConversationsUnitOfWork],
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
        """入站消息落库 + 同事务发布 InboundMessageStored（契约见 service.py）。

        并发安全：conversations 用 (tenant, account, channel) ON CONFLICT
        get-or-create；messages 用 (tenant, external_message_id) 冲突后重读
        语义比对——同语义返回既有 MessageId 且不重复发事件，异语义
        ``ReingestConflictError`` fail-closed（固定安全摘要）。
        """
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValidationError("会话租户无效")
        if not isinstance(account_id, str) or not account_id:
            raise ValidationError("会话账户无效")
        if (
            not isinstance(raw_artifact_ref, str)
            or not raw_artifact_ref.strip()
        ):
            raise ValidationError("消息原文引用无效")
        if (
            not isinstance(external_message_id, str)
            or not external_message_id.strip()
        ):
            raise ValidationError("消息 external Message-ID 无效")
        sent_at = self._validate_utc_input(sent_at, "sent_at")
        if outbound_message_id is not None and (
            not isinstance(outbound_message_id, str)
            or not outbound_message_id.strip()
        ):
            raise ValidationError("出站消息关联无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            conversation = await self._resolve_conversation(
                uow, tenant_id, conversation_id, account_id, sent_at
            )
            existing = await uow.messages.find_by_external_id(
                tenant_id, external_message_id
            )
            if existing is not None:
                self._require_semantic_identity(
                    existing,
                    conversation.conversation_id,
                    raw_artifact_ref,
                    sent_at,
                    outbound_message_id,
                )
                return existing.message_id
            message = Message(
                message_id=MessageId(new_id("msg")),
                tenant_id=tenant_id,
                conversation_id=conversation.conversation_id,
                direction=MessageDirection.INBOUND,
                sent_at=sent_at,
                raw_artifact_ref=raw_artifact_ref,
                external_message_id=external_message_id,
                outbound_message_id=outbound_message_id,
            )
            await uow.messages.add(message)
            # 并发冲突后重读胜者：语义一致 → 返回既有 id，不重复发事件
            winner = await uow.messages.find_by_external_id(
                tenant_id, external_message_id
            )
            if winner is None or winner.message_id != message.message_id:
                if winner is None:
                    raise ValidationError("消息写入竞态异常")
                self._require_semantic_identity(
                    winner,
                    conversation.conversation_id,
                    raw_artifact_ref,
                    sent_at,
                    outbound_message_id,
                )
                return winner.message_id
            await uow.bus.publish(
                InboundMessageStored(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    run_id=None,
                    message_id=message.message_id,
                    outbound_message_id=outbound_message_id,
                )
            )
            return message.message_id

    @staticmethod
    def _validate_utc_input(value: datetime, label: str) -> datetime:
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValidationError(f"{label} 必须为 UTC")
        return value

    async def _resolve_conversation(
        self,
        uow: ConversationsUnitOfWork,
        tenant_id: TenantId,
        conversation_id: ConversationId | None,
        account_id: ProspectAccountId,
        sent_at: datetime,
    ) -> Conversation:
        """conversation_id 非 None：必须存在且匹配，否则 fail-closed；
        None：按 (tenant, account, channel=email) 并发安全 get-or-create。"""
        channel = "email"
        if conversation_id is not None:
            existing = await uow.conversations.get(tenant_id, conversation_id)
            if existing is None:
                raise ValidationError("会话不存在")
            if existing.account_id != account_id:
                raise ValidationError("会话与账户不匹配")
            if existing.channel != channel:
                raise ValidationError("会话渠道不匹配")
            await self._bump_last_inbound(uow, existing, sent_at)
            return existing
        conversation = await uow.conversations.find_by_account_channel(
            tenant_id, account_id, channel
        )
        if conversation is None:
            created = Conversation(
                conversation_id=ConversationId(new_id("con")),
                tenant_id=tenant_id,
                account_id=account_id,
                channel=channel,
                created_at=self._now(),
                last_inbound_at=sent_at,
            )
            await uow.conversations.add(created)
            # 并发胜者可能已提交：重读既有行（无论赢/输都以其为准）
            conversation = await uow.conversations.find_by_account_channel(
                tenant_id, account_id, channel
            )
            if conversation is None:
                raise ValidationError("会话创建竞态异常")
        # 既有会话路径与竞态路径统一：last_inbound_at = max(既有, sent_at)
        await self._bump_last_inbound(uow, conversation, sent_at)
        return conversation

    async def _bump_last_inbound(
        self,
        uow: ConversationsUnitOfWork,
        conversation: Conversation,
        sent_at: datetime,
    ) -> None:
        """last_inbound_at = max(既有, sent_at)：委托仓储单条原子 UPDATE，
        不做整实体 read-compare+merge——并发不同 sent_at 下旧值后提交不回退。"""
        await uow.conversations.advance_last_inbound_at(
            conversation.tenant_id, conversation.conversation_id, sent_at
        )

    @staticmethod
    def _require_semantic_identity(
        message: Message,
        conversation_id: ConversationId,
        raw_artifact_ref: str,
        sent_at: datetime,
        outbound_message_id: OutboundMessageId | None,
    ) -> None:
        """同 external_message_id 的语义完全一致校验；不一致 fail-closed。

        account 语义已由 conversation_id 承载（conversation 唯一于
        tenant+account+channel）。"""
        if (
            message.conversation_id != conversation_id
            or message.raw_artifact_ref != raw_artifact_ref
            or message.sent_at != sent_at
            or message.outbound_message_id != outbound_message_id
        ):
            raise ReingestConflictError(
                "同 external_message_id 消息语义不一致，拒绝重复入库"
            )

    async def record_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        category: ReplyCategory,
        classified_by: str,
        *,
        outbound_message_id: OutboundMessageId | None = None,
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
        if outbound_message_id is not None and (
            not isinstance(outbound_message_id, str) or not outbound_message_id.strip()
        ):
            raise ValidationError("出站消息关联无效")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            # 同 message 事务级串行化：并发双写/双发布由锁 + 唯一约束兜底
            await uow.lock_message(tenant_id, message_id)
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
                        outbound_message_id=outbound_message_id,
                    )
                )
        return REPLY_ACTIONS[category]

    async def suggest_next_questions(
        self,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        missing_fields: list[str],
        completeness: int,
    ) -> NextQuestionSuggestion:
        """下一问建议（确定性选择；契约见 service.py docstring）。

        - 输入校验全部在开 UoW 前完成（固定摘要，不回显输入）
        - 稳定去重保留上游首次出现 → 截断前 2；不发明业务优先级/白名单
        - tenant-bound 校验会话存在：不存在/跨租户不可见 → "会话不存在"
          （即使 missing_fields 为空也检查）
        - 只读：不发布事件、不写 outbox、不写日志
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValidationError("会话租户无效")
        if len(tenant_id) > 32:
            raise ValidationError("会话租户超长")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValidationError("会话标识无效")
        if len(conversation_id) > 32:
            raise ValidationError("会话标识超长")
        if type(missing_fields) is not list:
            raise ValidationError("缺失字段必须是列表")
        for field in missing_fields:
            if (
                not isinstance(field, str)
                or not field.strip()
                or field != field.strip()
            ):
                raise ValidationError("缺失字段无效")
        if isinstance(completeness, bool) or not isinstance(completeness, int):
            raise ValidationError("完整度必须为 0–5 的整数")
        if not 0 <= completeness <= 5:
            raise ValidationError("完整度必须为 0–5 的整数")
        topics: list[str] = []
        seen: set[str] = set()
        for field in missing_fields:
            if field not in seen:
                seen.add(field)
                topics.append(field)
            if len(topics) == 2:
                break
        async with self._uow_factory(tenant_id) as uow:
            if await uow.conversations.get(tenant_id, conversation_id) is None:
                raise ValidationError("会话不存在")
        if not topics:
            reason = "无缺失字段，无需追问"
        else:
            reason = (
                f"完整度 {completeness}/5，缺失字段（按上游顺序取前 2）："
                f"{'、'.join(topics)}"
            )
        return NextQuestionSuggestion(topics=topics, reason=reason)

    async def correct_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        corrected_category: ReplyCategory,
        corrected_by: str,
    ) -> None:
        """人工纠正分类留痕（append-only；签名契约见 service.py，此处只谈实现语义）。

        - 原分类行永不修改：只 append ``ClassificationCorrection``；
          ``classified_by`` 保留模型版本，另记纠正人
        - 未分类 / 跨租户不可见 → ``ValidationError("消息尚未分类")``
          fail-closed（服务按租户绑定查询，不感知其他租户，不抛
          TenantIsolationViolation）
        - 输入校验（tenant/message/category/corrected_by 空值、长度上限
          32/100/100，与 DB 列对齐；时钟仅接受 UTC）全部在开 UoW 前完成，
          固定安全摘要、不回显输入；模型构造校验为兜底
        - 幂等：同 (tenant, message, corrected_by, corrected_category) 由 DB
          UNIQUE + ``ON CONFLICT DO NOTHING`` 保证至多一行（不 list-then-insert，
          TOCTOU 消除）；``add_correction`` 返回 bool 忽略
        - 不发布任何事件、不写日志
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValidationError("纠正租户无效")
        if len(tenant_id) > 32:
            raise ValidationError("纠正租户超长")
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("纠正消息无效")
        if len(message_id) > 100:
            raise ValidationError("纠正消息超长")
        if not isinstance(corrected_category, ReplyCategory):
            raise ValidationError("纠正类别无效")
        if not isinstance(corrected_by, str) or not corrected_by.strip():
            raise ValidationError("纠正人无效")
        if len(corrected_by) > 100:
            raise ValidationError("纠正人超长")
        now = self._validate_now(self._now())
        async with self._uow_factory(tenant_id) as uow:
            classifications: ClassificationRepository = uow.classifications
            if await classifications.get(tenant_id, message_id) is None:
                raise ValidationError("消息尚未分类")
            await classifications.add_correction(
                ClassificationCorrection(
                    correction_id=new_id("ccr"),
                    tenant_id=tenant_id,
                    message_id=message_id,
                    corrected_category=corrected_category,
                    corrected_by=corrected_by,
                    corrected_at=now,
                )
            )
