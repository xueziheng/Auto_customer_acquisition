"""scheduler 的 Campaign 生产事件接线：回复停序列 + 唤醒、身份激活唤醒。

经真实 outbox handler（EventHandler.handle）调用；状态变更全部经域服务与
引擎，测试不得直接 deliver 掩盖缺失。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.schemas import EnrollmentStopReason
from domains.outreach.service import OutreachService
from infra.db.tables import OutreachMessageAttemptRow, WorkflowRunRow
from shared.errors import ValidationError
from shared.events.catalog import ReplyReceived, SendingIdentityActivated
from shared.schemas.identifiers import EnrollmentId, RunId, TenantId
from workflows.engine.runner import WorkflowEngine
from workflows.outreach_campaign.flow import WORKFLOW_TYPE

_SENDER_ACTOR_ID = "system:scheduler-campaign-send"


class CampaignEventHandlers:
    """Campaign 流程的生产事件接线（真实 outbox handler，非测试直投）。

    ``ReplyReceived`` → 域 stop_enrollment(REPLY) 停序列 + deliver_event 唤醒
    wait_for_reply（handler 收束 run）。``SendingIdentityActivated`` → 广播
    deliver_event 唤醒 prepare-wait 的 run（非等待步骤 no-op，由引擎判定）。

    reply 的 message_id → Attempt 解析在 apps 层做租户过滤只读查询：域
    ``resolve_delivery_feedback`` 要求精确 identity scope，回复路径无法预知
    身份；状态变更仍全部经域服务与引擎。
    """

    def __init__(
        self,
        *,
        outreach: OutreachService,
        engine: WorkflowEngine,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._outreach = outreach
        self._engine = engine
        self._factory = factory
        self._tenant_id = tenant_id

    def _enrollment_actor(self, enrollment_id: EnrollmentId) -> OutreachActor:
        return OutreachActor(
            _SENDER_ACTOR_ID,
            OutreachScope(
                level=OutreachScopeLevel.SYSTEM,
                allowed_enrollment_ids=frozenset({enrollment_id}),
            ),
            "system",
        )

    async def handle(self, event: object) -> None:
        """EventHandler 入口：按事件类型分派；未知类型拒绝。"""
        if isinstance(event, ReplyReceived):
            await self.on_reply_received(event)
            return
        if isinstance(event, SendingIdentityActivated):
            await self.on_identity_activated(event)
            return
        raise ValidationError("未知 Campaign 事件类型")

    async def on_reply_received(self, event: ReplyReceived) -> None:
        if event.outbound_message_id is None:
            # 无出站关联：fail-closed（不停 enrollment、不唤醒 run）
            return
        async with self._factory() as session:
            row = (
                await session.execute(
                    select(OutreachMessageAttemptRow).where(
                        OutreachMessageAttemptRow.tenant_id
                        == str(self._tenant_id),
                        OutreachMessageAttemptRow.deterministic_message_id
                        == str(event.outbound_message_id),
                    )
                )
            ).scalars().first()
        if row is None:
            return
        enrollment_id = EnrollmentId(row.enrollment_id)
        await self._outreach.stop_enrollment(
            self._tenant_id,
            enrollment_id,
            EnrollmentStopReason.REPLY,
            actor=self._enrollment_actor(enrollment_id),
        )
        run_id = await self._engine.find_active_run(
            self._tenant_id, WORKFLOW_TYPE, str(enrollment_id)
        )
        if run_id is not None:
            await self._engine.deliver_event(
                self._tenant_id,
                run_id,
                "ReplyReceived",
                {"occurred_at": event.occurred_at.isoformat()},
            )

    async def on_identity_activated(self, event: SendingIdentityActivated) -> None:
        async with self._factory() as session:
            run_ids = (
                await session.execute(
                    select(WorkflowRunRow.run_id).where(
                        WorkflowRunRow.tenant_id == str(self._tenant_id),
                        WorkflowRunRow.workflow_type == WORKFLOW_TYPE,
                        WorkflowRunRow.status == "running",
                    )
                )
            ).scalars().all()
        for run_id in run_ids:
            await self._engine.deliver_event(
                self._tenant_id,
                RunId(run_id),
                "SendingIdentityActivated",
                {"occurred_at": event.occurred_at.isoformat()},
            )
