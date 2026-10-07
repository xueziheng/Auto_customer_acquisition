"""S3-10 human_handoff 流程单测：定义、事件装配、升级解析与幂等。"""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.schemas import HandoffCreateRequest
from domains.opportunities.service_impl import OpportunityServiceImpl
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.events.catalog import HandoffAccepted, HandoffRequested
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    ProspectAccountId,
    RunId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import Provenance, SourceType
from workflows.engine.runner import WorkflowRun

_opportunity_models = importlib.import_module("domains.opportunities.models")
HandoffPolicy = _opportunity_models.HandoffPolicy
Opportunity = _opportunity_models.Opportunity

try:
    _flow = importlib.import_module("workflows.human_handoff.flow")
except ModuleNotFoundError:
    _flow = None


def _missing(*args, **kwargs):
    raise AssertionError("RED：workflows.human_handoff.flow 尚未实现")


HUMAN_HANDOFF_WORKFLOW_TYPE = (
    getattr(_flow, "HUMAN_HANDOFF_WORKFLOW_TYPE", "human_handoff")
    if _flow is not None
    else "human_handoff"
)
HandoffEscalationNotice = (
    getattr(_flow, "HandoffEscalationNotice", object) if _flow is not None else object
)
HumanHandoffEmployeeReader = (
    getattr(_flow, "HumanHandoffEmployeeReader", None) if _flow is not None else None
)
build_human_handoff_definition = (
    getattr(_flow, "build_human_handoff_definition", _missing)
    if _flow is not None
    else _missing
)
build_human_handoff_step_handlers = (
    getattr(_flow, "build_human_handoff_step_handlers", _missing)
    if _flow is not None
    else _missing
)
register_human_handoff = (
    getattr(_flow, "register_human_handoff", _missing)
    if _flow is not None
    else _missing
)

_REQUESTED_AT = datetime(2026, 8, 9, 1, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 8, 9, 3, 0, 0, tzinfo=UTC)
_T1 = timedelta(minutes=15)
_T2 = timedelta(minutes=30)
_TENANT = TenantId("tenant-flow")


def _opportunity_actor() -> OpportunityActor:
    return OpportunityActor(
        actor_id="system:human-handoff",
        scope=OpportunityScope(level=ScopeLevel.SYSTEM),
        role="system",
    )


def _employee_actor() -> EmployeeActor:
    return EmployeeActor(
        actor_id="system:human-handoff", scope=EmployeeScope.SYSTEM, role="system"
    )


def _requested() -> HandoffRequested:
    return HandoffRequested(
        tenant_id=_TENANT,
        occurred_at=_REQUESTED_AT,
        run_id=None,
        handoff_id=HandoffId("hand-1"),
        opportunity_id=OpportunityId("opp-1"),
        assigned_to=EmployeeId("sales-1"),
        trigger="quote_requested",
    )


def _accepted() -> HandoffAccepted:
    return HandoffAccepted(
        tenant_id=_TENANT,
        occurred_at=_NOW,
        run_id=None,
        handoff_id=HandoffId("hand-1"),
        accepted_by=EmployeeId("sales-1"),
    )


class _FakeEngine:
    def __init__(self) -> None:
        self.registered = []
        self.starts: list[dict[str, object]] = []
        self.active: RunId | None = None
        self.deliveries: list[tuple] = []
        self.delivered = False

    def register(self, definition) -> None:
        self.registered.append(definition)

    async def start(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        initial_context,
        idempotency_key,
        *,
        scheduled_at=None,
    ) -> RunId:
        self.starts.append(
            {
                "tenant_id": tenant_id,
                "workflow_type": workflow_type,
                "subject_ref": subject_ref,
                "initial_context": initial_context,
                "idempotency_key": idempotency_key,
                "scheduled_at": scheduled_at,
            }
        )
        if self.active is None:
            self.active = RunId("run-1")
        return self.active

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        return self.active

    async def deliver_event(self, tenant_id, run_id, event_type, payload) -> bool:
        self.deliveries.append((tenant_id, run_id, event_type, payload))
        self.active = None
        self.delivered = True
        return True

    async def has_delivered_event(
        self, tenant_id, workflow_type, subject_ref, event_type, payload
    ) -> bool:
        return self.delivered


class _FakeRegistry:
    def __init__(self) -> None:
        self.rows: list[tuple[type, str, object]] = []

    def register_handler(self, event_type, handler_name, handler) -> None:
        self.rows.append((event_type, handler_name, handler))


class _FakeOpportunityService:
    def __init__(self) -> None:
        self.escalations: list[tuple] = []

    async def record_handoff_escalation(
        self, tenant_id, handoff_id, level, at, *, actor
    ) -> None:
        self.escalations.append((tenant_id, handoff_id, level, at, actor))


class _FakeEmployeeService:
    def __init__(self, employees: list[EmployeeView]) -> None:
        self._employees = {e.employee_id: e for e in employees}
        self.calls: list[tuple] = []

    async def get_employee(self, tenant_id, employee_id, *, actor) -> EmployeeView:
        self.calls.append((tenant_id, employee_id, actor))
        if employee_id not in self._employees:
            raise ValidationError("employee unavailable")
        return self._employees[employee_id]


def test_handoff_employee_dependency_is_the_narrow_read_protocol() -> None:
    """流程只要求 get_employee；新增员工写能力不应扩大该依赖面。"""
    assert HumanHandoffEmployeeReader is not None
    reader = _FakeEmployeeService(_employees())
    assert isinstance(reader, HumanHandoffEmployeeReader)


class _FakeNotifier:
    def __init__(self) -> None:
        self.notices: list[HandoffEscalationNotice] = []

    async def notify(self, notice: HandoffEscalationNotice) -> None:
        if notice.dedup_key not in {n.dedup_key for n in self.notices}:
            self.notices.append(notice)


def _employees(*, manager_active: bool = True, manager_role: str = "manager"):
    return [
        EmployeeView(
            employee_id=EmployeeId("sales-1"),
            tenant_id=_TENANT,
            name="Sales",
            role="sales",
            manager_id=EmployeeId("manager-1"),
        ),
        EmployeeView(
            employee_id=EmployeeId("manager-1"),
            tenant_id=_TENANT,
            name="Manager",
            role=manager_role,
            manager_id=EmployeeId("boss-1"),
            is_active=manager_active,
        ),
        EmployeeView(
            employee_id=EmployeeId("boss-1"),
            tenant_id=_TENANT,
            name="Boss",
            role="boss",
        ),
    ]


def _run(step: str) -> WorkflowRun:
    return WorkflowRun(
        run_id=RunId("run-1"),
        tenant_id=_TENANT,
        workflow_type=HUMAN_HANDOFF_WORKFLOW_TYPE,
        workflow_version=1,
        subject_ref="hand-1",
        current_step=step,
        status="running",  # type: ignore[arg-type]
        created_at=_NOW,
        context={
            "handoff_id": "hand-1",
            "opportunity_id": "opp-1",
            "assigned_to": "sales-1",
            "sla_started_at": _REQUESTED_AT.isoformat(),
        },
    )


def test_definition_has_explicit_t1_t2_and_no_defaults() -> None:
    """变异点：删除步骤/写死 SLA/补默认值时失败。"""
    params = inspect.signature(build_human_handoff_definition).parameters
    assert params["t1"].default is inspect.Parameter.empty
    assert params["t2"].default is inspect.Parameter.empty
    definition = build_human_handoff_definition(_T1, _T2)
    assert definition.workflow_type == HUMAN_HANDOFF_WORKFLOW_TYPE
    assert [s.step_name for s in definition.steps] == [
        "notify_owner",
        "wait_acceptance_t1",
        "escalate_manager",
        "wait_acceptance_t2",
        "escalate_boss",
        "wait_acceptance_boss",
    ]
    by_name = {s.step_name: s for s in definition.steps}
    assert by_name["wait_acceptance_t1"].timeout == _T1
    assert by_name["wait_acceptance_t1"].on_timeout == "escalate_manager"
    assert by_name["wait_acceptance_t2"].timeout == _T2
    assert by_name["wait_acceptance_t2"].on_timeout == "escalate_boss"
    assert by_name["wait_acceptance_boss"].timeout is None
    assert by_name["wait_acceptance_boss"].reminder_interval == _T2
    assert (
        by_name["wait_acceptance_boss"].reminder_handler_ref
        == "human_handoff.remind_boss"
    )
    assert definition.transitions == {
        "notify_owner": ("wait_acceptance_t1",),
        "wait_acceptance_t1": ("escalate_manager",),
        "escalate_manager": ("wait_acceptance_t2",),
        "wait_acceptance_t2": ("escalate_boss",),
        "escalate_boss": ("wait_acceptance_boss",),
    }


@pytest.mark.parametrize(
    ("t1", "t2"),
    [
        (timedelta(0), _T2),
        (timedelta(seconds=-1), _T2),
        (_T1, timedelta(0)),
        (_T1, timedelta(seconds=-1)),
    ],
)
def test_definition_rejects_nonpositive_sla(t1: timedelta, t2: timedelta) -> None:
    with pytest.raises(ValidationError):
        build_human_handoff_definition(t1, t2)


async def test_requested_handler_uses_original_event_time_and_stable_key() -> None:
    """延迟消费不能重置 SLA；重复事件必须命中同一 start 幂等键。"""
    engine = _FakeEngine()
    registry = _FakeRegistry()
    register_human_handoff(engine, registry, t1=_T1, t2=_T2)
    requested_handler = next(row[2] for row in registry.rows if row[0] is HandoffRequested)

    await requested_handler.handle(_requested())
    await requested_handler.handle(_requested())

    assert len(engine.starts) == 2
    assert engine.starts[0]["subject_ref"] == "hand-1"
    assert engine.starts[0]["scheduled_at"] == _REQUESTED_AT
    assert engine.starts[0]["idempotency_key"] == (
        "tenant-flow:human_handoff:hand-1"
    )
    assert engine.starts[0]["initial_context"] == {
        "handoff_id": "hand-1",
        "opportunity_id": "opp-1",
        "assigned_to": "sales-1",
        "sla_started_at": _REQUESTED_AT.isoformat(),
    }


async def test_requested_handler_rejects_unassigned_handoff() -> None:
    engine = _FakeEngine()
    registry = _FakeRegistry()
    register_human_handoff(engine, registry, t1=_T1, t2=_T2)
    handler = next(row[2] for row in registry.rows if row[0] is HandoffRequested)
    event = HandoffRequested(
        tenant_id=_TENANT,
        occurred_at=_REQUESTED_AT,
        handoff_id=HandoffId("hand-no-owner"),
        opportunity_id=OpportunityId("opp-1"),
        assigned_to=None,
        trigger="quote_requested",
    )
    with pytest.raises(ValidationError):
        await handler.handle(event)
    assert engine.starts == []


async def test_accepted_handler_delivers_active_and_repeated_call_noops() -> None:
    engine = _FakeEngine()
    engine.active = RunId("run-1")
    registry = _FakeRegistry()
    register_human_handoff(engine, registry, t1=_T1, t2=_T2)
    handler = next(row[2] for row in registry.rows if row[0] is HandoffAccepted)

    await handler.handle(_accepted())
    await handler.handle(_accepted())

    assert engine.deliveries == [
        (
            _TENANT,
            RunId("run-1"),
            "HandoffAccepted",
            {"handoff_id": "hand-1", "accepted_by": "sales-1"},
        )
    ]


async def test_accepted_handler_missing_active_is_fixed_transient_error() -> None:
    engine = _FakeEngine()
    registry = _FakeRegistry()
    register_human_handoff(engine, registry, t1=_T1, t2=_T2)
    handler = next(row[2] for row in registry.rows if row[0] is HandoffAccepted)
    with pytest.raises(TransientError) as exc_info:
        await handler.handle(_accepted())
    assert str(exc_info.value) == "human_handoff active run temporarily unavailable"
    assert "hand-1" not in str(exc_info.value)


async def test_manager_and_boss_escalation_use_public_employee_chain() -> None:
    opportunities = _FakeOpportunityService()
    employees = _FakeEmployeeService(_employees())
    notifier = _FakeNotifier()
    handlers = build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=employees,
        notifier=notifier,
        opportunity_system_actor=_opportunity_actor(),
        employee_system_actor=_employee_actor(),
        t1=_T1,
        t2=_T2,
        now=lambda: _NOW,
    )

    assert await handlers["human_handoff.escalate_manager"].execute(
        _run("escalate_manager")
    ) == ("advance", "wait_acceptance_t2", {})
    assert await handlers["human_handoff.escalate_boss"].execute(
        _run("escalate_boss")
    ) == ("advance", "wait_acceptance_boss", {})

    assert [(row[2], row[1]) for row in opportunities.escalations] == [
        (1, HandoffId("hand-1")),
        (2, HandoffId("hand-1")),
    ]
    assert [notice.recipient_id for notice in notifier.notices] == [
        EmployeeId("manager-1"),
        EmployeeId("boss-1"),
    ]
    assert notifier.notices[0].sla_due_at == _REQUESTED_AT + _T1
    assert notifier.notices[1].sla_due_at == _REQUESTED_AT + _T1 + _T2
    assert all(n.assigned_to == EmployeeId("sales-1") for n in notifier.notices)
    assert [call[1] for call in employees.calls] == [
        EmployeeId("sales-1"),
        EmployeeId("manager-1"),
        EmployeeId("sales-1"),
        EmployeeId("manager-1"),
        EmployeeId("boss-1"),
    ]


@pytest.mark.parametrize(
    ("active", "role"), [(False, "manager"), (True, "sales")]
)
async def test_manager_resolution_fails_closed(active: bool, role: str) -> None:
    handlers = build_human_handoff_step_handlers(
        opportunity_service=_FakeOpportunityService(),
        employee_service=_FakeEmployeeService(
            _employees(manager_active=active, manager_role=role)
        ),
        notifier=_FakeNotifier(),
        opportunity_system_actor=_opportunity_actor(),
        employee_system_actor=_employee_actor(),
        t1=_T1,
        t2=_T2,
        now=lambda: _NOW,
    )
    with pytest.raises(ValidationError):
        await handlers["human_handoff.escalate_manager"].execute(
            _run("escalate_manager")
        )


class _AllowAuthorizer:
    def require(self, actor, action, scope, tenant_id) -> str:
        return "test:allow"


class _DenyAuthorizer:
    def require(self, actor, action, scope, tenant_id) -> str:
        raise PermissionDenied("deny")


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def log(self, **row) -> None:
        self.rows.append({k: str(v) for k, v in row.items()})


class _NeverScorer:
    async def score(self, *args, **kwargs):
        raise AssertionError("unused")


class _RequestRepo:
    def __init__(self, opportunity: Opportunity) -> None:
        self.opportunity = opportunity

    async def get(self, tenant_id, opportunity_id):
        return self.opportunity

    async def get_for_handoff(self, tenant_id, opportunity_id):
        return await self.get(tenant_id, opportunity_id)


class _RequestHandoffs:
    def __init__(self) -> None:
        self.added = []

    async def find_pending_for_opportunity(self, tenant_id, opportunity_id):
        return None

    async def add(self, packet) -> None:
        self.added.append(packet)


class _NoopProvenance:
    async def save(self, *args) -> None:
        return None


class _Bus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, event) -> None:
        self.events.append(event)


class _RequestUoW:
    def __init__(self, opportunity: Opportunity) -> None:
        self.opportunities = _RequestRepo(opportunity)
        self.handoffs = _RequestHandoffs()
        self.provenance = _NoopProvenance()
        self.bus = _Bus()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _handoff_request() -> HandoffCreateRequest:
    return HandoffCreateRequest(
        opportunity_id="opp-1",
        trigger="quote_requested",
        account_name="Acme",
        country="US",
        why_valuable="expansion",
        customer_verbatim="we need hinges",
        customer_verbatim_provenance=Provenance(
            source_type=SourceType.CONVERSATION,
            source_id="msg-1",
            extracted_by="human",
            extracted_at=_NOW,
        ),
    )


def _opportunity(owner: EmployeeId | None) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId("opp-1"),
        tenant_id=_TENANT,
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId("need-1"),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
        owner=owner,
    )


async def test_request_handoff_copies_current_owner_to_packet_and_event() -> None:
    uow = _RequestUoW(_opportunity(EmployeeId("sales-1")))
    service = OpportunityServiceImpl(
        lambda: uow,
        _NeverScorer(),
        HandoffPolicy(sla_seconds=1, backlog_threshold=1),
        authorizer=_AllowAuthorizer(),
        audit=_Audit(),
        now=lambda: _NOW,
    )
    await service.request_handoff(_TENANT, _handoff_request(), actor=_opportunity_actor())
    assert uow.handoffs.added[0].assigned_to == EmployeeId("sales-1")
    assert uow.bus.events[0].assigned_to == EmployeeId("sales-1")


async def test_request_handoff_without_current_owner_fails_closed() -> None:
    uow = _RequestUoW(_opportunity(None))
    service = OpportunityServiceImpl(
        lambda: uow,
        _NeverScorer(),
        HandoffPolicy(sla_seconds=1, backlog_threshold=1),
        authorizer=_AllowAuthorizer(),
        audit=_Audit(),
        now=lambda: _NOW,
    )
    with pytest.raises(ValidationError):
        await service.request_handoff(
            _TENANT, _handoff_request(), actor=_opportunity_actor()
        )
    assert uow.handoffs.added == []
    assert uow.bus.events == []


def test_owner_mode_keeps_legacy_and_has_only_durable_owner_wait() -> None:
    interval = timedelta(seconds=7200)
    definition = build_human_handoff_definition(
        _T1, _T2, owner_reminder_interval=interval
    )
    assert definition.workflow_type == HUMAN_HANDOFF_WORKFLOW_TYPE
    assert definition.version == 7201
    assert [step.step_name for step in definition.steps] == [
        "notify_owner",
        "wait_owner_acceptance",
    ]
    wait = definition.steps[1]
    assert wait.reminder_interval == interval
    assert wait.timeout is None
    assert wait.wait_event_type == "HandoffAccepted"
    assert wait.reminder_handler_ref == "human_handoff.remind_owner"
    assert build_human_handoff_definition(_T1, _T2).version == 1


@pytest.mark.parametrize("seconds", [0, -1, 0.5])
def test_owner_mode_rejects_nonpositive_or_fractional_interval(seconds) -> None:
    with pytest.raises(ValidationError):
        build_human_handoff_definition(
            _T1, _T2, owner_reminder_interval=timedelta(seconds=seconds)
        )


async def test_new_owner_wait_checks_current_facts_before_every_enqueue() -> None:
    from contextlib import asynccontextmanager

    from workflows.engine.runner import ReminderInvocation

    class PendingOpportunities(_FakeOpportunityService):
        pending = True

        @asynccontextmanager
        async def handoff_notification_scope(self, *args, **kwargs):
            yield self.pending

    opportunities = PendingOpportunities()
    notifier = _FakeNotifier()
    handlers = build_human_handoff_step_handlers(
        opportunity_service=opportunities,
        employee_service=_FakeEmployeeService([]),
        notifier=notifier,
        opportunity_system_actor=_opportunity_actor(),
        employee_system_actor=_employee_actor(),
        t1=_T1,
        t2=_T2,
        now=lambda: _NOW,
        owner_reminder_interval=timedelta(seconds=7200),
    )
    run = _run("notify_owner")
    run.workflow_version = 7201
    assert await handlers["human_handoff.notify_pending_owner"].execute(run) == (
        "advance",
        "wait_owner_acceptance",
        {},
    )
    run.current_step = "wait_owner_acceptance"
    run.reminder = ReminderInvocation(1, _REQUESTED_AT + timedelta(seconds=7200))
    await handlers["human_handoff.remind_owner"].execute(run)
    opportunities.pending = False
    run.reminder = ReminderInvocation(2, _REQUESTED_AT + timedelta(seconds=14400))
    await handlers["human_handoff.remind_owner"].execute(run)
    assert [n.level for n in notifier.notices] == ["owner_pending", "owner_reminder"]
    assert all(n.recipient_id == EmployeeId("sales-1") for n in notifier.notices)
    assert opportunities.escalations == []


def test_owner_interval_rejects_database_integer_overflow() -> None:
    with pytest.raises(ValidationError):
        build_human_handoff_definition(
            _T1, _T2, owner_reminder_interval=timedelta(seconds=2147483647)
        )
