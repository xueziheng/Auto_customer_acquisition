"""真实 PostgreSQL 验证国家政策工作流启动、恢复与批准应用幂等。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.approvals.service_impl import ApprovalServiceImpl
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import DECISION_FIELDS, CountryPolicyProposalCreate
from domains.compliance.service_impl import ComplianceServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.events.catalog import ApprovalDecided, CountryPolicyVersionProposed
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
    new_id,
)
from shared.schemas.provenance import SourceType
from workflows.engine.runner import StepHandler, WorkflowRun

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


def _services(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> tuple[ComplianceServiceImpl, ApprovalServiceImpl, ComplianceActor]:
    compliance = ComplianceServiceImpl(
        lambda requested_tenant: SqlAlchemyComplianceUnitOfWork(
            factory, requested_tenant, now=lambda: NOW
        ),
        Phase1ComplianceAuthorizer(tenant),
        now=lambda: NOW,
    )
    approvals = ApprovalServiceImpl(
        lambda requested_tenant: SqlAlchemyApprovalUnitOfWork(
            factory, requested_tenant, now=lambda: NOW
        ),
        now=lambda: NOW,
    )
    system = _actor(
        tenant, "system:country-policy-change", ComplianceScope.SYSTEM, "system"
    )
    return compliance, approvals, system


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
