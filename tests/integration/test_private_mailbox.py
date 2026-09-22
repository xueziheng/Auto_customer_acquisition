"""真实 PG：本人隔离、幂等页、分页与断点提交。"""

from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.gmail.mailbox import parse_message
from domains.conversations.mailbox import MailboxActor, MailboxService
from infra.db.mailbox import SqlMailboxRepository
from infra.db.tables import EmployeeRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.mailbox import MailboxPage
from tests.unit.test_gmail_mailbox import message


@pytest.mark.db
async def test_private_mailbox_atomic_pages_and_tenant_owner_isolation(
    integration_engine,
):
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = "ten_" + uuid4().hex[:20]
    owner, other = "emp_" + uuid4().hex[:20], "emp_" + uuid4().hex[:20]
    async with sessions() as db, db.begin():
        db.add_all(
            [
                EmployeeRow(
                    employee_id=eid,
                    user_id="usr_" + eid[4:],
                    tenant_id=tenant,
                    name="测试",
                    role="boss",
                    is_active=True,
                )
                for eid in (owner, other)
            ]
        )
    repo = SqlMailboxRepository(sessions)
    service = MailboxService(repo)
    actor = MailboxActor(tenant_id=tenant, employee_id=owner)
    mailbox = await repo.register(actor, "owner@example.com")
    assert mailbox == await repo.register(actor, "owner@example.com")
    start = await repo.checkpoint(actor, mailbox)
    msgs = [
        parse_message(message("a1")),
        parse_message(message("a2", ["INBOX", "UNREAD"])),
    ]
    page = MailboxPage(
        cursor="checkpoint-1", phase="catch_up", messages=msgs, full_scan_complete=True
    )
    await repo.apply_page(actor, start, page)
    with pytest.raises(ValidationError):
        await repo.apply_page(actor, start, page)
    assert (await service.mailboxes(actor))[0].message_count == 2
    threads = await service.threads(actor, mailbox, search="询价", label="SENT")
    assert threads.items[0].message_count == 2
    assert threads.items[0].unread
    one = await service.messages(actor, mailbox, "b1", limit=1)
    assert len(one.items) == 1 and one.next_offset == 1
    assert "raw" not in one.model_dump()["items"][0]
    assert (
        await service.messages(actor, mailbox, "b1", offset=1, limit=1)
    ).next_offset is None
    for denied in (
        MailboxActor(tenant_id=tenant, employee_id=other),
        MailboxActor(tenant_id="other-tenant", employee_id=owner),
    ):
        with pytest.raises(PermissionDenied):
            await service.messages(denied, mailbox, "b1")
    with pytest.raises(PermissionDenied):
        await repo.register(
            MailboxActor(tenant_id=tenant, employee_id=other), "owner@example.com"
        )
    await repo.failed(actor, mailbox, "rate_limited")
    assert (await repo.checkpoint(actor, mailbox)).cursor == "checkpoint-1"
    await repo.apply_page(
        actor,
        await repo.checkpoint(actor, mailbox),
        MailboxPage(cursor="checkpoint-2", phase="synced", messages=msgs),
    )
    assert (await service.mailboxes(actor))[0].message_count == 2
    assert (await service.mailboxes(actor))[0].failure_code is None
    await repo.apply_page(
        actor,
        await repo.checkpoint(actor, mailbox),
        MailboxPage(cursor="reset", phase="backfill", reset=True),
    )
    assert (await service.mailboxes(actor))[0].message_count == 2
    await repo.apply_page(
        actor,
        await repo.checkpoint(actor, mailbox),
        MailboxPage(
            cursor="end", phase="catch_up", full_scan_complete=True, messages=[msgs[0]]
        ),
    )
    assert (await service.mailboxes(actor))[0].message_count == 1
    async with sessions() as db, db.begin():
        await db.execute(
            update(EmployeeRow)
            .where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == owner)
            .values(user_id="usr-reassigned")
        )
    assert await service.mailboxes(actor) == []
    with pytest.raises(PermissionDenied):
        await service.messages(actor, mailbox, "b1")
    async with sessions() as db, db.begin():
        await db.execute(
            update(EmployeeRow)
            .where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == owner)
            .values(is_active=False)
        )
    with pytest.raises(PermissionDenied):
        await service.mailboxes(actor)


@pytest.mark.db
async def test_sync_uses_real_gateway_and_lock_and_persists_safe_failure(
    integration_engine,
):
    from apps.email_feedback_worker.mailbox import MailboxSync
    from connectors.gmail.mailbox import GmailMailboxReader
    from infra.db.advisory_lock import PostgresAdvisoryLock, derive_advisory_lock_key
    from shared.schemas.mailbox import MailboxFailure
    from tests.unit.test_gmail_mailbox import Provider
    from tool_gateway.fingerprint import HmacFingerprintProvider

    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant, owner = "ten_" + uuid4().hex[:20], "emp_" + uuid4().hex[:20]
    actor = MailboxActor(tenant_id=tenant, employee_id=owner)
    async with sessions() as db, db.begin():
        db.add(
            EmployeeRow(
                employee_id=owner,
                user_id="usr_" + owner[4:],
                tenant_id=tenant,
                name="测试",
                role="sales",
                is_active=True,
            )
        )
    repo = SqlMailboxRepository(sessions)
    mailbox = await repo.register(actor, "owner@example.com")
    lock = PostgresAdvisoryLock(
        integration_engine, derive_advisory_lock_key(actor.tenant_id, mailbox)
    )
    assert await lock.acquire()
    provider = Provider()
    sync = MailboxSync(
        repo,
        actor,
        mailbox,
        GmailMailboxReader(provider, "owner@example.com"),
        HmacFingerprintProvider("test-v1", b"test-only-mailbox-fingerprint-key-12345"),
        lock,
    )
    try:
        first = await sync.once()
        assert first.phase == "backfill"
        await sync.once()
        await sync.once()
        assert (await repo.mailboxes(actor))[0].message_count == 3
        checkpoint = await repo.checkpoint(actor, mailbox)

        async def failed(path, params):
            raise MailboxFailure("authorization_required")

        provider.get = failed
        with pytest.raises(MailboxFailure, match="authorization_required"):
            await sync.once()
        assert (await repo.checkpoint(actor, mailbox)).cursor == checkpoint.cursor
        assert (await repo.mailboxes(actor))[0].failure_code == "authorization_required"
    finally:
        await lock.close()
