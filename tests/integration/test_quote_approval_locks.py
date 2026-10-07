"""报价审批员工全集与历史access lease真实多连接测试。"""

import asyncio

import pytest

from shared.schemas.identifiers import EmployeeId
from tests.integration.test_need_units import (
    unit_db_case as unit_db_case,  # noqa: PLC0414
)
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.integration.test_quote_context_locks import (
    context_case as context_case,  # noqa: PLC0414
)


async def test_historical_access_does_not_need_current_need_or_issuer(context_case):
    c = context_case
    assert hasattr(c.provider, "open_approval_access"), "缺少历史审批access lease"
    await c.unit.demand.update_need_fields(
        c.tenant_id,
        c.unit.need_id,
        {"quantity": {"value": 600, "quote": "600", "extracted_by": c.actor_id}},
        source_message_id="msg_customer_changed",
        updated_by=c.actor_id,
    )
    async with c.provider.open_approval_access(
        c.tenant_id,
        c.opportunity_id,
        c.actor_id,
        prepared_by=c.prepared_by,
        submitted_owner_id=EmployeeId("emp_owner"),
    ) as access:
        assert access.actor.role == "boss"
        assert access.owner.employee_id == "emp_owner"
        writer = asyncio.create_task(c.update_employee(c.actor_id, role="finance"))
        await c.wait_for_blocked_writer(writer)
    await asyncio.wait_for(writer, 3)


@pytest.mark.parametrize("reverse", [False, True])
async def test_all_deciders_share_locked_once_before_opportunity(context_case, reverse):
    c = context_case
    assert hasattr(c.provider, "open_for_approval"), "缺少全决策人context lease"
    deciders = (c.actor_id, EmployeeId("emp_manager"))
    if reverse:
        deciders = tuple(reversed(deciders))
    async with c.provider.open_for_approval(
        c.tenant_id,
        c.opportunity_id,
        c.actor_id,
        prepared_by=c.prepared_by,
        decider_ids=deciders,
    ) as context:
        assert {item.employee_id for item in context.deciders} == set(deciders)
        async with asyncio.timeout(1):
            async with c.provider.open_for_approval(
                c.tenant_id,
                c.opportunity_id,
                c.actor_id,
                prepared_by=c.prepared_by,
                decider_ids=tuple(reversed(deciders)),
            ):
                pass
        writer = asyncio.create_task(
            c.update_employee(EmployeeId("emp_manager"), is_active=False)
        )
        await c.wait_for_blocked_writer(writer)
    await asyncio.wait_for(writer, 3)
