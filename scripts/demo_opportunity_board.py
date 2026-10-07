"""Slice 3 机会看板真实演示。

本文件是**仅供演示的 composition root**，不能作为 production wiring：它显式装配
最小默认拒绝 authorizer、通知适配器和 structured-log-only policy，再使用真实
Postgres service/UoW/workflow/outbox/notification 组件生成可复查的业务事实。

连接串只从 ``DATABASE_URL`` 读取；stdout 仅输出真实 read-back 的 JSON summary，
stderr 仅输出安全结构化通知日志或固定失败消息。不得在这里接入默认 API/worker runtime。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.composition.opportunity_intake import (
    create_opportunity_from_validated_need,
)
from apps.api.identity import RequestIdentity
from domains.employees.permissions import (
    Actor as EmployeeActor,
)
from domains.employees.permissions import (
    EmployeeAction,
    EmployeeScope,
)
from domains.employees.service import EmployeeService
from domains.employees.service_impl import EmployeeServiceImpl
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityAction,
    OpportunityScope,
    ScopeLevel,
    StandardAuditLogger,
)
from domains.opportunities.schemas import (
    HandoffCreateRequest,
    OpportunityCreateRequest,
    OpportunityView,
    ValidatedNeedEvidence,
)
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service import LossReason, OpportunityState
from domains.opportunities.service_impl import (
    HandoffPolicy,
    OpportunityServiceImpl,
    OpportunityUnitOfWork,
)
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.session import create_engine_from
from infra.db.tables import (
    EmployeeRow,
    HandoffEscalationRow,
    NotificationDeliveryRow,
    OutboxEventRow,
    TerritoryAssignmentRow,
    WorkflowRunRow,
)
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from notification_gateway.channels.structured_log import StructuredLogChannel
from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import (
    Notification,
    NotificationChannel,
    NotificationPriority,
)
from notification_gateway.router import NotificationRouter
from shared.errors import PermissionDenied, PolicyViolation
from shared.schemas.evidence import EvidenceItem, EvidenceLevel, derive_confidence
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType
from workflows.human_handoff.flow import (
    HandoffEscalationNotice,
    build_human_handoff_step_handlers,
    register_human_handoff,
)

_USD = CurrencyCode("USD")
_DEMO_VERBATIM = "演示客户原话不得输出"
_DEMO_EVIDENCE_URL = "https://evidence.invalid/s3-20-demo"


@dataclass(frozen=True)
class _DemoEmployees:
    """一次演示独有的员工 ID；employees.employee_id 是全局主键。"""

    operator: EmployeeId
    manager: EmployeeId
    sales_a: EmployeeId
    sales_b: EmployeeId

    @classmethod
    def create(cls) -> _DemoEmployees:
        return cls(
            operator=EmployeeId(new_id("emp")),
            manager=EmployeeId(new_id("emp")),
            sales_a=EmployeeId(new_id("emp")),
            sales_b=EmployeeId(new_id("emp")),
        )

    def summary(self) -> dict[str, str]:
        return {
            "operator": str(self.operator),
            "manager": str(self.manager),
            "sales_a": str(self.sales_a),
            "sales_b": str(self.sales_b),
        }


class _Clock:
    """演示用 UTC aware 时钟；只靠显式推进，不使用 sleep。"""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, seconds: int) -> None:
        self._value += timedelta(seconds=seconds)


class _DemoOpportunityAuthorizer:
    """演示最小授权：精确 actor/action/scope/tenant 匹配，其他全部拒绝。"""

    def __init__(self, tenant_id: TenantId, employees: _DemoEmployees) -> None:
        self._tenant_id = tenant_id
        self._operator_id = employees.operator
        self._operator_scope = OpportunityScope(level=ScopeLevel.TENANT)
        self._system_scope = OpportunityScope(level=ScopeLevel.SYSTEM)

    def require(
        self,
        actor: OpportunityActor,
        action: OpportunityAction,
        scope: OpportunityScope,
        tenant_id: TenantId,
    ) -> str:
        operator_actions = {
            OpportunityAction.OPPORTUNITY_CREATE,
            OpportunityAction.OPPORTUNITY_ASSIGN,
            OpportunityAction.OPPORTUNITY_MARK_LOST,
            OpportunityAction.OPPORTUNITY_READ,
            OpportunityAction.OPPORTUNITY_TRANSITION,
            OpportunityAction.HANDOFF_REQUEST,
            OpportunityAction.HANDOFF_QUEUE_READ,
        }
        is_operator = (
            actor.actor_id == str(self._operator_id)
            and actor.role == "boss"
            and actor.scope == self._operator_scope
            and scope == self._operator_scope
            and action in operator_actions
        )
        is_system = (
            actor.actor_id == "system:demo-handoff"
            and actor.role == "system"
            and actor.scope == self._system_scope
            and scope == self._system_scope
            and action is OpportunityAction.HANDOFF_ESCALATION_RECORD
        )
        if tenant_id != self._tenant_id or not (is_operator or is_system):
            raise PermissionDenied("演示授权拒绝")
        return "demo:explicit-allow"


class _DemoEmployeeAuthorizer:
    """演示员工服务的最小授权；flow 只可读员工，operator 只可创建归属锁。"""

    def __init__(self, tenant_id: TenantId, employees: _DemoEmployees) -> None:
        self._tenant_id = tenant_id
        self._operator_id = employees.operator

    def require(
        self,
        actor: EmployeeActor,
        action: EmployeeAction,
        scope: EmployeeScope,
        tenant_id: TenantId,
    ) -> str:
        is_operator = (
            actor.actor_id == str(self._operator_id)
            and actor.role == "boss"
            and actor.scope is EmployeeScope.TENANT
            and scope is EmployeeScope.TENANT
            and action is EmployeeAction.OWNERSHIP_LOCK
        )
        is_system = (
            actor.actor_id == "system:demo-handoff"
            and actor.role == "system"
            and actor.scope is EmployeeScope.SYSTEM
            and scope is EmployeeScope.SYSTEM
            and action is EmployeeAction.EMPLOYEE_READ
        )
        if tenant_id != self._tenant_id or not (is_operator or is_system):
            raise PermissionDenied("演示授权拒绝")
        return "demo:explicit-allow"


class _StructuredLogOnlyPolicy:
    """演示通知只允许唯一的 structured-log channel，缺失即拒绝。"""

    def channels_for(
        self,
        notification: Notification,
        available: list[NotificationChannel],
    ) -> list[NotificationChannel]:
        del notification
        if len(available) != 1 or available[0].name != "structured_log":
            raise PolicyViolation("演示通知渠道未配置")
        return list(available)


class _DemoHandoffNotifier:
    """把 workflow 的窄 notice 转换为 public Notification，再交给真实 router。"""

    def __init__(self, router: NotificationRouter) -> None:
        self._router = router

    async def notify(self, notice: HandoffEscalationNotice) -> None:
        await self._router.dispatch(
            Notification(
                tenant_id=notice.tenant_id,
                recipient=notice.recipient_id,
                priority=NotificationPriority.URGENT,
                title="演示人工接管提醒",
                context=NotificationContext(
                    NotificationKind.HANDOFF_ESCALATION,
                    str(notice.handoff_id),
                    None,
                    reason_code=notice.level,
                    level=None,
                ),
                source_event="HandoffRequested",
                dedup_key=notice.dedup_key,
                next_step="处理演示接管任务",
                due_at=notice.sla_due_at,
                link=f"/crm/handoffs/{notice.handoff_id}",
            )
        )


class _FlowEmployeeReader:
    """为 workflow 的每次员工读取建立真实 request-scoped EmployeeService。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        clock: _Clock,
        authorizer: _DemoEmployeeAuthorizer,
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id
        self._clock = clock
        self._authorizer = authorizer

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ):
        if tenant_id != self._tenant_id:
            raise PermissionDenied("演示租户拒绝")
        async with _employee_service_scope(
            self._factory,
            tenant_id,
            clock=self._clock,
            authorizer=self._authorizer,
        ) as employees:
            return await employees.get_employee(tenant_id, employee_id, actor=actor)


class _SafeNotificationFormatter(logging.Formatter):
    """仅序列化 allowlist 结构化字段，拒绝把 LogRecord 的异常/业务内容带到 stderr。"""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "logger": record.name,
                "message": "通知投递（structured_log）",
                "tenant_id": str(getattr(record, "tenant_id", "")),
                "recipient": str(getattr(record, "recipient", "")),
                "priority": str(getattr(record, "priority", "")),
                "dedup_key": str(getattr(record, "dedup_key", "")),
                "source_event": str(getattr(record, "source_event", "")),
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _configure_safe_logging() -> None:
    """屏蔽其他库日志；仅让 StructuredLogChannel 写安全 JSON 到 stderr。"""
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.CRITICAL)

    logger = logging.getLogger("notification_gateway.channels.structured_log")
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_SafeNotificationFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def _employee_service_scope(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    clock: _Clock,
    authorizer: _DemoEmployeeAuthorizer,
) -> AsyncIterator[EmployeeService]:
    """每次使用独立 session 的员工服务，明确提交/回滚/关闭。"""
    session = factory()
    try:
        employees = EmployeeRepositoryImpl(session, tenant_id)
        yield EmployeeServiceImpl(
            employees=employees,
            territories=TerritoryRepositoryImpl(session, tenant_id),
            ownership=OwnershipRepositoryImpl(session, tenant_id),
            now=clock.now,
            manager_pool=lambda _: (),
            count_active_accounts=employees.count_active_accounts,
            authorizer=authorizer,
            audit=StandardAuditLogger(),
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _seed_employees_and_territories(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    *,
    clock: _Clock,
    employees_for_run: _DemoEmployees,
) -> None:
    """在真实事务中写演示员工/Territory fixture，供公开员工服务解析归属。"""
    session = factory()
    try:
        for employee in (
            EmployeeRow(
                employee_id=str(employees_for_run.operator),
                tenant_id=str(tenant_id),
                name="演示老板",
                role="boss",
                created_at=clock.now(),
            ),
            EmployeeRow(
                employee_id=str(employees_for_run.manager),
                tenant_id=str(tenant_id),
                name="演示经理",
                role="manager",
                created_at=clock.now(),
                manager_id=str(employees_for_run.operator),
            ),
            EmployeeRow(
                employee_id=str(employees_for_run.sales_a),
                tenant_id=str(tenant_id),
                name="演示销售A",
                role="sales",
                created_at=clock.now(),
                manager_id=str(employees_for_run.manager),
            ),
            EmployeeRow(
                employee_id=str(employees_for_run.sales_b),
                tenant_id=str(tenant_id),
                name="演示销售B",
                role="sales",
                created_at=clock.now(),
                manager_id=str(employees_for_run.manager),
            ),
        ):
            session.add(employee)
        await session.flush()

        for territory in (
            TerritoryAssignmentRow(
                assignment_id=new_id("ter"),
                tenant_id=str(tenant_id),
                employee_id=str(employees_for_run.sales_a),
                priority=1,
                effective_from=clock.now(),
                countries=["US", "DE", "JP"],
                need_categories=["hinges"],
            ),
            TerritoryAssignmentRow(
                assignment_id=new_id("ter"),
                tenant_id=str(tenant_id),
                employee_id=str(employees_for_run.sales_b),
                priority=1,
                effective_from=clock.now(),
                countries=["CA", "GB"],
                need_categories=["hinges"],
            ),
        ):
            session.add(territory)
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


def _provenance(source_id: str, clock: _Clock) -> Provenance:
    """演示事实均来自 conversation，避免把推断写入机会或接管包。"""
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=source_id,
        extracted_by="demo-operator",
        extracted_at=clock.now(),
    )


def _evidence_tier(source_id: str, clock: _Clock) -> str:
    """从与 ValidatedNeedEvidence 同源的事实经公共规则推导档位。"""
    evidence = EvidenceItem(
        level=EvidenceLevel.CUSTOMER_INTEREST_REPLY,
        source_type=SourceType.CONVERSATION.value,
        source_id=source_id,
        observed_at=clock.now(),
        summary="演示客户确认需求",
    )
    return derive_confidence([evidence], now=clock.now()).tier.value


def _opportunity_request(index: int, country: str, clock: _Clock) -> OpportunityCreateRequest:
    """构造一条可通过全部硬门槛的、带关键字段来源的演示 Validated Need 输入。"""
    source_id = f"demo-message-{index}"
    return OpportunityCreateRequest(
        need_id=new_id("need"),
        account_id=new_id("acc"),
        account_name=f"演示企业{index}",
        country=country,
        product_category="hinges",
        evidence_tier=_evidence_tier(source_id, clock),
        has_verified_contact=True,
        category_allowed=True,
        minimum_order_value=Money(Decimal(100), _USD),
        estimated_order_value=Money(Decimal(1500), _USD),
        supply_available=True,
        field_provenance={
            "account_name": _provenance(source_id, clock),
            "country": _provenance(source_id, clock),
        },
    )


def _handoff_request(
    opportunity: OpportunityView, source_id: str, clock: _Clock
) -> HandoffCreateRequest:
    """构造完整接管包；敏感客户原话/证据 URL 只入库，禁止传给通知或 summary。"""
    return HandoffCreateRequest(
        opportunity_id=opportunity.opportunity_id,
        trigger="quote_requested",
        account_name=opportunity.account_name,
        country=opportunity.country,
        why_valuable="演示：客户已确认需求且等待人工处理",
        customer_verbatim=_DEMO_VERBATIM,
        customer_verbatim_provenance=_provenance(source_id, clock),
        validated_need_summary="演示已验证需求",
        missing_information=["finish"],
        suggested_next_step="人工确认规格",
        evidence_links=[_DEMO_EVIDENCE_URL],
    )


async def main() -> int:
    """运行真实演示并输出单行 JSON summary；连接串只从环境读取且最终释放 engine。"""
    database_url = os.environ["DATABASE_URL"]
    _configure_safe_logging()
    engine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        tenant_id = TenantId(new_id("tn"))
        clock = _Clock(datetime(2026, 8, 9, tzinfo=UTC))
        employees_for_run = _DemoEmployees.create()
        opportunity_authorizer = _DemoOpportunityAuthorizer(tenant_id, employees_for_run)
        employee_authorizer = _DemoEmployeeAuthorizer(tenant_id, employees_for_run)
        operator_opportunity_actor = OpportunityActor(
            str(employees_for_run.operator),
            OpportunityScope(level=ScopeLevel.TENANT),
            "boss",
        )
        operator_employee_actor = EmployeeActor(
            str(employees_for_run.operator), EmployeeScope.TENANT, "boss"
        )
        system_opportunity_actor = OpportunityActor(
            "system:demo-handoff",
            OpportunityScope(level=ScopeLevel.SYSTEM),
            "system",
        )
        system_employee_actor = EmployeeActor(
            "system:demo-handoff", EmployeeScope.SYSTEM, "system"
        )

        await _seed_employees_and_territories(
            factory,
            tenant_id,
            clock=clock,
            employees_for_run=employees_for_run,
        )

        @asynccontextmanager
        async def employee_services(
            scope_tenant: TenantId,
        ) -> AsyncIterator[EmployeeService]:
            async with _employee_service_scope(
                factory,
                scope_tenant,
                clock=clock,
                authorizer=employee_authorizer,
            ) as service:
                yield service

        async with _employee_service_scope(
            factory,
            tenant_id,
            clock=clock,
            authorizer=employee_authorizer,
        ) as employees:
            operator_view = await employees.get_employee(
                tenant_id, employees_for_run.operator, actor=system_employee_actor
            )

        identity = RequestIdentity(
            tenant_id=tenant_id,
            employee=operator_view,
            employee_actor=operator_employee_actor,
            opportunity_actor=operator_opportunity_actor,
        )
        service = OpportunityServiceImpl(
            cast(
                Callable[[], OpportunityUnitOfWork],
                lambda: SqlAlchemyOpportunityUnitOfWork(factory, tenant_id),
            ),
            OpportunityScorerImpl(
                ScoringPolicy(
                    version="s3-20-demo",
                    value_band_boundaries=(
                        Money(Decimal(100), _USD),
                        Money(Decimal(1000), _USD),
                    ),
                    bucket_map={rank: "high" for rank in range(1, 8)},
                )
            ),
            HandoffPolicy(sla_seconds=10, backlog_threshold=10),
            authorizer=opportunity_authorizer,
            audit=StandardAuditLogger(),
            now=clock.now,
        )

        opportunities_for_handoff: list[OpportunityView] = []
        opportunity_ids: list[str] = []
        for index, country in enumerate(("US", "CA", "DE", "GB", "JP"), start=1):
            created = await create_opportunity_from_validated_need(
                opportunities=service,
                employee_services=employee_services,
                identity=identity,
                request=_opportunity_request(index, country, clock),
                evidence=ValidatedNeedEvidence(
                    level=EvidenceLevel.CUSTOMER_INTEREST_REPLY,
                    provenance=_provenance(f"demo-message-{index}", clock),
                ),
            )
            if created is None:
                raise RuntimeError("演示机会未通过硬门槛")
            opportunity_ids.append(created.opportunity_id)
            opportunities_for_handoff.append(created)

        lost_opportunity_id = opportunity_ids[-1]
        await service.transition(
            tenant_id,
            lost_opportunity_id,
            OpportunityState.ASSIGNED,
            actor=operator_opportunity_actor,
        )
        await service.mark_lost(
            tenant_id,
            lost_opportunity_id,
            LossReason.PRICE_TOO_HIGH,
            actor=operator_opportunity_actor,
            confirmed_by=employees_for_run.operator,
            confirmed_at=clock.now(),
        )
        async with SqlAlchemyOpportunityUnitOfWork(factory, tenant_id) as uow:
            lost_opportunity = await uow.opportunities.get(tenant_id, lost_opportunity_id)
            if (
                lost_opportunity is None
                or lost_opportunity.state.value != "lost"
                or lost_opportunity.loss_reason is not LossReason.PRICE_TOO_HIGH
                or lost_opportunity.died_at_state is None
                or lost_opportunity.closed_by != employees_for_run.operator
                or lost_opportunity.closed_at is None
            ):
                raise RuntimeError("演示机会终态读回不完整")

        oldest_handoff_id = await service.request_handoff(
            tenant_id,
            _handoff_request(opportunities_for_handoff[0], "demo-message-1", clock),
            actor=operator_opportunity_actor,
        )
        clock.advance(10)
        second_handoff_id = await service.request_handoff(
            tenant_id,
            _handoff_request(opportunities_for_handoff[1], "demo-message-2", clock),
            actor=operator_opportunity_actor,
        )
        queue = await service.list_pending_handoffs(
            tenant_id, operator_opportunity_actor, limit=10
        )
        queue_order = [item.handoff_id for item in queue]
        if queue_order != [str(oldest_handoff_id), str(second_handoff_id)]:
            raise RuntimeError("演示接管队列顺序不正确")

        router = NotificationRouter(
            PostgresNotificationDedupStore(factory, now=clock.now),
            _StructuredLogOnlyPolicy(),
        )
        router.register_channel(StructuredLogChannel())
        engine_handlers = build_human_handoff_step_handlers(
            opportunity_service=service,
            employee_service=cast(EmployeeService, _FlowEmployeeReader(
                factory,
                tenant_id,
                clock,
                employee_authorizer,
            )),
            notifier=_DemoHandoffNotifier(router),
            opportunity_system_actor=system_opportunity_actor,
            employee_system_actor=system_employee_actor,
            t1=timedelta(seconds=5),
            t2=timedelta(seconds=1),
            now=clock.now,
        )
        workflow_engine = PostgresWorkflowEngine(factory, engine_handlers, now=clock.now)
        outbox = OutboxDeliverer(factory, tenant_id, now=clock.now)
        register_human_handoff(
            workflow_engine,
            outbox,
            t1=timedelta(seconds=5),
            t2=timedelta(seconds=1),
        )
        await outbox.drain()
        for _ in range(5):
            if await workflow_engine.poll_due(tenant_id, 1) != 1:
                raise RuntimeError("演示工作流未按预期推进")

        async with factory() as session:
            levels = (
                await session.execute(
                    select(HandoffEscalationRow.level)
                    .where(
                        HandoffEscalationRow.tenant_id == str(tenant_id),
                        HandoffEscalationRow.handoff_id == str(oldest_handoff_id),
                    )
                    .order_by(HandoffEscalationRow.level.asc())
                )
            ).scalars().all()
            second_levels = (
                await session.execute(
                    select(HandoffEscalationRow.escalation_id).where(
                        HandoffEscalationRow.tenant_id == str(tenant_id),
                        HandoffEscalationRow.handoff_id == str(second_handoff_id),
                    )
                )
            ).scalars().all()
            target_run = (
                await session.execute(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.tenant_id == str(tenant_id),
                        WorkflowRunRow.subject_ref == str(oldest_handoff_id),
                    )
                )
            ).scalar_one_or_none()
            requested_events = (
                await session.execute(
                    select(OutboxEventRow).where(
                        OutboxEventRow.tenant_id == str(tenant_id),
                        OutboxEventRow.event_type == "HandoffRequested",
                    )
                )
            ).scalars().all()
            notification_rows = (
                await session.execute(
                    select(NotificationDeliveryRow).where(
                        NotificationDeliveryRow.tenant_id == str(tenant_id),
                        NotificationDeliveryRow.dedup_key.in_(
                            [
                                f"human_handoff:{oldest_handoff_id}:owner",
                                f"human_handoff:{oldest_handoff_id}:manager",
                                f"human_handoff:{oldest_handoff_id}:boss",
                            ]
                        ),
                    )
                )
            ).scalars().all()
        if (
            levels != [1, 2]
            or second_levels
            or target_run is None
            or target_run.status != "running"
            or target_run.current_step != "wait_acceptance_boss"
            or len(requested_events) != 2
            or any(event.status != "delivered" for event in requested_events)
            or len(notification_rows) != 3
            or any(
                row.channel_name != "structured_log" or row.status != "delivered"
                for row in notification_rows
            )
        ):
            raise RuntimeError("演示工作流读回不完整")

        print(
            json.dumps(
                {
                    "tenant_id": str(tenant_id),
                    "employee_ids": employees_for_run.summary(),
                    "opportunity_ids": opportunity_ids,
                    "lost": {
                        "opportunity_id": str(lost_opportunity.opportunity_id),
                        "state": lost_opportunity.state.value,
                        "loss_reason": lost_opportunity.loss_reason.value,
                        "died_at_state": lost_opportunity.died_at_state.value,
                    },
                    "handoff_queue": queue_order,
                    "target_handoff_id": str(oldest_handoff_id),
                    "escalation_levels": levels,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


def _run() -> int:
    """进程边界只输出固定失败消息，不让 traceback/DSN/异常文本泄露。"""
    try:
        return asyncio.run(main())
    except Exception:  # noqa: BLE001 - 进程边界必须脱敏
        print("演示运行失败", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_run())
