"""审查专用窄探针；通过表示确认了隐式外键逆序死锁，不是功能验收通过。"""

import asyncio

from sqlalchemy import event, select

from domains.employees.models import OwnershipLock, OwnershipTransfer
from infra.db.repositories.employees import OwnershipRepositoryImpl
from infra.db.tables import EmployeeRow, OwnershipLockRow
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, new_id
from tests.integration.conftest import db_url as db_url  # noqa: PLC0414
from tests.integration.conftest import (
    integration_engine as integration_engine,  # noqa: PLC0414
)
from tests.integration.test_owner_handoff_reminders import NOW
from tests.integration.test_owner_handoff_reminders import (
    scenario as scenario,  # noqa: PLC0414
)


def _state(error):
    while error is not None:
        state = getattr(error, "sqlstate", None)
        if state:
            return state
        error = getattr(error, "orig", None) or error.__cause__
    return "unknown"


async def test_probe_detects_transfer_reminder_deadlock(scenario, integration_engine):
    factory, tenant, owner, opp, hand, service, _actor, system = scenario
    replacement = EmployeeId(new_id("emp"))
    async with factory.begin() as seed:
        seed.add(EmployeeRow(tenant_id=tenant, employee_id=replacement, name="合成新负责人", role="sales", is_active=True))
    owner_locked = asyncio.Event()
    transfer_updated = asyncio.Event()

    def after_execute(_conn, _cursor, statement, _params, _context, _many):
        if "FROM employees" in statement and "FOR UPDATE" in statement:
            owner_locked.set()

    event.listen(integration_engine.sync_engine, "after_cursor_execute", after_execute)

    async def transfer():
        try:
            async with factory.begin() as session:
                row = await session.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == tenant))
                await OwnershipRepositoryImpl(session, tenant).replace(
                    tenant,
                    OwnershipLock(tenant_id=tenant, account_id=ProspectAccountId(row.account_id), owner=replacement, locked_at=NOW, locked_by_rule="transfer"),
                    OwnershipTransfer(transfer_id=new_id("otr"), tenant_id=tenant, account_id=ProspectAccountId(row.account_id), from_owner=owner, to_owner=replacement, transferred_by=owner, transferred_at=NOW, reason="合成转移"),
                )
                transfer_updated.set()
                await asyncio.wait_for(owner_locked.wait(), 3)
            return "ok"
        except Exception as error:  # noqa: BLE001 - 受控探针只归类失败，不输出原文
            return _state(error)

    async def reminder():
        await asyncio.wait_for(transfer_updated.wait(), 3)
        try:
            async with service.handoff_notification_scope(tenant, hand, opp, owner, actor=system):
                pass
            return "ok"
        except Exception as error:  # noqa: BLE001 - 受控探针只归类失败，不输出原文
            return _state(error)

    try:
        states = await asyncio.wait_for(asyncio.gather(transfer(), reminder()), 8)
        assert "40P01" in states, "implicit_foreign_key_deadlock_not_reproduced"
    finally:
        event.remove(integration_engine.sync_engine, "after_cursor_execute", after_execute)
