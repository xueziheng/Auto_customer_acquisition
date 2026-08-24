"""真实 PostgreSQL 验证首次 Playbook 审批、事件唤醒与崩溃重放。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.approvals.errors import SelfApprovalError
from domains.approvals.service_impl import ApprovalServiceImpl
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.schemas import PlaybookProposalCreate
from domains.organization.service_impl import OrganizationServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.organization_uow import SqlAlchemyOrganizationUnitOfWork
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    RunId,
    TenantId,
    new_id,
)
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.playbook_change import (
    build_playbook_change_definition,
    build_playbook_change_handlers,
    register_playbook_change,
)

NOW = datetime(2026, 8, 24, 15, tzinfo=UTC)


def _actor(
    tenant_id: TenantId, actor_id: str, level: OrganizationScopeLevel, role: str
) -> OrganizationActor:
    return OrganizationActor(
        actor_id,
        OrganizationScope(level=level, tenant_id=tenant_id),
        role,
    )


@pytest.mark.asyncio
async def test_first_candidate_approval_wakes_and_activates_with_crash_replay(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    approver = EmployeeId(new_id("emp"))
    factory = async_sessionmaker(
        bind=integration_engine, class_=AsyncSession, expire_on_commit=False
    )
    organization = OrganizationServiceImpl(
        lambda requested_tenant: SqlAlchemyOrganizationUnitOfWork(
            factory, requested_tenant
        ),
        Phase1OrganizationAuthorizer(tenant),
        now=lambda: NOW,
    )
    approvals = ApprovalServiceImpl(
        lambda requested_tenant: SqlAlchemyApprovalUnitOfWork(
            factory, requested_tenant, now=lambda: NOW
        ),
        now=lambda: NOW,
    )
    boss = _actor(tenant, str(proposer), OrganizationScopeLevel.TENANT, "boss")
    system = _actor(
        tenant,
        "system:playbook-change-postgres",
        OrganizationScopeLevel.SYSTEM,
        "system",
    )
    proposal = await organization.propose_playbook(
        tenant,
        PlaybookProposalCreate.model_validate(
            {
                "company_type": "trading_company",
                "minimum_deal_amount": Decimal("10000.00"),
                "minimum_deal_currency": "USD",
                "excluded_categories": ["adult"],
                "sourcing_regions": ["guangdong"],
                "excluded_countries": ["north korea"],
            }
        ),
        actor=boss,
        idempotency_key=IdempotencyKey("postgres-first-playbook"),
    )
    handlers = build_playbook_change_handlers(organization, approvals, system)
    engine = PostgresWorkflowEngine(factory, handlers, now=lambda: NOW)
    engine.register(build_playbook_change_definition())
    run_id = await engine.start(
        tenant,
        "playbook_change",
        str(proposal.playbook_version_id),
        {
            "playbook_version_id": str(proposal.playbook_version_id),
            "content_hash": proposal.content_hash,
            "change_set_ref": proposal.change_set_ref,
        },
        f"playbook-change:{proposal.playbook_version_id}:{proposal.content_hash}",
    )

    assert await engine.poll_due(tenant, 10) == 3
    view = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert view is not None
    assert view.state == "pending"
    with pytest.raises(SelfApprovalError):
        await approvals.decide(tenant, ApprovalId(view.approval_id), True, proposer)
    await approvals.decide(tenant, ApprovalId(view.approval_id), True, approver)

    deliverer = OutboxDeliverer(factory, tenant, now=lambda: NOW)
    register_playbook_change(engine, deliverer, approvals)
    assert await deliverer.drain() == 1

    run_context = {
        "playbook_version_id": str(proposal.playbook_version_id),
        "content_hash": proposal.content_hash,
        "change_set_ref": proposal.change_set_ref,
        "proposed_by": str(proposer),
        "approval_id": view.approval_id,
        "approval_timeout_seconds": 604800,
        "approval_state": "approved",
    }
    # 模拟组织域激活事务已经提交、工作流步骤事务尚未提交时进程崩溃。
    direct = await handlers["playbook_change.apply"].execute(
        WorkflowRun(
            run_id=RunId(str(run_id)),
            tenant_id=tenant,
            workflow_type="playbook_change",
            workflow_version=1,
            subject_ref=str(proposal.playbook_version_id),
            current_step="apply_playbook",
            status=StepStatus.RUNNING,
            created_at=NOW,
            context=run_context,
        )
    )
    assert direct == (
        "advance",
        "mark_applied",
        {"application_state": "activated"},
    )
    assert await engine.poll_due(tenant, 10) == 2

    final_view = await approvals.get_by_change_set(tenant, proposal.change_set_ref)
    assert final_view is not None
    assert final_view.state == "applied"
    async with integration_engine.connect() as connection:
        activation_count = await connection.scalar(
            text(
                "SELECT count(*) FROM company_playbook_activations "
                "WHERE tenant_id=:tenant"
            ),
            {"tenant": str(tenant)},
        )
        application_count = await connection.scalar(
            text("SELECT count(*) FROM approval_applications WHERE tenant_id=:tenant"),
            {"tenant": str(tenant)},
        )
        status = await connection.scalar(
            text(
                "SELECT status FROM workflow_runs "
                "WHERE tenant_id=:tenant AND run_id=:run"
            ),
            {"tenant": str(tenant), "run": str(run_id)},
        )
    assert activation_count == 1
    assert application_count == 1
    assert status == "completed"
