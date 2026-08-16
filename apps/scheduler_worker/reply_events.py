"""scheduler 的 reply_qualification 生产事件接线：消费 InboundMessageStored。

``InboundMessageStored`` 是 conversations 域在入站消息落库的同事务发布的前置
信号（metadata-only，最小披露）。本 handler 按以下顺序校验，任一不满足即
fail-closed（不起 run、零副作用）：

1. ``event.tenant_id == self._tenant_id``（第一条显式校验，不依赖绑定查询兜底）
2. ``event.outbound_message_id`` 非 None
3. tenant-bound 查 ``MessageRow``：行存在、``direction == "inbound"``、
   ``row.outbound_message_id`` 非空且与事件值完全一致（防「真实 message +
   另一 attempt 的 outbound id」拼接导致错误 enrollment/contact 被分类抑制）
4. 按**经一致性校验后的持久化关联值**（``str(event.outbound_message_id)``）查
   ``OutreachMessageAttemptRow``（tenant-bound），无匹配返回
5. 由 attempt 的 enrollment_id 查 ``OutreachEnrollmentRow``（tenant-bound）取
   account/contact，缺失返回

通过后 ``engine.start`` 启动 reply_qualification run：subject_ref = message_id，
幂等键 ``reply:{message_id}``，initial_context 只含五个 typed ID（message_id /
outbound_message_id / enrollment_id / account_id / contact_point_id），不含
正文、artifact 引用、地址、category。

未知事件类型抛固定安全 ``ValidationError``；不捕获/回显原始异常或内容。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import MessageRow, OutreachEnrollmentRow, OutreachMessageAttemptRow
from shared.errors import ValidationError
from shared.events.catalog import InboundMessageStored
from shared.schemas.identifiers import TenantId
from workflows.engine.runner import WorkflowEngine


class ReplyQualificationEventHandlers:
    """InboundMessageStored 的 outbox handler（生产事件接线，非测试直投）。"""

    def __init__(
        self,
        *,
        engine: WorkflowEngine,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._engine = engine
        self._factory = factory
        self._tenant_id = tenant_id

    async def handle(self, event: object) -> None:
        """EventHandler 入口：仅接受 InboundMessageStored，未知类型拒绝。"""
        if isinstance(event, InboundMessageStored):
            await self.on_inbound_message_stored(event)
            return
        raise ValidationError("未知 reply 触发事件类型")

    async def on_inbound_message_stored(self, event: InboundMessageStored) -> None:
        # 1) 租户校验（第一条）
        if event.tenant_id != self._tenant_id:
            return
        # 2) 出站关联必填
        if event.outbound_message_id is None:
            return
        outbound_id = str(event.outbound_message_id)
        # 3) tenant-bound 消息行 + direction + 持久化关联一致性
        async with self._factory() as session:
            message = (
                await session.execute(
                    select(MessageRow).where(
                        MessageRow.tenant_id == str(self._tenant_id),
                        MessageRow.message_id == str(event.message_id),
                    )
                )
            ).scalars().first()
            if message is None or message.direction != "inbound":
                return
            if message.outbound_message_id is None:
                return
            if message.outbound_message_id != outbound_id:
                return
            # 4) tenant-bound attempt 查询（用经一致性校验后的持久化关联值）
            attempt = (
                await session.execute(
                    select(OutreachMessageAttemptRow).where(
                        OutreachMessageAttemptRow.tenant_id == str(self._tenant_id),
                        OutreachMessageAttemptRow.deterministic_message_id
                        == outbound_id,
                    )
                )
            ).scalars().first()
            if attempt is None:
                return
            # 5) tenant-bound enrollment → account/contact
            enrollment = await session.get(
                OutreachEnrollmentRow,
                (str(self._tenant_id), attempt.enrollment_id),
            )
        if enrollment is None:
            return
        await self._engine.start(
            self._tenant_id,
            "reply_qualification",
            str(event.message_id),
            {
                "message_id": str(event.message_id),
                "outbound_message_id": outbound_id,
                "enrollment_id": attempt.enrollment_id,
                "account_id": enrollment.account_id,
                "contact_point_id": enrollment.contact_point_id,
            },
            f"reply:{event.message_id}",
        )
