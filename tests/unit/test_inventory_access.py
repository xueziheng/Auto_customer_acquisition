"""库存领域门面必须重新检查当前员工权限。"""

import pytest

from domains.prospecting.inventory_access import InventoryAccessScope, ScopedInventory
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId
from tests.unit.test_prospect_inventory_api import Inventory

TENANT = TenantId("tn_inventory_api")
EMPLOYEE = EmployeeId("emp_inventory_api")


class Authority:
    allowed = True
    boss = False

    async def current_scope(self, tenant_id, employee_id):
        if not self.allowed:
            raise PermissionDenied("员工已停用")
        return InventoryAccessScope(
            tenant_id, employee_id, None if self.boss else frozenset({employee_id})
        )


async def test_rechecks_revocation_and_rejects_widened_scope():
    raw, authority = Inventory(), Authority()
    service = ScopedInventory(raw, authority, TENANT, EMPLOYEE)
    with pytest.raises(PermissionDenied):
        await service.status(TENANT)
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, None, 50)
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, (EmployeeId("other"),), 50)
    authority.allowed = False
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, (EMPLOYEE,), 50)
    assert raw.calls == []


async def test_business_scope_cannot_modify_company_quota_or_impersonate():
    raw, authority = Inventory(), Authority()
    service = ScopedInventory(raw, authority, TENANT, EMPLOYEE)
    with pytest.raises(PermissionDenied):
        await service.set_daily_limit(TENANT, 1000)
    with pytest.raises(PermissionDenied):
        await service.request(TENANT, EmployeeId("other"), raw.job.query, 2, "key")
    with pytest.raises(PermissionDenied):
        await service.status(TenantId("other"))
    assert raw.calls == []


async def test_each_history_page_rechecks_revoked_employee():
    raw, authority = Inventory(), Authority()
    service = ScopedInventory(raw, authority, TENANT, EMPLOYEE)
    await service.list_allocations(TENANT, (EMPLOYEE,), 50, "allocation_cursor")
    authority.allowed = False
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, (EMPLOYEE,), 50, "allocation_cursor")
    assert raw.calls == [(TENANT, (EMPLOYEE,), 50, "allocation_cursor")]


async def test_only_current_boss_can_search_assign_and_read_company_diagnostics():
    raw, authority = Inventory(), Authority()
    service = ScopedInventory(raw, authority, TENANT, EMPLOYEE)
    from shared.schemas.identifiers import CandidateAllocationId

    commands = (
        lambda: service.request(TENANT, EMPLOYEE, raw.job.query, 1, "key"),
        lambda: service.status(TENANT),
        lambda: service.list_jobs(TENANT, (EMPLOYEE,), 10),
        lambda: service.get_job(TENANT, raw.job.id),
        lambda: service.assign(
            TENANT,
            (CandidateAllocationId("cal_one"),),
            EMPLOYEE,
            acting_employee_id=EMPLOYEE,
        ),
    )
    for command in commands:
        with pytest.raises(PermissionDenied):
            await command()
    assert raw.calls == []
    authority.boss = True
    await service.request(TENANT, EMPLOYEE, raw.job.query, 1, "key")
    await service.assign(
        TENANT,
        (CandidateAllocationId("cal_one"),),
        EMPLOYEE,
        acting_employee_id=EMPLOYEE,
    )
    assert raw.calls[-1][-1] == EMPLOYEE
    authority.boss = False
    for command in commands:
        with pytest.raises(PermissionDenied):
            await command()


async def test_exact_allocation_read_hides_unassigned_and_other_owner():
    from domains.prospecting.inventory import CandidateAllocation
    from shared.schemas.identifiers import CandidateAllocationId
    from tests.unit.test_prospect_inventory import NOW, candidate

    raw, authority = Inventory(), Authority()
    service = ScopedInventory(raw, authority, TENANT, EMPLOYEE)
    for owner in (None, EmployeeId("other"), EMPLOYEE):
        raw.allocation = CandidateAllocation(
            tenant_id=TENANT,
            id=CandidateAllocationId("cal_one"),
            employee_id=owner,
            candidate=candidate(),
            allocated_at=NOW,
            request_key="hidden",
        )
        value = await service.get_allocation(TENANT, raw.allocation.id)
        assert (value is not None) == (owner == EMPLOYEE)
    authority.boss = True
    assert await service.get_allocation(TENANT, raw.allocation.id) == raw.allocation
    authority.allowed = False
    with pytest.raises(PermissionDenied):
        await service.get_allocation(TENANT, raw.allocation.id)


async def test_manager_subordinate_snapshot_cannot_widen_inventory_reads():
    raw = Inventory()

    class StaleScope:
        async def current_scope(self, tenant_id, employee_id):
            return InventoryAccessScope(
                tenant_id,
                employee_id,
                frozenset({employee_id, EmployeeId("subordinate")}),
            )

    service = ScopedInventory(raw, StaleScope(), TENANT, EMPLOYEE)
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, (EmployeeId("subordinate"),), 50)
    await service.list_allocations(TENANT, (EMPLOYEE,), 50)
    assert raw.calls == [(TENANT, (EMPLOYEE,), 50, None)]


async def test_boss_assignment_cannot_impersonate_actor():
    from shared.schemas.identifiers import CandidateAllocationId

    raw, authority = Inventory(), Authority()
    authority.boss = True
    service = ScopedInventory(raw, authority, TENANT, EMPLOYEE)
    with pytest.raises(PermissionDenied):
        await service.assign(
            TENANT,
            (CandidateAllocationId("cal_one"),),
            EMPLOYEE,
            acting_employee_id=EmployeeId("other"),
        )
    assert raw.calls == []


async def test_composition_reloads_current_boss_role_and_manager_self_scope():
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from types import SimpleNamespace

    from apps.api.composition.inventory import bind_inventory
    from apps.api.dependencies import get_request_identity
    from tests.unit.test_prospect_inventory_api import setup

    app, raw = setup(role="boss")
    identity = app.dependency_overrides[get_request_identity]()
    employee = [identity.employee]

    class Employees:
        async def get_employee(self, tenant_id, employee_id, *, actor):
            return employee[0]

        async def list_active(self, tenant_id, *, actor):
            raise AssertionError("不应扩大经理到下属范围")

    @asynccontextmanager
    async def factory(tenant_id):
        yield Employees()

    service = bind_inventory(
        raw,
        SimpleNamespace(employees=factory, employee_lookup_actor=object()),
        identity,
    )
    await service.status(TENANT)
    employee[0] = replace(employee[0], role="manager")
    with pytest.raises(PermissionDenied):
        await service.request(TENANT, EMPLOYEE, raw.job.query, 1, "revoked-boss")
    await service.list_allocations(TENANT, (EMPLOYEE,), 10)
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, None, 10)
    employee[0] = replace(employee[0], is_active=False)
    with pytest.raises(PermissionDenied):
        await service.list_allocations(TENANT, (EMPLOYEE,), 10)
