"""scheduler 的 Campaign 生产事件接线：回复停序列 + 唤醒、身份激活唤醒。

经真实 outbox handler（EventHandler.handle）调用；状态变更全部经域服务与
引擎，测试不得直接 deliver 掩盖缺失。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.approvals.service import ApprovalService, ApprovalType
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
from infra.db.tables import (
    OutreachCampaignRow,
    OutreachMessageAttemptRow,
    WorkflowRunRow,
)
from shared.errors import ValidationError
from shared.events.catalog import (
    ApprovalDecided,
    CampaignStateChanged,
    ReplyReceived,
    SendingIdentityActivated,
)
from shared.schemas.identifiers import ApprovalId, EnrollmentId, RunId, TenantId
from workflows.account_discovery.flow import WORKFLOW_TYPE as ACCOUNT_WORKFLOW_TYPE
from workflows.engine.runner import WorkflowEngine
from workflows.outreach_campaign.flow import WORKFLOW_TYPE

_SENDER_ACTOR_ID = "system:scheduler-campaign-send"
_ACCOUNT_WAIT_STEP = "await_campaign_activation"


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


class AccountDiscoveryCampaignEventHandlers:
    """用生产 outbox 把 Campaign 状态变化投递给账户发现等待步骤。"""

    def __init__(
        self,
        *,
        engine: WorkflowEngine,
        approvals: ApprovalService,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._engine = engine
        self._approvals = approvals
        self._factory = factory
        self._tenant_id = tenant_id

    async def handle(self, event: object) -> None:
        if isinstance(event, CampaignStateChanged):
            await self.on_campaign_state_changed(event)
            return
        if isinstance(event, ApprovalDecided):
            await self.on_approval_decided(event)
            return
        raise ValidationError("未知账户发现 Campaign 事件类型")

    async def _runs(
        self,
        campaign_id: str,
        *,
        only_waiting: bool,
    ) -> list[tuple[RunId, int | None]]:
        conditions = [
            WorkflowRunRow.tenant_id == str(self._tenant_id),
            WorkflowRunRow.workflow_type == ACCOUNT_WORKFLOW_TYPE,
            WorkflowRunRow.status == "running",
            WorkflowRunRow.context["campaign_id"].as_string() == campaign_id,
        ]
        if only_waiting:
            conditions.append(WorkflowRunRow.current_step == _ACCOUNT_WAIT_STEP)
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(WorkflowRunRow.run_id, WorkflowRunRow.context).where(
                        *conditions
                    )
                )
            ).all()
        result: list[tuple[RunId, int | None]] = []
        for run_id, context in rows:
            raw_version = context.get("campaign_version")
            version = (
                raw_version
                if isinstance(raw_version, int)
                and not isinstance(raw_version, bool)
                and raw_version > 0
                else None
            )
            result.append((RunId(run_id), version))
        return result

    async def on_campaign_state_changed(self, event: CampaignStateChanged) -> None:
        runs = await self._runs(
            str(event.campaign_id), only_waiting=event.state == "active"
        )
        for run_id, bound_version in runs:
            if event.state == "active":
                await self._engine.deliver_event(
                    self._tenant_id,
                    run_id,
                    "CampaignStateChanged",
                    {
                        "campaign_id": str(event.campaign_id),
                        "campaign_version": event.campaign_version,
                        "state": event.state,
                        "occurred_at": event.occurred_at.isoformat(),
                    },
                )
            elif event.state == "cancelled" or (
                event.state == "pending_approval"
                and bound_version is not None
                and bound_version != event.campaign_version
            ):
                await self._engine.cancel(
                    self._tenant_id, run_id, "campaign no longer activatable"
                )

    async def on_approval_decided(self, event: ApprovalDecided) -> None:
        if event.decision != "reject":
            return
        approval = await self._approvals.get(
            self._tenant_id, ApprovalId(event.approval_id)
        )
        if approval.approval_type != ApprovalType.CAMPAIGN_BOUNDARY_CHANGE.value:
            return
        change_set = approval.change_set_ref or ""
        parts = change_set.split(":")
        if (
            len(parts) != 3
            or parts[0] != "campaign"
            or not parts[2].startswith("v")
            or not parts[2][1:].isdigit()
        ):
            raise ValidationError("Campaign 审批变更集无效")
        campaign_id = parts[1]
        version = int(parts[2][1:])
        async with self._factory() as session:
            current_version = (
                await session.execute(
                    select(OutreachCampaignRow.current_version).where(
                        OutreachCampaignRow.tenant_id == str(self._tenant_id),
                        OutreachCampaignRow.campaign_id == campaign_id,
                    )
                )
            ).scalar_one_or_none()
        for run_id, bound_version in await self._runs(
            campaign_id, only_waiting=False
        ):
            if bound_version == version or (
                bound_version is None and current_version == version
            ):
                await self._engine.cancel(
                    self._tenant_id, run_id, "campaign approval rejected"
                )


__all__ = ("AccountDiscoveryCampaignEventHandlers", "CampaignEventHandlers")
