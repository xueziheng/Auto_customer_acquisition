"""准备队列执行的CAS、归属版本和原子写入边界。"""
from datetime import timedelta

import pytest
from sqlalchemy import select

from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from infra.db.prospect_inventory_tables import CustomerPreparationItemRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, new_id
from tests.integration.test_inventory_followup_queue import Authority
from tests.integration.test_prospect_inventory import NOW
from tests.unit.test_prospect_inventory import candidate

pytest_plugins = ("tests.integration.test_prospect_inventory",)


async def claim_item(inventory):
    service, catalog, _, tenant, _ = inventory
    service.followup_authority = Authority()
    await catalog.save_public((candidate(),))
    await service.request(tenant, EmployeeId("a"), InventoryQuery(product_tags=("hinges",)), 1, "scope", followup_scope=InventoryFollowupScope(campaign_id=new_id("cmp"), campaign_version=1))
    return service, tenant, await service.claim_preparation(tenant)


async def test_current_lease_guard_and_fixed_block_reason(inventory):
    service, tenant, claim = await claim_item(inventory)
    await service.require_preparation_claim(tenant, claim)
    with pytest.raises(ValueError):
        await service.block_preparation(tenant, claim, reason="raw email content")
    await service.block_preparation(tenant, claim, reason="verification_unavailable")
    with pytest.raises(PermissionDenied):
        await service.require_preparation_claim(tenant, claim)
    assert await service.claim_preparation(tenant) is None
    async with inventory[2]() as session:
        row = (await session.scalars(select(CustomerPreparationItemRow).where(CustomerPreparationItemRow.tenant_id == tenant))).one()
        assert row.state == "blocked" and row.blocked_reason == "verification_unavailable"


async def test_checkpoint_cannot_skip_or_overwrite_ownership(inventory):
    service, tenant, claim = await claim_item(inventory)
    with pytest.raises(PermissionDenied):
        await service.checkpoint_preparation(tenant, claim, stage="enroll")
    claim = await service.checkpoint_preparation(tenant, claim, stage="assign")
    claim = await service.checkpoint_preparation(tenant, claim, stage="evidence", result_refs={"account_id": new_id("acc")}, ownership_locked_at=NOW)
    assert claim.ownership_locked_at == NOW
    with pytest.raises(PermissionDenied):
        await service.checkpoint_preparation(tenant, claim, stage="evidence", ownership_locked_at=NOW + timedelta(seconds=1))
    service.now = lambda: NOW + timedelta(minutes=11)
    recovered = await service.claim_preparation(tenant)
    assert recovered.ownership_locked_at == NOW
    assert recovered.result_refs == claim.result_refs
