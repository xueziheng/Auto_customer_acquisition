"""承诺账本 PostgreSQL 仓储与事务边界。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.commitments.service_impl import CommitmentServiceImpl
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    CommitmentId,
    EmployeeId,
    MessageId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId("tenant-commitments-a")
OTHER_TENANT = TenantId("tenant-commitments-b")
_models = importlib.import_module("domains.commitments.models")
Commitment = _models.Commitment
CommitmentStatus = _models.CommitmentStatus
CommitmentType = _models.CommitmentType


def _commitment(
    tenant_id: TenantId,
    *,
    action: str = "Send the revised quotation",
    due_at: datetime = NOW + timedelta(days=1),
) -> Any:
    return Commitment(
        commitment_id=CommitmentId(new_id("com")),
        tenant_id=tenant_id,
        commitment_type=CommitmentType.EMPLOYEE,
        owner=EmployeeId("employee-owner"),
        action=action,
        due_at=due_at,
        source_message_id=MessageId("msg-commitment-source"),
        verbatim="I will send the revised quotation tomorrow.",
        created_at=NOW,
        extracted_by="team-operations-v1",
    )


@pytest.mark.asyncio
async def test_commitment_uow_persists_confirmation_and_outbox_atomically(
    integration_session: AsyncSession,
) -> None:
    from infra.db.commitment_uow import SqlAlchemyCommitmentUnitOfWork

    factory = async_sessionmaker(
        bind=integration_session.bind,
        expire_on_commit=False,
    )
    service = CommitmentServiceImpl(
        lambda tenant_id: SqlAlchemyCommitmentUnitOfWork(
            factory,
            tenant_id,
            now=lambda: NOW,
        ),
        now=lambda: NOW,
    )
    commitment = _commitment(TENANT)

    persisted_id = await service.record_extracted(TENANT, commitment)
    duplicate_id = await service.record_extracted(TENANT, _commitment(TENANT))
    await service.confirm(TENANT, persisted_id, EmployeeId("employee-reviewer"))

    async with SqlAlchemyCommitmentUnitOfWork(factory, TENANT) as uow:
        persisted = await uow.commitments.get(TENANT, persisted_id)

    assert duplicate_id == persisted_id
    assert persisted is not None
    assert persisted.confirmed_by == EmployeeId("employee-reviewer")
    assert persisted.confirmed_at == NOW


@pytest.mark.asyncio
async def test_commitment_repository_rejects_cross_tenant_reads(
    integration_session: AsyncSession,
) -> None:
    from infra.db.repositories.commitments import CommitmentRepositoryImpl

    repository = CommitmentRepositoryImpl(integration_session, TENANT)

    with pytest.raises(TenantIsolationViolation):
        await repository.get(OTHER_TENANT, CommitmentId(new_id("com")))


@pytest.mark.asyncio
async def test_due_query_excludes_unconfirmed_uncertain_and_fulfilled(
    integration_session: AsyncSession,
) -> None:
    from infra.db.repositories.commitments import CommitmentRepositoryImpl

    repository = CommitmentRepositoryImpl(integration_session, TENANT)
    confirmed = _commitment(TENANT, action="Confirmed due", due_at=NOW)
    confirmed.confirmed_by = EmployeeId("employee-reviewer")
    confirmed.confirmed_at = NOW
    uncertain = _commitment(TENANT, action="Uncertain due", due_at=NOW)
    uncertain.confirmed_by = EmployeeId("employee-reviewer")
    uncertain.confirmed_at = NOW
    uncertain.due_at_uncertain = True
    unconfirmed = _commitment(TENANT, action="Unconfirmed due", due_at=NOW)
    fulfilled = _commitment(TENANT, action="Fulfilled due", due_at=NOW)
    fulfilled.confirmed_by = EmployeeId("employee-reviewer")
    fulfilled.confirmed_at = NOW
    fulfilled.status = CommitmentStatus.FULFILLED
    fulfilled.fulfilled_at = NOW
    for item in (confirmed, uncertain, unconfirmed, fulfilled):
        await repository.add(item)
    await integration_session.commit()

    due = await repository.list_due(TENANT, NOW, 20)

    assert [item.action for item in due] == ["Confirmed due"]
