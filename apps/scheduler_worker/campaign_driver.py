"""scheduler 的 Campaign 序列驱动：到期扫描起 run、失效 run 取消、发送适配。

事件（回复/身份熔断）不在此处理：``domains/outreach`` 的订阅已把对应
Enrollment 终态化或挂起发送计划，prepare 的域内二次检查兜住竞态；本驱动
只做「扫描到期 Enrollment 起 per-step run」与「取消已取消/已完成 Campaign
的未终态 run」。
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
from domains.outreach.schemas import (
    CampaignState,
    SendAuthorization,
)
from domains.outreach.service import OutreachService
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.permissions import (
    SendingIdentityScope,
)
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.tables import WorkflowRunRow
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    EnrollmentId,
    MessageAttemptId,
    RunId,
    SendingIdentityId,
    TenantId,
    UserId,
)
from tool_gateway.errors import ToolCallStatus
from tool_gateway.pipeline import ToolCallContext, ToolGateway
from workflows.engine.runner import WorkflowEngine
from workflows.outreach_campaign.flow import WORKFLOW_TYPE

_SENDER_ACTOR_ID = "system:scheduler-campaign-send"
_DRIVER_ACTOR_ID = "system:scheduler-campaign-driver"

#: permission stage 时刻 attempt 的合法状态：reserved（尚未 claim）、
#: sending（上次 claim 后崩溃重试）、sent（成功后的幂等重放）。
_ATTEMPT_STATES = frozenset({"reserved", "sending", "sent"})


def driver_actor() -> OutreachActor:
    """驱动扫描 actor：ENROLLMENT_LIST/CAMPAIGN_READ 需要 TENANT 级 boss 角色。

    SYSTEM 作用域强制收窄单一资源，无法表达整租户扫描；Phase 1 单租户下
    由调度进程以 TENANT 级只读权限扫描。
    """
    return OutreachActor(
        _DRIVER_ACTOR_ID,
        OutreachScope(level=OutreachScopeLevel.TENANT),
        "boss",
    )


def outreach_actor_for(ctx: ToolCallContext) -> OutreachActor:
    """发送链路的 SYSTEM enrollment 作用域 actor（精确单一资源）。"""
    enrollment_id = ctx.params.get("enrollment_id")
    if not isinstance(enrollment_id, str):
        raise ValidationError("序列发送 enrollment 绑定无效")
    return OutreachActor(
        _SENDER_ACTOR_ID,
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({EnrollmentId(enrollment_id)}),
        ),
        "system",
    )


def sending_actor_for(_ctx: ToolCallContext, preflight: object) -> SendingIdentityActor:
    """发送链路的 SYSTEM identity 作用域 actor（精确单一资源）。"""
    from domains.outreach.schemas import MessageSendPreflight

    if not isinstance(preflight, MessageSendPreflight):
        raise ValidationError("发送 preflight 无效")
    identity_id = SendingIdentityId(str(preflight.sending_identity_id))
    return SendingIdentityActor(
        _SENDER_ACTOR_ID,
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


class SchedulerCampaignPermissionCheck:
    """把 scheduler 发送 ctx 绑定到当前数据库事实；缺失/不匹配一律拒绝。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id

    async def authorize(self, ctx: ToolCallContext, _state: object) -> bool:
        attempt_id = ctx.params.get("attempt_id")
        enrollment_id = ctx.params.get("enrollment_id")
        if (
            not isinstance(attempt_id, str)
            or not isinstance(enrollment_id, str)
            or ctx.tenant_id != self._tenant_id
        ):
            return False
        async with SqlAlchemyOutreachUnitOfWork(
            self._factory, self._tenant_id
        ) as uow:
            attempt = await uow.attempts.get_for_update(
                self._tenant_id, MessageAttemptId(attempt_id)
            )
        if attempt is None:
            return False
        return (
            attempt.state in _ATTEMPT_STATES
            and str(attempt.campaign_id) == ctx.campaign_ref
            and str(attempt.enrollment_id) == enrollment_id
            and str(attempt.idempotency_key) == ctx.idempotency_key
        )


class SchedulerCampaignSender:
    """CampaignEmailSender 适配器：把 SendAuthorization 转成完整 email.send 调用。

    可重试失败 → TransientError（引擎退避重试）；永久失败/拒绝 → 固定
    永久错误（run 转 FAILED，可观测）。
    """

    def __init__(
        self,
        gateway: ToolGateway,
        tenant_id: TenantId,
        user_id: UserId,
    ) -> None:
        self._gateway = gateway
        self._tenant_id = tenant_id
        self._user_id = user_id

    async def send(
        self, *, tenant_id: TenantId, authorization: SendAuthorization
    ) -> str:
        if tenant_id != self._tenant_id:
            raise ValidationError("序列发送租户绑定无效")
        ctx = ToolCallContext(
            tenant_id=tenant_id,
            user_id=self._user_id,
            tool_id="email.send",
            params={
                "attempt_id": str(authorization.attempt_id),
                "subject": authorization.subject,
                "body": authorization.body,
                "enrollment_id": str(authorization.enrollment_id),
            },
            idempotency_key=authorization.idempotency_key,
            campaign_ref=str(authorization.campaign_id),
        )
        result = await self._gateway.invoke(ctx)
        if result.status in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
            provider_ref = (result.output or {}).get("provider_ref")
            if not isinstance(provider_ref, str) or not provider_ref:
                raise ValidationError("序列发送结果无效")
            return provider_ref
        if result.status is ToolCallStatus.FAILED_TRANSIENT:
            raise TransientError("序列发送网关暂时失败")
        raise ValidationError("序列发送被网关拒绝")


class CampaignSendDriver:
    """到期 Enrollment 驱动：先取消失效 run，再为到期者起 per-step run。"""

    def __init__(
        self,
        *,
        outreach: OutreachService,
        engine: WorkflowEngine,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        scan_actor: OutreachActor,
        batch_limit: int,
    ) -> None:
        self._outreach = outreach
        self._engine = engine
        self._factory = factory
        self._tenant_id = tenant_id
        self._scan_actor = scan_actor
        self._batch_limit = batch_limit

    async def _cancel_stale_runs(self) -> int:
        """取消已取消/已完成 Campaign 的未终态 run；暂停的 run 保留（prepare 重试）。"""
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.tenant_id == str(self._tenant_id),
                        WorkflowRunRow.workflow_type == WORKFLOW_TYPE,
                        WorkflowRunRow.status == "running",
                    )
                )
            ).scalars().all()
        cancelled = 0
        for row in rows:
            campaign_id = (row.context or {}).get("campaign_id")
            if not isinstance(campaign_id, str) or not campaign_id.startswith("cmp_"):
                continue
            view = await self._outreach.get_campaign(
                self._tenant_id, CampaignId(campaign_id), actor=self._scan_actor
            )
            if view.state in {CampaignState.CANCELLED, CampaignState.COMPLETED}:
                await self._engine.cancel(
                    self._tenant_id, RunId(row.run_id), "campaign not active"
                )
                cancelled += 1
        return cancelled

    async def scan_once(self) -> int:
        """取消失效 run 并为到期 Enrollment 起 per-step run；返回启动数。"""
        await self._cancel_stale_runs()
        due = await self._outreach.list_due_sequence_enrollments(
            self._tenant_id, limit=self._batch_limit, actor=self._scan_actor
        )
        started = 0
        for enrollment in due:
            await self._engine.start(
                self._tenant_id,
                WORKFLOW_TYPE,
                str(enrollment.enrollment_id),
                {
                    "enrollment_id": str(enrollment.enrollment_id),
                    "campaign_id": str(enrollment.campaign_id),
                },
                f"campaign:{enrollment.enrollment_id}:step{enrollment.current_step + 1}",
            )
            started += 1
        return started
