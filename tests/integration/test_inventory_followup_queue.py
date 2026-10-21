"""授权搜索即时交付与后续异步交付必须在同一分配事务排准备任务。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from domains.prospecting.inventory_followup import InventoryFollowupAuthorization
from infra.db.prospect_inventory import PostgresInventoryService
from infra.db.prospect_inventory_tables import (
    CustomerPreparationItemRow,
    InventoryFollowupRequestRow,
)
from shared.errors import IdempotencyConflict, PermissionDenied
from shared.schemas.identifiers import EmployeeId, new_id
from tests.integration.test_prospect_inventory import NOW
from tests.unit.test_prospect_inventory import candidate

pytest_plugins = ("tests.integration.test_prospect_inventory",)


class Authority:
    async def authorize(self, tenant_id, employee_id, scope, query):
        return InventoryFollowupAuthorization(
            tenant_id=tenant_id,
            actor_employee_id=employee_id,
            actor_user_id="user:test",
            owner_employee_id=EmployeeId(new_id("emp")),
            scope=scope,
            policy_hash="a" * 64,
            authorized_at=NOW,
            query=query,
        )


@pytest.mark.parametrize("immediate", [True, False])
async def test_allocation_atomic_queue_and_replay(inventory, immediate):
    service, catalog, factory, tenant, _ = inventory
    service.followup_authority = Authority()
    scope = InventoryFollowupScope(campaign_id=new_id("cmp"), campaign_version=1)
    if immediate:
        await catalog.save_public((candidate(),))
    await service.request(
        tenant,
        EmployeeId("a"),
        InventoryQuery(product_tags=("hinges",)),
        1,
        "scope",
        followup_scope=scope,
    )
    if not immediate:
        job = await service.claim_job(tenant, timedelta(minutes=10))
        await catalog.save_public((candidate(),))
        await service.deliver_job(tenant, job.id, job.lease_token)
    async with factory() as session:
        requests = (
            await session.scalars(
                select(InventoryFollowupRequestRow).where(
                    InventoryFollowupRequestRow.tenant_id == tenant
                )
            )
        ).all()
        items = (
            await session.scalars(
                select(CustomerPreparationItemRow).where(
                    CustomerPreparationItemRow.tenant_id == tenant
                )
            )
        ).all()
        assert len(requests) == len(items) == 1
        assert requests[0].authorization["scope"]["campaign_version"] == 1
        assert items[0].state == "queued"
    await service.request(
        tenant,
        EmployeeId("a"),
        InventoryQuery(product_tags=("hinges",)),
        1,
        "scope",
        followup_scope=scope,
    )
    with pytest.raises(IdempotencyConflict):
        await service.request(
            tenant,
            EmployeeId("a"),
            InventoryQuery(product_tags=("hinges",)),
            1,
            "scope",
        )
    claim = await service.claim_preparation(tenant)
    assert claim is not None
    assert await service.claim_preparation(tenant) is None
    service = PostgresInventoryService(factory, tenant, catalog, now=lambda: NOW + timedelta(minutes=11))
    recovered = await service.claim_preparation(tenant)
    assert recovered.lease_token != claim.lease_token
    with pytest.raises(PermissionDenied):
        await service.checkpoint_preparation(tenant, claim, stage="assign")
    recovered = await service.checkpoint_preparation(tenant, recovered, stage="assign")
    recovered = await service.checkpoint_preparation(tenant, recovered, stage="evidence", result_refs={"account_id": new_id("acc")}, ownership_locked_at=NOW)
    await service.checkpoint_preparation(tenant, recovered, stage="evidence", external_started=True)
    service.now = lambda: NOW + timedelta(minutes=22)
    assert await service.claim_preparation(tenant) is None
    async with factory() as session:
        item = (
            await session.scalars(
                select(CustomerPreparationItemRow).where(
                    CustomerPreparationItemRow.tenant_id == tenant
                )
            )
        ).one()
        assert item.state == "blocked"
        assert item.blocked_reason == "external_reconciliation_required"


async def test_missing_authority_and_legacy_do_not_queue(inventory):
    service, catalog, factory, tenant, _ = inventory
    await catalog.save_public((candidate(),))
    scope = InventoryFollowupScope(campaign_id=new_id("cmp"), campaign_version=1)
    with pytest.raises(PermissionDenied):
        await service.request(
            tenant,
            EmployeeId("a"),
            InventoryQuery(product_tags=("hinges",)),
            1,
            "missing",
            followup_scope=scope,
        )
    assert (await service.status(tenant)).used_today == 0
    await service.request(
        tenant, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "legacy"
    )
    async with factory() as session:
        assert not (
            await session.scalars(
                select(CustomerPreparationItemRow).where(
                    CustomerPreparationItemRow.tenant_id == tenant
                )
            )
        ).all()
        assert not (
            await session.scalars(
                select(InventoryFollowupRequestRow).where(
                    InventoryFollowupRequestRow.tenant_id == tenant
                )
            )
        ).all()
