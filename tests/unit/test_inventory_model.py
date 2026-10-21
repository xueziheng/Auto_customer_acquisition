"""采集模型授权绑定原任务、当前员工、租约和固定来源调用序号。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domains.prospecting.service import AcquisitionJob, InventoryQuery
from shared.schemas.identifiers import AcquisitionJobId, EmployeeId, TenantId, UserId
from shared.schemas.model_invocation import ModelGenerationError

NOW = datetime(2026, 10, 13, tzinfo=UTC)
TENANT, EMPLOYEE = TenantId("tn_test"), EmployeeId("emp_test")


def setup():
    from apps.scheduler_worker.inventory_model import InventoryModelAuthority

    job = AcquisitionJob(
        id=AcquisitionJobId("acq_01K39P9M5D6K4A91YEQ80EJZ0X"),
        tenant_id=TENANT,
        employee_id=EMPLOYEE,
        query=InventoryQuery(product_tags=("tricycle",)),
        requested_count=5,
        remaining_count=5,
        status="processing",
        attempts=1,
        lease_token="fence-1",
        lease_until=NOW + timedelta(minutes=8),
        created_at=NOW,
        updated_at=NOW,
    )
    reader = SimpleNamespace(get_job=AsyncMock(return_value=job))
    employee = SimpleNamespace(
        tenant_id=TENANT,
        employee_id=EMPLOYEE,
        user_id=UserId("usr_test"),
        is_active=True,
        role="sales",
    )
    service = SimpleNamespace(get_employee=AsyncMock(return_value=employee))

    @asynccontextmanager
    async def employees(tenant):
        assert tenant == TENANT
        yield service

    config = SimpleNamespace(authorize=AsyncMock())
    authority = InventoryModelAuthority(
        job=job,
        source_id="company_websites",
        configuration_version="model-v1",
        backend=reader,
        employees=employees,
        configuration=config,
        now=lambda: NOW,
    )
    return authority, job, reader, employee, config


@pytest.mark.asyncio
async def test_identity_is_job_bound_stable_per_source():
    authority, job, _reader, _employee, config = setup()
    first = await authority.identity()
    assert first == await authority.identity()
    assert first.capability == "inventory_discovery" and first.run_id == job.id
    await authority.check(first)
    assert config.authorize.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tn_other"},
        {"employee_id": "emp_other"},
        {"user_id": "usr_other"},
        {"run_id": "run_other"},
        {"sequence": 999},
        {"capability": "research"},
        {"configuration_version": "old-model"},
        {"turn_id": "turn_other"},
    ],
)
async def test_untrusted_identity_cannot_enter_model(change):
    authority, _, _, _, config = setup()
    identity = (await authority.identity()).model_copy(update=change)
    with pytest.raises(ModelGenerationError):
        await authority.check(identity)
    config.authorize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"status": "complete"},
        {"lease_token": "new-fence"},
        {"lease_until": NOW},
        {"employee_id": EmployeeId("emp_other")},
    ],
)
async def test_stale_job_lease_cannot_dispatch_or_deliver_model(change):
    authority, job, reader, _, config = setup()
    identity = await authority.identity()
    reader.get_job.return_value = job.model_copy(update=change)
    with pytest.raises(ModelGenerationError):
        await authority.check(identity)
    config.authorize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("is_active", False), ("role", "viewer"), ("user_id", "usr_reassigned")],
)
async def test_current_employee_revocation_rejects_model(field, value):
    authority, _, _, employee, config = setup()
    identity = await authority.identity()
    setattr(employee, field, value)
    with pytest.raises(ModelGenerationError):
        await authority.check(identity)
    config.authorize.assert_not_awaited()
