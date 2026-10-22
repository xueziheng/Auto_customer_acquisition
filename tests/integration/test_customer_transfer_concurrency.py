"""真实多连接检验转交与入组/在途发送之间的互斥边界。"""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import EnrollmentCreateRequest, EnrollmentOwnershipBinding
from domains.outreach.service import EnrollmentStopReason
from domains.prospecting.inventory import InventoryQuery, InventoryTransferRequest
from infra.db.repositories.outreach import EnrollmentRepositoryImpl
from infra.db.tables import OutreachMessageAttemptRow, OwnershipLockRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import IdempotencyConflict, PermissionDenied
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    IdempotencyKey,
    SendingIdentityId,
    new_id,
)
from tests.integration.test_customer_assignment_bridge import (
    bridge_inventory as bridge_inventory,  # noqa: PLC0414
)
from tests.integration.test_outreach_concurrency import _snapshot
from tests.integration.test_outreach_enrollment_lifecycle import (
    NOW,
    MutableClock,
    _seed_active_campaign,
    _service,
)
from tests.integration.test_tool_gateway_pipeline import _Clock, _gateway, _received
from tests.unit.test_prospect_inventory import candidate
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.pipeline import ToolCallContext
from tool_gateway.repository import ToolCallId


async def _ready(bridge_inventory):
    inventory, catalog, factory, tenant, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await inventory.request(
            tenant, boss, InventoryQuery(product_tags=("hinges",)), 1, "race"
        )
    ).allocations[0]
    allocation = (
        await inventory.assign(tenant, (allocation.id,), boss, acting_employee_id=boss)
    )[0]
    campaign, approval, sender, contact = (
        CampaignId(new_id("cmp")),
        ApprovalId(new_id("apr")),
        SendingIdentityId(new_id("sid")),
        ContactPointId(new_id("cp")),
    )
    await _seed_active_campaign(factory, tenant, campaign, (sender,), approval)
    outreach = _service(
        factory,
        tenant,
        campaign,
        approval,
        (sender,),
        {
            (contact, allocation.account_id): _snapshot(
                tenant, contact, allocation.account_id
            ),
        },
        MutableClock(NOW),
    )
    actor = Actor(str(boss), OutreachScope(level=ScopeLevel.TENANT), "boss")
    request = EnrollmentCreateRequest(
        allocation.account_id, contact, IdempotencyKey("race-enroll")
    )
    transfer = InventoryTransferRequest(
        employee_id=sales,
        expected_employee_id=boss,
        expected_assigned_at=allocation.assigned_at,
        reason="明确转交",
    )
    return (
        inventory,
        factory,
        tenant,
        boss,
        allocation,
        outreach,
        campaign,
        actor,
        request,
        transfer,
    )


async def test_transfer_waits_for_uncommitted_enrollment_then_rejects(
    bridge_inventory, monkeypatch
):
    (
        inventory,
        _,
        tenant,
        boss,
        allocation,
        outreach,
        campaign,
        actor,
        request,
        transfer,
    ) = await _ready(bridge_inventory)
    inserted, release = asyncio.Event(), asyncio.Event()
    original = EnrollmentRepositoryImpl.insert_if_absent

    async def paused_insert(repository, enrollment):
        result = await original(repository, enrollment)
        inserted.set()
        await release.wait()
        return result

    monkeypatch.setattr(EnrollmentRepositoryImpl, "insert_if_absent", paused_insert)
    enroll = asyncio.create_task(
        outreach.enroll(tenant, campaign, request, actor=actor)
    )
    await asyncio.wait_for(inserted.wait(), 3)
    move = asyncio.create_task(
        inventory.transfer(tenant, allocation.id, transfer, acting_employee_id=boss)
    )
    try:
        done, _ = await asyncio.wait({move}, timeout=0.3)
        assert not done, "未提交入组必须阻止转交越过账户锁"
    finally:
        release.set()
        results = await asyncio.gather(enroll, move, return_exceptions=True)
    assert not isinstance(results[0], BaseException)
    assert isinstance(results[1], IdempotencyConflict)
    assert (await inventory.get_allocation(tenant, allocation.id)).employee_id == boss


async def _bound_request(factory, tenant, allocation, request):
    async with factory() as session:
        owner = (
            await session.scalars(
                select(OwnershipLockRow).where(
                    OwnershipLockRow.tenant_id == tenant,
                    OwnershipLockRow.account_id == allocation.account_id,
                )
            )
        ).one()
    return replace(
        request, ownership=EnrollmentOwnershipBinding(owner.owner, owner.locked_at)
    )


async def test_transfer_first_blocks_stale_enroll_until_commit(
    bridge_inventory, monkeypatch
):
    from domains.employees.service_impl import EmployeeServiceImpl

    (
        inventory,
        factory,
        tenant,
        boss,
        allocation,
        outreach,
        campaign,
        actor,
        request,
        transfer,
    ) = await _ready(bridge_inventory)
    request = await _bound_request(factory, tenant, allocation, request)
    entered, release = asyncio.Event(), asyncio.Event()
    original = EmployeeServiceImpl.transfer

    async def paused_transfer(service, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(service, *args, **kwargs)

    monkeypatch.setattr(EmployeeServiceImpl, "transfer", paused_transfer)
    move = asyncio.create_task(
        inventory.transfer(tenant, allocation.id, transfer, acting_employee_id=boss)
    )
    await asyncio.wait_for(entered.wait(), 3)
    enroll = asyncio.create_task(
        outreach.enroll(tenant, campaign, request, actor=actor)
    )
    try:
        done, _ = await asyncio.wait({enroll}, timeout=0.3)
        assert not done, "入组须等转交提交后核对原归属版本"
    finally:
        release.set()
        results = await asyncio.gather(move, enroll, return_exceptions=True)
    assert not isinstance(results[0], BaseException)
    assert isinstance(results[1], PermissionDenied)
    assert not await outreach.list_enrollments(
        tenant, actor.scope, limit=100, actor=actor
    )


async def test_ownership_binding_rejects_aba_with_fixed_clock(bridge_inventory):
    (
        inventory,
        factory,
        tenant,
        boss,
        allocation,
        outreach,
        campaign,
        actor,
        request,
        transfer,
    ) = await _ready(bridge_inventory)
    request = await _bound_request(factory, tenant, allocation, request)
    moved = await inventory.transfer(
        tenant, allocation.id, transfer, acting_employee_id=boss
    )
    await inventory.transfer(
        tenant,
        allocation.id,
        InventoryTransferRequest(
            employee_id=boss,
            expected_employee_id=moved.employee_id,
            expected_assigned_at=moved.assigned_at,
            reason="明确转回",
        ),
        acting_employee_id=boss,
    )
    with pytest.raises(PermissionDenied, match="归属"):
        await outreach.enroll(tenant, campaign, request, actor=actor)
    current_request = await _bound_request(factory, tenant, allocation, request)
    assert current_request.ownership.locked_at > request.ownership.locked_at
    result = await outreach.enroll(tenant, campaign, current_request, actor=actor)
    assert result.state.value == "enrolled"


@pytest.mark.parametrize("ledger", ["none", "executing", "unknown", "succeeded"])
async def test_stopped_enrollment_with_inflight_or_unknown_send_blocks_transfer(
    bridge_inventory, ledger
):
    (
        inventory,
        factory,
        tenant,
        boss,
        allocation,
        outreach,
        campaign,
        actor,
        request,
        transfer,
    ) = await _ready(bridge_inventory)
    enrollment = await outreach.enroll(tenant, campaign, request, actor=actor)
    system = Actor(
        "system:transfer-test",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    attempt = await outreach.prepare_message_attempt(
        tenant, enrollment.enrollment_id, actor=system
    )
    await outreach.claim_message_send(tenant, attempt.attempt_id, actor=system)
    await outreach.stop_enrollment(
        tenant, enrollment.enrollment_id, EnrollmentStopReason.MANUAL, actor=actor
    )
    if ledger != "none":
        # Connector已成功但canonical完成失败：Attempt已sent仍需先核对发送账本。
        await outreach.record_sent(
            tenant, attempt.attempt_id, "provider-ref", actor=system
        )
        call_id = ToolCallId(new_id("tcl"))
        record = replace(
            _received(tenant, call_id, NOW),
            message_attempt_id=str(attempt.attempt_id),
            campaign_id=str(campaign),
        )
        async with SqlAlchemyToolGatewayUnitOfWork(
            factory, tenant, now=lambda: NOW
        ) as uow:
            await uow.calls.create_received(record)
            await uow.calls.claim(
                tenant,
                call_id,
                tool_id="email.send",
                idempotency_key=attempt.idempotency_key,
                request_fingerprint="a" * 64,
                fingerprint_version="v1",
                lease_owner="worker",
                lease_expires_at=NOW + timedelta(seconds=5),
            )
            await uow.calls.mark_executing(tenant, call_id)
            if ledger in {"unknown", "succeeded"}:
                await uow.calls.complete(
                    tenant,
                    call_id,
                    status=ToolCallStatus.FAILED_TRANSIENT
                    if ledger == "unknown"
                    else ToolCallStatus.SUCCEEDED,
                    provider_ref=None if ledger == "unknown" else "provider-ref",
                    error_category=ToolErrorCategory.RECONCILIATION_REQUIRED
                    if ledger == "unknown"
                    else None,
                    retry_after_at=None,
                )
    if ledger == "succeeded":
        assert (
            await inventory.transfer(
                tenant, allocation.id, transfer, acting_employee_id=boss
            )
        ).employee_id == transfer.employee_id
    else:
        with pytest.raises(IdempotencyConflict, match="在途|核对"):
            await inventory.transfer(
                tenant, allocation.id, transfer, acting_employee_id=boss
            )
    async with factory() as session:
        state = await session.scalar(
            select(OutreachMessageAttemptRow.state).where(
                OutreachMessageAttemptRow.tenant_id == tenant,
                OutreachMessageAttemptRow.attempt_id == attempt.attempt_id,
            )
        )
    assert state == ("sending" if ledger == "none" else "sent")


async def test_running_gateway_after_stop_blocks_transfer_until_send_settles(
    bridge_inventory, monkeypatch
):
    (
        inventory,
        factory,
        tenant,
        boss,
        allocation,
        outreach,
        campaign,
        actor,
        request,
        transfer,
    ) = await _ready(bridge_inventory)
    enrollment = await outreach.enroll(tenant, campaign, request, actor=actor)
    system = Actor(
        "system:transfer-test",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    attempt = await outreach.prepare_message_attempt(
        tenant, enrollment.enrollment_id, actor=system
    )
    gateway, handler, rate = _gateway(factory, tenant, _Clock(NOW))
    entered, release = asyncio.Event(), asyncio.Event()

    async def claim(ctx, state):
        await outreach.claim_message_send(tenant, attempt.attempt_id, actor=system)

    async def record_sent(ctx, state, provider_ref):
        await outreach.record_sent(
            tenant, attempt.attempt_id, provider_ref, actor=system
        )

    async def transport_barrier(tenant_id, prepared):
        entered.set()
        await release.wait()
        return {"provider_ref": "provider-ref", "already_existed": False}

    monkeypatch.setattr(rate, "check", claim)
    monkeypatch.setattr(rate, "record_sent", record_sent)
    monkeypatch.setattr(handler, "execute", transport_barrier)
    send = asyncio.create_task(
        gateway.invoke(
            ToolCallContext(
                tenant_id=tenant,
                user_id=boss,
                tool_id="email.send",
                params={"attempt_id": str(attempt.attempt_id)},
                idempotency_key=attempt.idempotency_key,
                campaign_ref=str(campaign),
            )
        )
    )
    await asyncio.wait_for(entered.wait(), 3)
    try:
        await outreach.stop_enrollment(
            tenant, enrollment.enrollment_id, EnrollmentStopReason.MANUAL, actor=actor
        )
        with pytest.raises(IdempotencyConflict, match="在途|核对"):
            await inventory.transfer(
                tenant, allocation.id, transfer, acting_employee_id=boss
            )
    finally:
        release.set()
        result = await send
    assert result.status == ToolCallStatus.SUCCEEDED
    assert (
        await inventory.transfer(
            tenant, allocation.id, transfer, acting_employee_id=boss
        )
    ).employee_id == transfer.employee_id
