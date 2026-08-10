"""Phase 1 API 的唯一正式依赖装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    AuditLogger as EmployeeAuditLogger,
)
from domains.employees.permissions import (
    EmployeeAuthorizer,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
)
from domains.employees.permissions import (
    StandardAuditLogger as EmployeeStandardAuditLogger,
)
from domains.employees.schemas import EmployeeView
from domains.employees.service import EmployeeService
from domains.employees.service_impl import EmployeeServiceImpl
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    Phase1OpportunityAuthorizer,
    ScopeLevel,
)
from domains.opportunities.permissions import (
    StandardAuditLogger as OpportunityStandardAuditLogger,
)
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.service_impl import OpportunityServiceImpl
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from notification_gateway.channels.structured_log import StructuredLogChannel
from notification_gateway.models import (
    Notification,
    NotificationChannel,
    NotificationPriority,
)
from notification_gateway.router import NotificationRouter
from shared.errors import PolicyViolation
from shared.schemas.identifiers import EmployeeId, TenantId
from workflows.human_handoff.flow import (
    HandoffEscalationNotice,
    build_human_handoff_step_handlers,
    register_human_handoff,
)

from ..dependencies import ConfiguredApiDependencies, EmployeeServiceScope
from ..runtime_config import Phase1RuntimeSettings


@asynccontextmanager
async def employee_service_scope(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    now: Callable[[], datetime],
    authorizer: EmployeeAuthorizer,
    audit: EmployeeAuditLogger,
) -> AsyncIterator[EmployeeService]:
    """为一次调用创建独立员工服务事务，异常回滚且总是关闭会话。"""
    session = factory()
    try:
        employees = EmployeeRepositoryImpl(session, tenant_id)
        yield EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=now,
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=authorizer,
            audit=audit,
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


class RequestScopedHandoffEmployeeReader:
    """让 workflow 的每次员工读取使用独立 service scope。"""

    def __init__(self, scope: EmployeeServiceScope) -> None:
        self._scope = scope

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView:
        async with self._scope(tenant_id) as service:
            return await service.get_employee(tenant_id, employee_id, actor=actor)


class StructuredLogOnlyPolicy:
    """Phase 1 仅允许唯一的结构化日志通知渠道。"""

    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        del notification
        if len(available) != 1 or available[0].name != "structured_log":
            raise PolicyViolation("Phase 1 通知渠道配置无效")
        return list(available)


class RuntimeHandoffNotifier:
    """把 workflow 的最小通知 DTO 转成安全的统一通知。"""

    def __init__(self, router: NotificationRouter) -> None:
        self._router = router

    async def notify(self, notice: HandoffEscalationNotice) -> None:
        await self._router.dispatch(
            Notification(
                tenant_id=notice.tenant_id,
                recipient=notice.recipient_id,
                priority=NotificationPriority.URGENT,
                title="人工接管提醒",
                context={
                    "kind": "handoff_escalation",
                    "level": notice.level,
                },
                source_event="HandoffRequested",
                dedup_key=notice.dedup_key,
                next_step="处理人工接管任务",
                due_at=notice.sla_due_at,
                link=f"/crm/handoffs/{notice.handoff_id}",
            )
        )


def build_phase1_dependencies(
    settings: Phase1RuntimeSettings,
    factory: async_sessionmaker[AsyncSession],
    *,
    now: Callable[[], datetime],
) -> ConfiguredApiDependencies:
    """装配真实 Postgres、领域服务、workflow、outbox 与通知出口。"""
    tenant = TenantId(settings.tenant_id)
    opportunity_authorizer = Phase1OpportunityAuthorizer(tenant)
    employee_authorizer = Phase1EmployeeAuthorizer(tenant)
    opportunity_audit = OpportunityStandardAuditLogger()
    employee_audit = EmployeeStandardAuditLogger()
    employees = partial(
        employee_service_scope,
        factory,
        now=now,
        authorizer=employee_authorizer,
        audit=employee_audit,
    )
    opportunities = OpportunityServiceImpl(
        # 可写 Protocol 属性不协变；具体 UoW 的仓储/总线逐项实现同一公共契约。
        lambda: SqlAlchemyOpportunityUnitOfWork(  # type: ignore[arg-type, return-value]
            factory, tenant
        ),
        OpportunityScorerImpl(settings.scoring_policy),
        settings.handoff_policy,
        authorizer=opportunity_authorizer,
        audit=opportunity_audit,
        now=now,
    )
    dedup = PostgresNotificationDedupStore(factory, now=now)
    router = NotificationRouter(dedup, StructuredLogOnlyPolicy())
    router.register_channel(StructuredLogChannel())
    employee_system_actor = EmployeeActor(
        "system:phase1-handoff",
        EmployeeScope.SYSTEM,
        "system",
    )
    opportunity_system_actor = OpportunityActor(
        "system:phase1-handoff",
        OpportunityScope(level=ScopeLevel.SYSTEM),
        "system",
    )
    handlers = build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=RequestScopedHandoffEmployeeReader(employees),
        notifier=RuntimeHandoffNotifier(router),
        opportunity_system_actor=opportunity_system_actor,
        employee_system_actor=employee_system_actor,
        t1=settings.t1,
        t2=settings.t2,
        now=now,
    )
    workflow = PostgresWorkflowEngine(factory, handlers, now=now)
    outbox = OutboxDeliverer(
        factory,
        tenant,
        now=now,
        max_attempts=settings.outbox_max_attempts,
    )
    register_human_handoff(workflow, outbox, t1=settings.t1, t2=settings.t2)
    return ConfiguredApiDependencies(
        opportunities=opportunities,
        employees=employees,
        opportunity_authorizer=opportunity_authorizer,
        employee_authorizer=employee_authorizer,
        workflow_engine=workflow,
        outbox_deliverer=outbox,
        notification_router=router,
        notification_dedup_store=dedup,
        employee_lookup_actor=employee_system_actor,
    )
