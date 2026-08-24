"""OrganizationService 在真实 PostgreSQL 锁下的并发激活验收。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.organization.errors import PlaybookBaseVersionConflictError
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.schemas import PlaybookApprovalFact, PlaybookProposalCreate
from domains.organization.service_impl import OrganizationServiceImpl
from infra.db.organization_uow import SqlAlchemyOrganizationUnitOfWork
from shared.schemas.identifiers import ApprovalId, EmployeeId, IdempotencyKey, TenantId

NOW = datetime(2026, 8, 24, 14, tzinfo=UTC)
TENANT = TenantId("tenant-service-postgres")
BOSS_ID = EmployeeId("emp_01K00000000000000000000010")


def _actor(level: OrganizationScopeLevel, role: str, actor_id: str) -> OrganizationActor:
    return OrganizationActor(
        actor_id,
        OrganizationScope(level, TENANT),
        role,
    )


def _command(amount: str) -> PlaybookProposalCreate:
    return PlaybookProposalCreate.model_validate(
        {
            "company_type": "trading_company",
            "minimum_deal_amount": Decimal(amount),
            "minimum_deal_currency": "USD",
            "excluded_categories": ["adult"],
            "sourcing_regions": ["guangdong"],
            "excluded_countries": ["north korea"],
        }
    )


def _approval(version, suffix: str) -> PlaybookApprovalFact:
    return PlaybookApprovalFact(
        approval_id=ApprovalId(f"apr_{suffix}"),
        approval_type="playbook_change",
        change_set_ref=version.change_set_ref,
        decided_by=EmployeeId(f"emp_approver_{suffix}"),
        decided_at=NOW,
    )


@pytest.mark.asyncio
async def test_concurrent_candidates_from_same_base_allow_exactly_one_activation(
    db_url: str,
) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    service = OrganizationServiceImpl(
        lambda tenant_id: SqlAlchemyOrganizationUnitOfWork(factory, tenant_id),
        Phase1OrganizationAuthorizer(TENANT),
        now=lambda: NOW + timedelta(minutes=1),
    )
    boss = _actor(OrganizationScopeLevel.TENANT, "boss", str(BOSS_ID))
    system = _actor(
        OrganizationScopeLevel.SYSTEM,
        "system",
        "system:playbook-postgres-test",
    )
    try:
        initial_result = await service.propose_playbook(
            TENANT,
            _command("10000"),
            actor=boss,
            idempotency_key=IdempotencyKey("postgres-initial"),
        )
        initial = await service.get_version(
            TENANT, initial_result.playbook_version_id, actor=boss
        )
        await service.activate_playbook(
            TENANT,
            initial.playbook_version_id,
            _approval(initial, "postgres-initial"),
            actor=system,
        )
        first_result = await service.propose_playbook(
            TENANT,
            _command("11000"),
            actor=boss,
            idempotency_key=IdempotencyKey("postgres-first"),
        )
        second_result = await service.propose_playbook(
            TENANT,
            _command("12000"),
            actor=boss,
            idempotency_key=IdempotencyKey("postgres-second"),
        )
        first = await service.get_version(
            TENANT, first_result.playbook_version_id, actor=boss
        )
        second = await service.get_version(
            TENANT, second_result.playbook_version_id, actor=boss
        )

        outcomes = await asyncio.gather(
            service.activate_playbook(
                TENANT,
                first.playbook_version_id,
                _approval(first, "postgres-first"),
                actor=system,
            ),
            service.activate_playbook(
                TENANT,
                second.playbook_version_id,
                _approval(second, "postgres-second"),
                actor=system,
            ),
            return_exceptions=True,
        )

        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        conflicts = [
            item
            for item in outcomes
            if isinstance(item, PlaybookBaseVersionConflictError)
        ]
        assert len(conflicts) == 1
    finally:
        await engine.dispose()
