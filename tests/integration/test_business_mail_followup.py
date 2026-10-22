"""真实PG的当前授权、幂等重放、并发CAS与原件不变。"""

import asyncio
import pytest
from sqlalchemy import select, update
from tests.integration.test_business_mail import setup_mail
from domains.conversations.business_mail_followup import (
    BusinessMailDraftCommand,
    BusinessMailHandlingCommand,
    BusinessMailFollowupConflict,
)
from infra.db.business_mail_followup import SqlBusinessMailFollowupRepository
from infra.db.business_mail_tables import BusinessMailRow
from infra.db.tables import EmployeeRow
from shared.errors import PermissionDenied, ValidationError


async def prepared(engine):
    sessions, actor, binding, source, box, page = await setup_mail(engine)
    await source.apply_page(actor, await source.checkpoint(actor, box), page)
    message = (
        (await source.list(actor, bucket="all", offset=0, limit=50)).items[0].message_id
    )
    return (
        sessions,
        actor,
        binding,
        source,
        message,
        SqlBusinessMailFollowupRepository(source),
    )


async def test_followup_refresh_replay_and_original(integration_engine):
    sessions, actor, binding, source, message, repo = await prepared(integration_engine)
    before = await source.detail(actor, message)
    assert (await repo.get(actor, message)).revision == 0
    command = BusinessMailDraftCommand(
        expected_revision=0,
        subject="Re: Inquiry",
        body_text="Thank you. What quantity and destination?",
    )
    first = await repo.apply(actor, message, command, idempotency_key="draft-key-1")
    assert first.draft.source_sha256 == before.source.source_sha256
    second = await repo.apply(
        actor,
        message,
        BusinessMailHandlingCommand(
            expected_revision=1, status="waiting_customer", note="准备确认数量"
        ),
        idempotency_key="handle-key-1",
    )
    replay = await repo.apply(actor, message, command, idempotency_key="draft-key-1")
    assert replay == second == await repo.get(actor, message)
    assert len(replay.history) == 2 and replay.history[0].request_key == "draft-key-1"
    with pytest.raises(BusinessMailFollowupConflict):
        await repo.apply(
            actor,
            message,
            command.model_copy(update={"body_text": "Changed"}),
            idempotency_key="draft-key-1",
        )
    assert await source.detail(actor, message) == before
    binding.operation = "rotated"
    with pytest.raises(PermissionDenied):
        await repo.apply(actor, message, command, idempotency_key="draft-key-1")


async def test_followup_cas_and_revoke(integration_engine):
    sessions, actor, binding, source, message, repo = await prepared(integration_engine)
    command = BusinessMailHandlingCommand(
        expected_revision=0, status="completed", note="人工处理"
    )
    results = await asyncio.gather(
        *(
            repo.apply(actor, message, command, idempotency_key=f"concurrent-{i}")
            for i in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, BusinessMailFollowupConflict) for r in results) == 1
    assert len((await repo.get(actor, message)).history) == 1
    async with sessions.begin() as db:
        await db.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == actor.tenant_id,
                EmployeeRow.employee_id == actor.employee_id,
            )
            .values(is_active=False)
        )
    with pytest.raises(PermissionDenied):
        await repo.get(actor, message)


@pytest.mark.parametrize(
    "change", [{"tenant_id": "other"}, {"employee_id": "other"}, {"user_id": "other"}]
)
async def test_followup_identity_boundary(integration_engine, change):
    _, actor, _, _, message, repo = await prepared(integration_engine)
    with pytest.raises(PermissionDenied):
        await repo.get(actor.model_copy(update=change), message)


async def test_headers_only_no_draft(integration_engine):
    # 原件不可变触发器不能被测试绕过，构造合法headers_only页后持久化。
    sessions, actor, _, source, box, page = await setup_mail(integration_engine)
    item = page.messages[0].model_copy(
        update={"source_kind": "headers_only", "mime_sha256": None}
    )
    await source.apply_page(
        actor,
        await source.checkpoint(actor, box),
        page.model_copy(update={"messages": (item,)}),
    )
    message = (
        (await source.list(actor, bucket="all", offset=0, limit=50)).items[0].message_id
    )
    repo = SqlBusinessMailFollowupRepository(source)
    with pytest.raises(ValidationError):
        await repo.apply(
            actor,
            message,
            BusinessMailDraftCommand(
                expected_revision=0, subject="Hi", body_text="Hello"
            ),
            idempotency_key="headers-key",
        )
    assert (await repo.get(actor, message)).revision == 0

async def test_role_removal_denies_replay(integration_engine):
    sessions, actor, _, _, message, repo = await prepared(integration_engine)
    command = BusinessMailHandlingCommand(expected_revision=0, status='completed', note='处理')
    await repo.apply(actor, message, command, idempotency_key='role-replay')
    async with sessions.begin() as db:
        await db.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == actor.tenant_id, EmployeeRow.employee_id == actor.employee_id).values(role='viewer'))
    with pytest.raises(PermissionDenied):
        await repo.apply(actor, message, command, idempotency_key='role-replay')

async def test_boss_has_no_other_mailbox_privilege(integration_engine):
    sessions, actor, _, _, message, repo = await prepared(integration_engine)
    async with sessions.begin() as db:
        db.add(EmployeeRow(tenant_id=actor.tenant_id, employee_id='emp-other-boss', user_id='usr-other-boss', name='老板', role='boss', is_active=True))
    with pytest.raises(PermissionDenied):
        await repo.get(actor.model_copy(update={'employee_id':'emp-other-boss','user_id':'usr-other-boss'}), message)

async def test_immutable_history_and_draft(integration_engine):
    from sqlalchemy.exc import DBAPIError
    from infra.db.business_mail_followup_tables import BusinessMailFollowupEventRow, BusinessMailReplyDraftRow
    sessions, actor, _, _, message, repo = await prepared(integration_engine)
    result = await repo.apply(actor, message, BusinessMailDraftCommand(expected_revision=0,subject='Hi',body_text='Hello'), idempotency_key='immutable-key')
    for model, fields in ((BusinessMailFollowupEventRow, {'note':'changed'}),(BusinessMailReplyDraftRow, {'body_text':'changed'})):
        with pytest.raises(DBAPIError):
            async with sessions.begin() as db:
                await db.execute(update(model).where(model.tenant_id == actor.tenant_id, model.message_id == message).values(**fields))
    assert await repo.get(actor,message)==result
