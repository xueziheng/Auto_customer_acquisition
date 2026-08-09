"""人工接管 SLA 流程：通知负责人，按绝对 T1/T2 边界逐级升级。"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from domains.employees.schemas import EmployeeView
from domains.employees.service import Actor as EmployeeActor
from domains.employees.service import EmployeeService
from domains.opportunities.service import Actor as OpportunityActor
from domains.opportunities.service import OpportunityService
from shared.errors import TransientError, ValidationError
from shared.events.bus import EventHandler
from shared.events.catalog import DomainEvent, HandoffAccepted, HandoffRequested
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    RunId,
    TenantId,
)
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowRun,
)

HUMAN_HANDOFF_WORKFLOW_TYPE = "human_handoff"


@dataclass(frozen=True)
class HandoffEscalationNotice:
    """交给通知出口的最小内部 DTO；不耦合具体通知网关。"""

    tenant_id: TenantId
    handoff_id: HandoffId
    opportunity_id: OpportunityId
    recipient_id: EmployeeId
    assigned_to: EmployeeId
    level: str
    sla_started_at: datetime
    sla_due_at: datetime
    dedup_key: str


class HandoffEscalationNotifier(Protocol):
    async def notify(self, notice: HandoffEscalationNotice) -> None: ...


class OutboxHandlerRegistry(Protocol):
    def register_handler(
        self,
        event_type: type[DomainEvent],
        handler_name: str,
        handler: EventHandler[DomainEvent],
    ) -> None: ...


def _validate_sla(t1: timedelta, t2: timedelta) -> None:
    if t1.total_seconds() <= 0 or t2.total_seconds() <= 0:
        raise ValidationError("human_handoff T1/T2 必须为正时长")


def build_human_handoff_definition(
    t1: timedelta, t2: timedelta
) -> WorkflowDefinition:
    """构建显式六步流程；SLA 必须由 composition root 注入。"""
    _validate_sla(t1, t2)
    return WorkflowDefinition(
        workflow_type=HUMAN_HANDOFF_WORKFLOW_TYPE,
        version=1,
        steps=(
            StepDefinition("notify_owner", "human_handoff.notify_owner"),
            StepDefinition(
                "wait_acceptance_t1",
                "human_handoff.accept",
                timeout=t1,
                on_timeout="escalate_manager",
                wait_event_type="HandoffAccepted",
            ),
            StepDefinition(
                "escalate_manager", "human_handoff.escalate_manager"
            ),
            StepDefinition(
                "wait_acceptance_t2",
                "human_handoff.accept",
                timeout=t2,
                on_timeout="escalate_boss",
                wait_event_type="HandoffAccepted",
            ),
            StepDefinition("escalate_boss", "human_handoff.escalate_boss"),
            StepDefinition(
                "wait_acceptance_boss",
                "human_handoff.accept",
                wait_event_type="HandoffAccepted",
                reminder_interval=t2,
                reminder_handler_ref="human_handoff.remind_boss",
            ),
        ),
        transitions={
            "notify_owner": ("wait_acceptance_t1",),
            "wait_acceptance_t1": ("escalate_manager",),
            "escalate_manager": ("wait_acceptance_t2",),
            "wait_acceptance_t2": ("escalate_boss",),
            "escalate_boss": ("wait_acceptance_boss",),
        },
    )


@dataclass(frozen=True)
class _HandoffContext:
    tenant_id: TenantId
    handoff_id: HandoffId
    opportunity_id: OpportunityId
    assigned_to: EmployeeId
    sla_started_at: datetime


def _context(run: WorkflowRun) -> _HandoffContext:
    try:
        handoff = str(run.context["handoff_id"])
        opportunity = str(run.context["opportunity_id"])
        assigned = str(run.context["assigned_to"])
        started = datetime.fromisoformat(str(run.context["sla_started_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("human_handoff context invalid") from exc
    if not handoff or not opportunity or not assigned or started.utcoffset() is None:
        raise ValidationError("human_handoff context invalid")
    if handoff != run.subject_ref:
        raise ValidationError("human_handoff subject mismatch")
    return _HandoffContext(
        run.tenant_id,
        HandoffId(handoff),
        OpportunityId(opportunity),
        EmployeeId(assigned),
        started,
    )


class _NotifyOwnerStep:
    def __init__(
        self, notifier: HandoffEscalationNotifier, t1: timedelta
    ) -> None:
        self._notifier = notifier
        self._t1 = t1

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]:
        ctx = _context(run)
        await self._notifier.notify(
            _notice(ctx, ctx.assigned_to, "owner", ctx.sla_started_at + self._t1)
        )
        return ("advance", "wait_acceptance_t1", {})


class _AcceptStep:
    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]:
        ctx = _context(run)
        event = run.context.get("event")
        if not isinstance(event, dict) or event.get("event_type") != "HandoffAccepted":
            raise ValidationError("human_handoff acceptance event invalid")
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("handoff_id") != str(
            ctx.handoff_id
        ):
            raise ValidationError("human_handoff acceptance event invalid")
        return ("complete", None, {})


class _EscalateManagerStep:
    def __init__(
        self,
        opportunity_service: OpportunityService,
        employee_service: EmployeeService,
        notifier: HandoffEscalationNotifier,
        opportunity_actor: OpportunityActor,
        employee_actor: EmployeeActor,
        t1: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        self._opportunities = opportunity_service
        self._employees = employee_service
        self._notifier = notifier
        self._opportunity_actor = opportunity_actor
        self._employee_actor = employee_actor
        self._t1 = t1
        self._now = now

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]:
        ctx = _context(run)
        owner = await _employee(
            self._employees, run.tenant_id, ctx.assigned_to, self._employee_actor
        )
        if owner.manager_id is None:
            raise ValidationError("human_handoff manager unavailable")
        manager = await _employee(
            self._employees, run.tenant_id, owner.manager_id, self._employee_actor
        )
        _require_role(manager, "manager")
        await self._opportunities.record_handoff_escalation(
            run.tenant_id,
            ctx.handoff_id,
            1,
            self._now(),
            actor=self._opportunity_actor,
        )
        await self._notifier.notify(
            _notice(
                ctx,
                manager.employee_id,
                "manager",
                ctx.sla_started_at + self._t1,
            )
        )
        return ("advance", "wait_acceptance_t2", {})


class _EscalateBossStep:
    def __init__(
        self,
        opportunity_service: OpportunityService,
        employee_service: EmployeeService,
        notifier: HandoffEscalationNotifier,
        opportunity_actor: OpportunityActor,
        employee_actor: EmployeeActor,
        t1: timedelta,
        t2: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        self._opportunities = opportunity_service
        self._employees = employee_service
        self._notifier = notifier
        self._opportunity_actor = opportunity_actor
        self._employee_actor = employee_actor
        self._t1 = t1
        self._t2 = t2
        self._now = now

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]:
        ctx = _context(run)
        boss = await _resolve_boss(
            self._employees,
            run.tenant_id,
            ctx.assigned_to,
            self._employee_actor,
        )
        await self._opportunities.record_handoff_escalation(
            run.tenant_id,
            ctx.handoff_id,
            2,
            self._now(),
            actor=self._opportunity_actor,
        )
        await self._notifier.notify(
            _notice(
                ctx,
                boss.employee_id,
                "boss",
                ctx.sla_started_at + self._t1 + self._t2,
            )
        )
        return ("advance", "wait_acceptance_boss", {})


class _RemindBossStep:
    def __init__(
        self,
        employee_service: EmployeeService,
        notifier: HandoffEscalationNotifier,
        employee_actor: EmployeeActor,
    ) -> None:
        self._employees = employee_service
        self._notifier = notifier
        self._employee_actor = employee_actor

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]:
        ctx = _context(run)
        reminder = run.reminder
        if reminder is None or reminder.index < 1:
            raise ValidationError("human_handoff reminder context invalid")
        boss = await _resolve_boss(
            self._employees, run.tenant_id, ctx.assigned_to, self._employee_actor
        )
        await self._notifier.notify(
            _notice(
                ctx,
                boss.employee_id,
                "boss_reminder",
                reminder.scheduled_at,
                dedup_key=(
                    f"human_handoff:{ctx.handoff_id}:boss-reminder:{reminder.index}"
                ),
            )
        )
        return ("wait", None, {})


async def _employee(
    service: EmployeeService,
    tenant_id: TenantId,
    employee_id: EmployeeId,
    actor: EmployeeActor,
) -> EmployeeView:
    employee = await service.get_employee(tenant_id, employee_id, actor=actor)
    if employee.tenant_id != tenant_id or not employee.is_active:
        raise ValidationError("human_handoff employee unavailable")
    return employee


async def _resolve_boss(
    service: EmployeeService,
    tenant_id: TenantId,
    assigned_to: EmployeeId,
    actor: EmployeeActor,
) -> EmployeeView:
    owner = await _employee(service, tenant_id, assigned_to, actor)
    if owner.manager_id is None:
        raise ValidationError("human_handoff manager unavailable")
    manager = await _employee(service, tenant_id, owner.manager_id, actor)
    _require_role(manager, "manager")
    if manager.manager_id is None:
        raise ValidationError("human_handoff boss unavailable")
    boss = await _employee(service, tenant_id, manager.manager_id, actor)
    _require_role(boss, "boss")
    return boss


def _require_role(employee: EmployeeView, role: str) -> None:
    if not employee.is_active or employee.role != role:
        raise ValidationError("human_handoff escalation role unavailable")


def _notice(
    ctx: _HandoffContext,
    recipient: EmployeeId,
    level: str,
    due_at: datetime,
    *,
    dedup_key: str | None = None,
) -> HandoffEscalationNotice:
    return HandoffEscalationNotice(
        tenant_id=ctx.tenant_id,
        handoff_id=ctx.handoff_id,
        opportunity_id=ctx.opportunity_id,
        recipient_id=recipient,
        assigned_to=ctx.assigned_to,
        level=level,
        sla_started_at=ctx.sla_started_at,
        sla_due_at=due_at,
        dedup_key=dedup_key or f"human_handoff:{ctx.handoff_id}:{level}",
    )


def build_human_handoff_step_handlers(
    *,
    opportunity_service: OpportunityService,
    employee_service: EmployeeService,
    notifier: HandoffEscalationNotifier,
    opportunity_system_actor: OpportunityActor,
    employee_system_actor: EmployeeActor,
    t1: timedelta,
    t2: timedelta,
    now: Callable[[], datetime],
) -> Mapping[str, StepHandler]:
    """装配步骤 handler；actor、SLA 与时钟均由 composition root 显式注入。"""
    _validate_sla(t1, t2)
    return {
        "human_handoff.notify_owner": _NotifyOwnerStep(notifier, t1),
        "human_handoff.accept": _AcceptStep(),
        "human_handoff.escalate_manager": _EscalateManagerStep(
            opportunity_service,
            employee_service,
            notifier,
            opportunity_system_actor,
            employee_system_actor,
            t1,
            now,
        ),
        "human_handoff.escalate_boss": _EscalateBossStep(
            opportunity_service,
            employee_service,
            notifier,
            opportunity_system_actor,
            employee_system_actor,
            t1,
            t2,
            now,
        ),
        "human_handoff.remind_boss": _RemindBossStep(
            employee_service,
            notifier,
            employee_system_actor,
        ),
    }


class _RequestedHandler:
    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def handle(self, event: HandoffRequested) -> None:
        if event.assigned_to is None:
            raise ValidationError("human_handoff requires assigned owner")
        if event.opportunity_id is None:
            raise ValidationError("human_handoff requires opportunity")
        tenant = str(event.tenant_id)
        handoff = str(event.handoff_id)
        await self._engine.start(
            event.tenant_id,
            HUMAN_HANDOFF_WORKFLOW_TYPE,
            handoff,
            {
                "handoff_id": handoff,
                "opportunity_id": str(event.opportunity_id),
                "assigned_to": str(event.assigned_to),
                "sla_started_at": event.occurred_at.isoformat(),
            },
            f"{tenant}:{HUMAN_HANDOFF_WORKFLOW_TYPE}:{handoff}",
            scheduled_at=event.occurred_at,
        )


class _AcceptedHandler:
    def __init__(self, engine: WorkflowEngine) -> None:
        self._engine = engine

    async def handle(self, event: HandoffAccepted) -> None:
        if event.accepted_by is None:
            raise ValidationError("human_handoff acceptance requires employee")
        payload = {
            "handoff_id": str(event.handoff_id),
            "accepted_by": str(event.accepted_by),
        }
        run_id: RunId | None = await self._engine.find_active_run(
            event.tenant_id,
            HUMAN_HANDOFF_WORKFLOW_TYPE,
            str(event.handoff_id),
        )
        if run_id is None:
            if await self._engine.has_delivered_event(
                event.tenant_id,
                HUMAN_HANDOFF_WORKFLOW_TYPE,
                str(event.handoff_id),
                "HandoffAccepted",
                payload,
            ):
                return
            raise TransientError(
                "human_handoff active run temporarily unavailable"
            )
        accepted = await self._engine.deliver_event(
            event.tenant_id,
            run_id,
            "HandoffAccepted",
            payload,
        )
        if accepted:
            return
        raise TransientError("human_handoff active run temporarily unavailable")


def register_human_handoff(
    engine: WorkflowEngine,
    registry: OutboxHandlerRegistry,
    *,
    t1: timedelta,
    t2: timedelta,
) -> None:
    """注册流程定义与两个 durable outbox 事件入口。"""
    engine.register(build_human_handoff_definition(t1, t2))
    registry.register_handler(
        HandoffRequested,
        "human_handoff.requested",
        _RequestedHandler(engine),  # type: ignore[arg-type]
    )
    registry.register_handler(
        HandoffAccepted,
        "human_handoff.accepted",
        _AcceptedHandler(engine),  # type: ignore[arg-type]
    )
