"""真实 PostgreSQL 验证国家政策工作流启动、恢复与批准应用幂等。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.approvals.service_impl import ApprovalServiceImpl
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyProposalCreate,
    CountryPolicyProposalResult,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.errors import TransientError
from shared.events.catalog import ApprovalDecided, CountryPolicyVersionProposed
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
    new_id,
)
from shared.schemas.provenance import SourceType
from workflows.engine.runner import StepHandler, StepStatus, WorkflowRun

try:
    from workflows.country_policy_change import (
        ApprovalDecidedHandler,
        CountryPolicyVersionProposedHandler,
        build_country_policy_change_definition,
        build_country_policy_change_handlers,
        register_country_policy_change,
    )
except ModuleNotFoundError:

    def _missing(*args: object, **kwargs: object):
        del args, kwargs
        pytest.fail("RED：country_policy_change workflow 尚未实现")

    ApprovalDecidedHandler = _missing
    CountryPolicyVersionProposedHandler = _missing
    build_country_policy_change_definition = _missing
    build_country_policy_change_handlers = _missing
    register_country_policy_change = _missing

NOW = datetime(2026, 8, 24, 15, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _command() -> CountryPolicyProposalCreate:
    return CountryPolicyProposalCreate.model_validate(
        {
            "country": "Synthetic Workflow Market",
            "public_research_allowed": True,
            "contact_enrichment_allowed": False,
            "cold_b2b_email_allowed": False,
            "personal_data_basis_required": True,
            "subject_type_affects_judgment": True,
            "contact_type_affects_judgment": True,
            "opt_out_deadline_days": 30,
            "local_representative_required": False,
            "requirements": ["honor_opt_out"],
            "notes": "Synthetic workflow fixture.",
            "field_sources": {
                field: {
                    "source_type": SourceType.EMPLOYEE_INPUT,
                    "source_id": f"assessment:workflow:{index}",
                }
                for index, field in enumerate(
                    sorted(DECISION_FIELDS, key=lambda item: item.value), 1
                )
            },
        }
    )


def _actor(
    tenant: TenantId, actor_id: str, scope: ComplianceScope, role: str
) -> ComplianceActor:
    return ComplianceActor(actor_id=actor_id, tenant_id=tenant, scope=scope, role=role)


class _StartOnlyStep:
    async def execute(self, run: WorkflowRun):
        raise AssertionError(f"start-only engine must not execute {run.current_step}")


class _FailOnceStep:
    def __init__(self, delegate: StepHandler) -> None:
        self._delegate = delegate
        self._failed = False

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, object]]:
        if not self._failed:
            self._failed = True
            raise TransientError("postgres://user:" + "sensitive@db/private")
        return await self._delegate.execute(run)


def _services(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    *,
    now: Callable[[], datetime] = lambda: NOW,
) -> tuple[ComplianceServiceImpl, ApprovalServiceImpl, ComplianceActor]:
    compliance = ComplianceServiceImpl(
        lambda requested_tenant: SqlAlchemyComplianceUnitOfWork(
            factory, requested_tenant, now=now
        ),
        Phase1ComplianceAuthorizer(tenant),
        now=now,
    )
    approvals = ApprovalServiceImpl(
        lambda requested_tenant: SqlAlchemyApprovalUnitOfWork(
            factory, requested_tenant, now=now
        ),
        now=now,
    )
    system = _actor(
        tenant, "system:country-policy-change", ComplianceScope.SYSTEM, "system"
    )
    return compliance, approvals, system


async def _proposal_and_engine(
    integration_engine: AsyncEngine,
    *,
    clock: _Clock,
    suffix: str,
    fail_once_handler_ref: str | None = None,
) -> tuple[
    TenantId,
    EmployeeId,
    async_sessionmaker[AsyncSession],
    ComplianceServiceImpl,
    ApprovalServiceImpl,
    PostgresWorkflowEngine,
    CountryPolicyProposalResult,
]:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    factory = async_sessionmaker(
        bind=integration_engine, class_=AsyncSession, expire_on_commit=False
    )
    compliance, approvals, system = _services(factory, tenant, now=clock.now)
    proposal = await compliance.propose_country_policy(
        tenant,
        _command(),
        actor=_actor(tenant, str(proposer), ComplianceScope.TENANT, "boss"),
        idempotency_key=IdempotencyKey(f"country-policy-{suffix}"),
    )
    handlers = dict(build_country_policy_change_handlers(compliance, approvals, system))
    if fail_once_handler_ref is not None:
        handlers[fail_once_handler_ref] = _FailOnceStep(handlers[fail_once_handler_ref])
    engine = PostgresWorkflowEngine(factory, handlers, now=clock.now)
    engine.register(build_country_policy_change_definition())
    await engine.start(
        tenant,
        "country_policy_change",
        str(proposal.country_policy_version_id),
        {
            "country_policy_version_id": str(proposal.country_policy_version_id),
            "country_key": proposal.country_key,
            "content_hash": proposal.content_hash,
            "change_set_ref": proposal.change_set_ref,
            "proposed_by": str(proposer),
        },
        f"country-policy-change:{tenant}:{proposal.country_policy_version_id}",
    )
    assert await engine.poll_due(tenant, 10) == 3
    return tenant, proposer, factory, compliance, approvals, engine, proposal


def _start_only_engine(
    factory: async_sessionmaker[AsyncSession],
) -> PostgresWorkflowEngine:
    definition = build_country_policy_change_definition()
    handler: StepHandler = _StartOnlyStep()
    engine = PostgresWorkflowEngine(
        factory,
        {step.handler_ref: handler for step in definition.steps},
        now=lambda: NOW,
    )
    engine.register(definition)
    return engine


@pytest.mark.asyncio
async def test_duplicate_proposal_event_starts_one_run(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    factory = async_sessionmaker(
        bind=integration_engine, class_=AsyncSession, expire_on_commit=False
    )
    compliance, _, _ = _services(factory, tenant)
    proposal = await compliance.propose_country_policy(
        tenant,
        _command(),
        actor=_actor(tenant, str(proposer), ComplianceScope.TENANT, "boss"),
        idempotency_key=IdempotencyKey("country-policy-workflow-duplicate"),
    )
    event = CountryPolicyVersionProposed(
        tenant_id=tenant,
        occurred_at=NOW,
        country_policy_version_id=proposal.country_policy_version_id,
        country_key=proposal.country_key,
        content_hash=proposal.content_hash,
        proposed_by=proposer,
    )
    engine = _start_only_engine(factory)
    handler = CountryPolicyVersionProposedHandler(engine)

    first = await handler.handle(event)
    second = await handler.handle(event)

    assert first == second
    async with integration_engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT count(*) FROM workflow_runs "
                "WHERE tenant_id=:tenant AND workflow_type='country_policy_change'"
            ),
            {"tenant": str(tenant)},
        )
    assert count == 1


@pytest.mark.asyncio
async def test_api_start_then_outbox_delivery_reuses_same_run(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    factory = async_sessionmaker(
        bind=integration_engine, class_=AsyncSession, expire_on_commit=False
    )
    compliance, _, _ = _services(factory, tenant)
    proposal = await compliance.propose_country_policy(
        tenant,
        _command(),
        actor=_actor(tenant, str(proposer), ComplianceScope.TENANT, "boss"),
        idempotency_key=IdempotencyKey("country-policy-api-before-outbox"),
    )
    context = {
        "country_policy_version_id": str(proposal.country_policy_version_id),
        "country_key": proposal.country_key,
        "content_hash": proposal.content_hash,
        "change_set_ref": proposal.change_set_ref,
        "proposed_by": str(proposer),
    }
    key = f"country-policy-change:{tenant}:{proposal.country_policy_version_id}"
    engine = _start_only_engine(factory)
    direct = await engine.start(
        tenant,
        "country_policy_change",
        str(proposal.country_policy_version_id),
        context,
        key,
    )
    event = CountryPolicyVersionProposed(
        tenant_id=tenant,
        occurred_at=NOW,
        country_policy_version_id=proposal.country_policy_version_id,
        country_key=proposal.country_key,
        content_hash=proposal.content_hash,
        proposed_by=proposer,
    )

    recovered = await CountryPolicyVersionProposedHandler(engine).handle(event)

    assert recovered == direct


@pytest.mark.asyncio
async def test_duplicate_approval_event_does_not_activate_twice(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    approver = EmployeeId(new_id("emp"))
    factory = async_sessionmaker(
        bind=integration_engine, class_=AsyncSession, expire_on_commit=False
    )
    compliance, approvals, system = _services(factory, tenant)
    proposal = await compliance.propose_country_policy(
        tenant,
        _command(),
        actor=_actor(tenant, str(proposer), ComplianceScope.TENANT, "boss"),
        idempotency_key=IdempotencyKey("country-policy-approved-once"),
    )
    handlers = build_country_policy_change_handlers(compliance, approvals, system)
    engine = PostgresWorkflowEngine(factory, handlers, now=lambda: NOW)
    outbox = OutboxDeliverer(factory, tenant, now=lambda: NOW)
    register_country_policy_change(engine, outbox, approvals)
    assert await outbox.drain() == 1
    assert await engine.poll_due(tenant, 10) == 3
    approval = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert approval is not None
    await approvals.decide(tenant, ApprovalId(approval.approval_id), True, approver)
    decided = ApprovalDecided(
        tenant_id=tenant,
        occurred_at=NOW,
        approval_id=approval.approval_id,
        decision="approve",
        decided_by=approver,
    )

    assert await outbox.drain() == 1
    assert await engine.poll_due(tenant, 10) == 2
    await ApprovalDecidedHandler(engine, approvals).handle(decided)

    final = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final is not None
    assert final.state == "applied"
    async with integration_engine.connect() as connection:
        activation_count = await connection.scalar(
            text(
                "SELECT count(*) FROM country_policy_activations "
                "WHERE tenant_id=:tenant AND country_policy_version_id=:version"
            ),
            {"tenant": str(tenant), "version": str(proposal.country_policy_version_id)},
        )
    assert activation_count == 1


@pytest.mark.asyncio
async def test_timeout_expires_pending_country_policy_approval(
    integration_engine: AsyncEngine,
) -> None:
    clock = _Clock()
    tenant, _, _, _, approvals, engine, proposal = await _proposal_and_engine(
        integration_engine, clock=clock, suffix="timeout-expire"
    )
    approval = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert approval is not None and approval.state == "pending"

    clock.advance(timedelta(days=7, seconds=1))
    await engine.poll_due(tenant, 10)

    final = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final is not None and final.state == "expired"
    async with integration_engine.connect() as connection:
        status = await connection.scalar(
            text(
                "SELECT status FROM workflow_runs "
                "WHERE tenant_id=:tenant AND subject_ref=:version"
            ),
            {
                "tenant": str(tenant),
                "version": str(proposal.country_policy_version_id),
            },
        )
    assert status == "completed"


@pytest.mark.asyncio
async def test_timeout_race_applies_approval_decided_before_timeout_scan(
    integration_engine: AsyncEngine,
) -> None:
    clock = _Clock()
    tenant, _, _, _, approvals, engine, proposal = await _proposal_and_engine(
        integration_engine, clock=clock, suffix="timeout-approved-race"
    )
    approval = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert approval is not None
    approver = EmployeeId(new_id("emp"))
    clock.advance(timedelta(days=6))
    await approvals.decide(tenant, ApprovalId(approval.approval_id), True, approver)

    clock.advance(timedelta(days=1, seconds=1))
    await engine.poll_due(tenant, 10)

    final = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final is not None and final.state == "applied"
    async with integration_engine.connect() as connection:
        activation_count = await connection.scalar(
            text(
                "SELECT count(*) FROM country_policy_activations "
                "WHERE tenant_id=:tenant AND country_policy_version_id=:version"
            ),
            {
                "tenant": str(tenant),
                "version": str(proposal.country_policy_version_id),
            },
        )
    assert activation_count == 1


@pytest.mark.asyncio
async def test_submit_commit_then_decision_then_step_replay_reuses_original_approval(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    approver = EmployeeId(new_id("emp"))
    factory = async_sessionmaker(
        bind=integration_engine, class_=AsyncSession, expire_on_commit=False
    )
    compliance, approvals, system = _services(factory, tenant)
    proposal = await compliance.propose_country_policy(
        tenant,
        _command(),
        actor=_actor(tenant, str(proposer), ComplianceScope.TENANT, "boss"),
        idempotency_key=IdempotencyKey("country-policy-submit-crash-replay"),
    )
    handlers = build_country_policy_change_handlers(compliance, approvals, system)
    base_context = {
        "country_policy_version_id": str(proposal.country_policy_version_id),
        "country_key": proposal.country_key,
        "content_hash": proposal.content_hash,
        "change_set_ref": proposal.change_set_ref,
        "proposed_by": str(proposer),
    }
    engine = PostgresWorkflowEngine(factory, handlers, now=lambda: NOW)
    engine.register(build_country_policy_change_definition())
    run_id = await engine.start(
        tenant,
        "country_policy_change",
        str(proposal.country_policy_version_id),
        base_context,
        f"country-policy-change:{tenant}:{proposal.country_policy_version_id}",
    )
    assert await engine.poll_due(tenant, 1) == 1
    crash_run = WorkflowRun(
        run_id=run_id,
        tenant_id=tenant,
        workflow_type="country_policy_change",
        workflow_version=1,
        subject_ref=str(proposal.country_policy_version_id),
        current_step="submit_approval",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=base_context,
    )
    first = await handlers["country_policy_change.submit"].execute(crash_run)
    original_id = ApprovalId(first[2]["approval_id"])
    await approvals.decide(tenant, original_id, True, approver)

    assert await engine.poll_due(tenant, 10) == 4
    async with integration_engine.connect() as connection:
        approval_count = await connection.scalar(
            text(
                "SELECT count(*) FROM approval_packages "
                "WHERE tenant_id=:tenant AND change_set_ref=:change_set"
            ),
            {"tenant": str(tenant), "change_set": proposal.change_set_ref},
        )
        run_row = (
            (
                await connection.execute(
                    text(
                        "SELECT status, context FROM workflow_runs "
                        "WHERE tenant_id=:tenant AND run_id=:run"
                    ),
                    {"tenant": str(tenant), "run": str(run_id)},
                )
            )
            .mappings()
            .one()
        )
    assert approval_count == 1
    assert run_row["status"] == "completed"
    assert run_row["context"]["approval_id"] == str(original_id)
    final = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final is not None and final.state == "applied"


@pytest.mark.asyncio
async def test_apply_transient_error_uses_engine_backoff_then_retries(
    integration_engine: AsyncEngine,
) -> None:
    clock = _Clock()
    tenant, _, _, _, approvals, engine, proposal = await _proposal_and_engine(
        integration_engine,
        clock=clock,
        suffix="apply-transient-retry",
        fail_once_handler_ref="country_policy_change.apply",
    )
    approval = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert approval is not None
    approver = EmployeeId(new_id("emp"))
    await approvals.decide(tenant, ApprovalId(approval.approval_id), True, approver)
    await ApprovalDecidedHandler(engine, approvals).handle(
        ApprovalDecided(
            tenant_id=tenant,
            occurred_at=clock.now(),
            approval_id=approval.approval_id,
            decision="approve",
            decided_by=approver,
        )
    )

    assert await engine.poll_due(tenant, 1) == 1
    async with integration_engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT current_step, status, retry_count, next_poll_at, last_error "
                        "FROM workflow_runs WHERE tenant_id=:tenant AND subject_ref=:version"
                    ),
                    {
                        "tenant": str(tenant),
                        "version": str(proposal.country_policy_version_id),
                    },
                )
            )
            .mappings()
            .one()
        )
    assert row["current_step"] == "apply_policy"
    assert row["status"] == "running"
    assert row["retry_count"] == 1
    assert row["next_poll_at"] == clock.now() + timedelta(seconds=30)
    assert row["last_error"] == "TransientError"

    clock.advance(timedelta(seconds=30))
    assert await engine.poll_due(tenant, 10) == 2
    final = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final is not None and final.state == "applied"


@pytest.mark.asyncio
async def test_mark_applied_transient_error_uses_engine_backoff_then_retries(
    integration_engine: AsyncEngine,
) -> None:
    clock = _Clock()
    tenant, _, _, _, approvals, engine, proposal = await _proposal_and_engine(
        integration_engine,
        clock=clock,
        suffix="mark-applied-transient-retry",
        fail_once_handler_ref="country_policy_change.mark_applied",
    )
    approval = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert approval is not None
    approver = EmployeeId(new_id("emp"))
    await approvals.decide(tenant, ApprovalId(approval.approval_id), True, approver)
    await ApprovalDecidedHandler(engine, approvals).handle(
        ApprovalDecided(
            tenant_id=tenant,
            occurred_at=clock.now(),
            approval_id=approval.approval_id,
            decision="approve",
            decided_by=approver,
        )
    )

    assert await engine.poll_due(tenant, 1) == 1
    assert await engine.poll_due(tenant, 1) == 1
    async with integration_engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT current_step, status, retry_count, next_poll_at, last_error "
                        "FROM workflow_runs WHERE tenant_id=:tenant AND subject_ref=:version"
                    ),
                    {
                        "tenant": str(tenant),
                        "version": str(proposal.country_policy_version_id),
                    },
                )
            )
            .mappings()
            .one()
        )
    assert row["current_step"] == "mark_applied"
    assert row["status"] == "running"
    assert row["retry_count"] == 1
    assert row["next_poll_at"] == clock.now() + timedelta(seconds=30)
    assert row["last_error"] == "TransientError"

    clock.advance(timedelta(seconds=30))
    assert await engine.poll_due(tenant, 10) == 1
    final = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final is not None and final.state == "applied"
