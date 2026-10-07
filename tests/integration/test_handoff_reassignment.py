"""真实PG事务交错下，接管创建/接受必须与负责人转交串行。"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete

from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.opportunities.schemas import HandoffCreateRequest
from infra.db.repositories.opportunities import (
    HandoffRepositoryImpl,
    OpportunityRepositoryImpl,
)
from infra.db.tables import HandoffRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, new_id
from shared.schemas.provenance import Provenance, SourceType
from tests.integration.test_owner_handoff_reminders import NOW
from tests.integration.test_owner_handoff_reminders import (
    scenario as scenario,  # noqa: PLC0414 - 复用真实 PostgreSQL 夹具
)


@pytest.mark.parametrize("operation", ["request", "accept"])
async def test_handoff_owner_read_holds_reassignment_until_commit(
    scenario, monkeypatch, operation: str,
) -> None:
    """读到负责人后必须持机会锁；否则转交可越过待创建/待接受事务。"""
    factory, tenant, owner, opp, hand, service, actor, _ = scenario
    next_owner = EmployeeId(new_id("emp"))
    if operation == "request":
        async with factory.begin() as session:
            await session.execute(delete(HandoffRow).where(
                HandoffRow.tenant_id == tenant, HandoffRow.handoff_id == hand,
            ))

    read, release = asyncio.Event(), asyncio.Event()
    original = getattr(OpportunityRepositoryImpl, "get_for_handoff", OpportunityRepositoryImpl.get)

    async def observed_read(repo, requested_tenant, opportunity_id):
        value = await original(repo, requested_tenant, opportunity_id)
        read.set()
        await release.wait()
        return value

    monkeypatch.setattr(OpportunityRepositoryImpl, "get", observed_read)
    monkeypatch.setattr(OpportunityRepositoryImpl, "get_for_handoff", observed_read, raising=False)
    if operation == "accept":
        pending = service.accept_handoff(tenant, hand, owner, actor=actor)
    else:
        pending = service.request_handoff(
            tenant,
            HandoffCreateRequest(
                opportunity_id=opp, trigger="quote_requested", account_name="Synthetic",
                country="US", why_valuable="Customer asked for a quote",
                customer_verbatim="Please quote these fasteners",
                customer_verbatim_provenance=Provenance(
                    source_type=SourceType.CONVERSATION, source_id="msg-synthetic",
                    extracted_by="human", extracted_at=NOW,
                ),
            ),
            actor=Actor("boss-synthetic", OpportunityScope(level=ScopeLevel.TENANT), "boss"),
        )
    operation_task = asyncio.create_task(pending)
    assignment_task = None
    try:
        await asyncio.wait_for(read.wait(), 3)

        async def reassign():
            async with factory.begin() as session:
                assert await OpportunityRepositoryImpl(session, tenant).assign_owner(
                    tenant, opp, next_owner, owner, NOW,
                )

        assignment_task = asyncio.create_task(reassign())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(assignment_task), 0.15)
    finally:
        release.set()
        result = await asyncio.wait_for(operation_task, 3)
        if assignment_task is not None:
            await asyncio.wait_for(assignment_task, 3)

    async with factory() as session:
        packet = await HandoffRepositoryImpl(session, tenant).get(
            tenant, result if operation == "request" else hand,
        )
        assert packet is not None
        if operation == "request":
            assert packet.assigned_to == next_owner
            assert packet.state.value == "requested"
        else:
            assert packet.assigned_to == owner
            assert packet.accepted_by == owner
            assert packet.state.value == "accepted"


@pytest.mark.parametrize("use_current_owner", [False, True])
async def test_accept_rechecks_after_waiting_for_reassignment(
    scenario, monkeypatch, use_current_owner: bool,
) -> None:
    """转交先取得锁时，旧员工不可接管，新员工须重读新指向后正常接管。"""
    factory, tenant, owner, opp, hand, service, actor, _ = scenario
    next_owner = EmployeeId(new_id("emp"))
    accepted_by = next_owner if use_current_owner else owner
    if use_current_owner:
        actor = Actor(str(next_owner), OpportunityScope(
            level=ScopeLevel.SELF, allowed_owners=frozenset({next_owner}),
        ), "sales")
    read, release = asyncio.Event(), asyncio.Event()
    original = HandoffRepositoryImpl.get

    async def first_packet_read(repo, requested_tenant, handoff_id):
        value = await original(repo, requested_tenant, handoff_id)
        if not read.is_set():
            read.set()
            await release.wait()
        return value

    monkeypatch.setattr(HandoffRepositoryImpl, "get", first_packet_read)
    task = None
    try:
        async with factory.begin() as transfer:
            assert await OpportunityRepositoryImpl(transfer, tenant).assign_owner(
                tenant, opp, next_owner, owner, NOW,
            )
            task = asyncio.create_task(service.accept_handoff(tenant, hand, accepted_by, actor=actor))
            await asyncio.wait_for(read.wait(), 3)
            release.set()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.15)
        if use_current_owner:
            await asyncio.wait_for(task, 3)
        else:
            with pytest.raises(PermissionDenied):
                await asyncio.wait_for(task, 3)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async with factory() as session:
        packet = await HandoffRepositoryImpl(session, tenant).get(tenant, hand)
        assert packet is not None
        assert packet.assigned_to == next_owner
        assert packet.accepted_by == (next_owner if use_current_owner else None)
