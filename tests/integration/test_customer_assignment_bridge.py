"""客户档案、归属与取得记录必须同一事务落库。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.prospecting.inventory import InventoryQuery
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.tables import EmployeeRow, OwnershipLockRow, ProspectAccountRow
from shared.errors import IdempotencyConflict
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from tests.unit.test_prospect_inventory import candidate

NOW = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.fixture
async def bridge_inventory(integration_engine):
    from apps.api.composition.customer_assignment import CustomerAssignmentBridge

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    t = TenantId(new_id("tn"))
    boss, sales, other = (EmployeeId(new_id("emp")) for _ in range(3))
    async with factory() as s, s.begin():
        s.add_all(
            [
                EmployeeRow(
                    tenant_id=t,
                    employee_id=e,
                    name="测试",
                    role=r,
                    created_at=NOW,
                    is_active=True,
                    languages=[],
                )
                for e, r in ((boss, "boss"), (sales, "sales"), (other, "sales"))
            ]
        )
    catalog = PostgresPublicCatalog(
        factory, TenantId("ctl_" + uuid4().hex), now=lambda: NOW
    )
    service = PostgresInventoryService(
        factory,
        t,
        catalog,
        now=lambda: NOW,
        assignment_bridge=CustomerAssignmentBridge(now=lambda: NOW),
    )
    return service, catalog, factory, t, boss, sales, other


async def test_assignment_creates_account_and_real_owner_once(bridge_inventory):
    service, catalog, factory, t, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocations = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 1, "first"
        )
    ).allocations
    result = await service.assign(
        t, (allocations[0].id,), sales, acting_employee_id=boss
    )
    assert result[0].account_id
    assert (
        await service.assign(t, (allocations[0].id,), sales, acting_employee_id=boss)
        == result
    )
    async with factory() as s:
        account = (
            await s.scalars(
                select(ProspectAccountRow).where(ProspectAccountRow.tenant_id == t)
            )
        ).one()
        lock = (
            await s.scalars(
                select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == t)
            )
        ).one()
        assert account.entity_type is None and account.field_provenance
        assert lock.owner == sales and lock.account_id == result[0].account_id


async def test_transfer_updates_allocation_owner_and_history_without_quota(
    bridge_inventory,
):
    from domains.prospecting.inventory import InventoryTransferRequest
    from infra.db.tables import OwnershipTransferHistoryRow

    service, catalog, factory, t, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 1, "transfer"
        )
    ).allocations[0]
    original = (
        await service.assign(t, (allocation.id,), boss, acting_employee_id=boss)
    )[0]
    before = await service.status(t)
    command = InventoryTransferRequest(
        employee_id=sales,
        expected_employee_id=boss,
        expected_assigned_at=original.assigned_at,
        reason="管理员交给员工跟进",
    )
    result = await service.transfer(t, allocation.id, command, acting_employee_id=boss)
    assert result.employee_id == sales and result.account_id == original.account_id
    assert (await service.status(t)).used_today == before.used_today
    assert not await service.list_allocations(t, (boss,), 50)
    assert len(await service.list_allocations(t, (sales,), 50)) == 1
    async with factory() as s:
        lock = (
            await s.scalars(
                select(OwnershipLockRow).where(
                    OwnershipLockRow.tenant_id == t,
                    OwnershipLockRow.account_id == result.account_id,
                )
            )
        ).one()
        history = (
            await s.scalars(
                select(OwnershipTransferHistoryRow).where(
                    OwnershipTransferHistoryRow.tenant_id == t,
                    OwnershipTransferHistoryRow.account_id == result.account_id,
                )
            )
        ).one()
        assert lock.owner == sales
        assert (history.from_owner, history.to_owner, history.transferred_by) == (
            boss,
            sales,
            boss,
        )
    with pytest.raises(IdempotencyConflict):
        await service.transfer(t, allocation.id, command, acting_employee_id=boss)
    await service.transfer(
        t,
        allocation.id,
        InventoryTransferRequest(
            employee_id=boss,
            expected_employee_id=sales,
            expected_assigned_at=result.assigned_at,
            reason="再次安排",
        ),
        acting_employee_id=boss,
    )
    with pytest.raises(IdempotencyConflict):
        await service.transfer(t, allocation.id, command, acting_employee_id=boss)


async def test_transfer_bridge_failure_rolls_back_owner_and_history(bridge_inventory):
    from domains.prospecting.inventory import InventoryTransferRequest
    from infra.db.tables import OwnershipTransferHistoryRow

    service, catalog, factory, t, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 1, "transfer"
        )
    ).allocations[0]
    original = (
        await service.assign(t, (allocation.id,), boss, acting_employee_id=boss)
    )[0]
    bridge = service.assignment_bridge

    class BrokenBridge:
        async def transfer(self, *args):
            await bridge.transfer(*args)
            raise RuntimeError("模拟事务后续失败")

    service.assignment_bridge = BrokenBridge()
    with pytest.raises(RuntimeError):
        await service.transfer(
            t,
            original.id,
            InventoryTransferRequest(
                employee_id=sales,
                expected_employee_id=boss,
                expected_assigned_at=original.assigned_at,
                reason="转交",
            ),
            acting_employee_id=boss,
        )
    assert (await service.get_allocation(t, original.id)) == original
    async with factory() as s:
        owner = await s.scalar(
            select(OwnershipLockRow.owner).where(
                OwnershipLockRow.tenant_id == t,
                OwnershipLockRow.account_id == original.account_id,
            )
        )
        history = (
            await s.scalars(
                select(OwnershipTransferHistoryRow).where(
                    OwnershipTransferHistoryRow.tenant_id == t,
                )
            )
        ).all()
        assert owner == boss and not history


async def test_transfer_refuses_active_followup(bridge_inventory):
    from domains.prospecting.inventory import InventoryTransferRequest
    from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
    from infra.db.repositories.outreach import EnrollmentRepositoryImpl
    from tests.integration.test_outreach_repositories import (
        _campaign_graph,
        _enrollment,
    )

    service, catalog, factory, t, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 1, "active"
        )
    ).allocations[0]
    original = (
        await service.assign(t, (allocation.id,), boss, acting_employee_id=boss)
    )[0]
    campaign, version = _campaign_graph(tenant_id=t)
    async with SqlAlchemyOutreachUnitOfWork(factory, t, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)
    async with factory() as s, s.begin():
        await EnrollmentRepositoryImpl(s, t).insert_if_absent(
            _enrollment(tenant_id=t, account_id=original.account_id)
        )
    with pytest.raises(IdempotencyConflict, match="先停止"):
        await service.transfer(
            t,
            original.id,
            InventoryTransferRequest(
                employee_id=sales,
                expected_employee_id=boss,
                expected_assigned_at=original.assigned_at,
                reason="转交",
            ),
            acting_employee_id=boss,
        )
    assert (await service.get_allocation(t, original.id)) == original


@pytest.mark.parametrize("invalid", ["staff_actor", "foreign_target", "stale_owner"])
async def test_transfer_refuses_unauthorized_or_stale_command(
    bridge_inventory, invalid
):
    from domains.prospecting.inventory import InventoryTransferRequest
    from shared.errors import PermissionDenied

    service, catalog, _, t, boss, sales, other = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 1, "transfer"
        )
    ).allocations[0]
    original = (
        await service.assign(t, (allocation.id,), boss, acting_employee_id=boss)
    )[0]
    command = InventoryTransferRequest(
        employee_id=EmployeeId("emp_foreign") if invalid == "foreign_target" else sales,
        expected_employee_id=other if invalid == "stale_owner" else boss,
        expected_assigned_at=original.assigned_at,
        reason="明确转交",
    )
    with pytest.raises((PermissionDenied, IdempotencyConflict)):
        await service.transfer(
            t,
            allocation.id,
            command,
            acting_employee_id=sales if invalid == "staff_actor" else boss,
        )
    assert (await service.get_allocation(t, allocation.id)).employee_id == boss


async def test_second_bind_conflict_rolls_back_first_account(bridge_inventory):
    service, catalog, factory, t, boss, sales, _ = bridge_inventory
    await catalog.save_public(
        (
            candidate(id="pci_a", canonical_domain="a.example.com", public_emails=()),
            candidate(id="pci_b", canonical_domain="b.example.com", public_emails=()),
        )
    )
    values = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 2, "batch"
        )
    ).allocations
    # 在第二次桥接时制造现有归属冲突，保证第一个已执行而整批仍回滚。
    base_bridge = service.assignment_bridge

    class ConflictBridge:
        async def bind(self, session, tenant, allocation, target, acting):
            if allocation.id == sorted(v.id for v in values)[1]:
                raise IdempotencyConflict("已有其他负责人")
            return await base_bridge.bind(session, tenant, allocation, target, acting)

    service.assignment_bridge = ConflictBridge()
    with pytest.raises(IdempotencyConflict):
        await service.assign(
            t, tuple(v.id for v in values), sales, acting_employee_id=boss
        )
    async with factory() as s:
        assert not (
            await s.scalars(
                select(ProspectAccountRow).where(ProspectAccountRow.tenant_id == t)
            )
        ).all()
        assert not (
            await s.scalars(
                select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == t)
            )
        ).all()
    assert all(
        v.employee_id is None and v.account_id is None
        for v in await service.list_allocations(t, None, 10)
    )


async def test_unassigned_pool_filters_in_sql_before_limit(bridge_inventory):
    service, catalog, _, t, boss, sales, _ = bridge_inventory
    await catalog.save_public(
        tuple(
            candidate(
                id=f"pci_{i}", canonical_domain=f"c{i}.example.com", public_emails=()
            )
            for i in range(3)
        )
    )
    await service.request(
        t, boss, InventoryQuery(product_tags=("hinges",)), 3, "pending"
    )
    ordered = await service.list_allocations(t, None, 10)
    await service.assign(
        t, tuple(v.id for v in ordered[:2]), sales, acting_employee_id=boss
    )
    remaining = await service.list_allocations(t, None, 1, unassigned_only=True)
    assert remaining[0].id == ordered[2].id


async def test_historical_same_owner_sync_keeps_assignment_audit_and_quota(
    bridge_inventory,
):
    service, catalog, _, t, boss, sales, _ = bridge_inventory
    await catalog.save_public((candidate(public_emails=()),))
    allocation = (
        await service.request(
            t, boss, InventoryQuery(product_tags=("hinges",)), 1, "legacy"
        )
    ).allocations[0]
    bridge = service.assignment_bridge
    service.assignment_bridge = None  # 仅模拟历史纯仓储记录，线上组合必须注入。
    historical = (
        await service.assign(t, (allocation.id,), sales, acting_employee_id=boss)
    )[0]
    assert historical.account_id is None
    used = (await service.status(t)).used_today
    service.assignment_bridge = bridge
    synced = (
        await service.assign(t, (allocation.id,), sales, acting_employee_id=boss)
    )[0]
    assert synced.account_id and synced.employee_id == historical.employee_id
    assert (
        synced.assigned_at == historical.assigned_at
        and synced.assigned_by == historical.assigned_by
    )
    assert (await service.status(t)).used_today == used
