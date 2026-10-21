"""真实仓储验证人员记录不冒充客户确认，且重试、转派不能改写历史。"""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.conversations.schemas import (
    ReplyWorkAction,
    ReplyWorkActionRequest,
    ReplyWorkStatus,
)
from domains.conversations.service import InboxActor, InboxScope
from infra.db.tables import EmployeeRow, OwnershipLockRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)
from tests.integration.test_conversation_reply_work_actions import (
    NOW,
    _message,
    _service,
)


async def owned_work(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant, owner = TenantId(new_id("tn")), EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    async with factory.begin() as session:
        session.add(EmployeeRow(employee_id=owner, tenant_id=tenant, name="测试负责人",
                                role="sales", is_active=True))
        session.add(OwnershipLockRow(lock_id=new_id("own"), tenant_id=tenant,
            account_id=account, owner=owner, locked_at=NOW, locked_by_rule="fixture"))
    service = _service(factory, tenant)
    message = await _message(service, tenant, account)
    work = await service.enqueue_reply_work_action(tenant, ReplyWorkActionRequest(
        message_id=message, outbound_message_id=OutboundMessageId(new_id("out")),
        enrollment_id=EnrollmentId(new_id("enr")), account_id=account,
        contact_point_id=ContactPointId(new_id("cp")),
        action=ReplyWorkAction.START_QUALIFICATION,
        idempotency_key=IdempotencyKey(f"reply:start_qualification:{message}"),
    ))
    actor = InboxActor(tenant, owner, "sales", InboxScope.SELF, frozenset({owner}))
    return factory, tenant, actor, service, work


async def test_waiting_record_remains_open_and_completion_history_survives_reload(integration_engine):
    _, tenant, actor, service, work = await owned_work(integration_engine)
    waiting = await service.handle_reply_work(
        tenant, work.action_id, actor=actor, expected_status=ReplyWorkStatus.PENDING,
        status=ReplyWorkStatus.IN_PROGRESS, disposition="waiting_customer",
        note="测试：客户尚未给出数量，等待补充。", request_id="waiting-first",
    )
    assert waiting.status is ReplyWorkStatus.IN_PROGRESS
    assert len(waiting.history) == 1
    assert waiting.history[0].disposition == "waiting_customer"
    assert waiting.history[0].handled_by == actor.employee_id
    assert waiting.history[0].handled_at == NOW
    assert waiting.message_id == work.message_id
    done = await service.handle_reply_work(
        tenant, work.action_id, actor=actor, expected_status=ReplyWorkStatus.IN_PROGRESS,
        status=ReplyWorkStatus.COMPLETED, disposition="handled",
        note="测试：已核对现有资料；本次处理完成，不代表确认采购。",
        request_id="completed-first",
    )
    assert done.status is ReplyWorkStatus.COMPLETED
    history = (await service.list_owned_reply_work(
        tenant, actor=actor, status=ReplyWorkStatus.COMPLETED, limit=10,
    ))[0].history
    assert [item.disposition for item in history] == ["waiting_customer", "handled"]
    assert history[0].note == "测试：客户尚未给出数量，等待补充。"


async def test_concurrent_same_request_is_once_and_changed_replay_is_rejected(integration_engine):
    factory, tenant, actor, service, work = await owned_work(integration_engine)
    command = {"actor": actor, "expected_status": ReplyWorkStatus.PENDING,
        "status": ReplyWorkStatus.IN_PROGRESS, "disposition": "waiting_customer",
        "note": "测试：等待规格。", "request_id": "repeat-waiting"}
    results = await asyncio.gather(*(
        service.handle_reply_work(tenant, work.action_id, **command) for _ in range(4)
    ))
    assert all(len(result.history) == 1 for result in results)
    with pytest.raises(ValidationError):
        await service.handle_reply_work(tenant, work.action_id, **{**command, "note": "改写原记录"})
    with pytest.raises(ValidationError):
        await service.handle_reply_work(tenant, work.action_id, **{**command, "request_id": "stale-request"})
    from infra.db.tables import ConversationReplyWorkUpdateRow
    async with factory() as session:
        rows = (await session.scalars(select(ConversationReplyWorkUpdateRow).where(
            ConversationReplyWorkUpdateRow.tenant_id == tenant,
        ))).all()
        assert len(rows) == 1


async def test_missing_result_cannot_complete_and_transfer_revokes_even_replay(integration_engine):
    factory, tenant, actor, service, work = await owned_work(integration_engine)
    with pytest.raises(ValidationError):
        await service.handle_reply_work(tenant, work.action_id, actor=actor,
            expected_status=ReplyWorkStatus.PENDING, status=ReplyWorkStatus.COMPLETED)
    command = {"actor": actor, "expected_status": ReplyWorkStatus.PENDING,
        "status": ReplyWorkStatus.IN_PROGRESS, "disposition": "waiting_customer",
        "note": "测试：等待确认。", "request_id": "transfer-waiting"}
    await service.handle_reply_work(tenant, work.action_id, **command)
    other = EmployeeId(new_id("emp"))
    async with factory.begin() as session:
        session.add(EmployeeRow(employee_id=other, tenant_id=tenant, name="新负责人",
                                role="sales", is_active=True))
        await session.execute(update(OwnershipLockRow).where(
            OwnershipLockRow.tenant_id == tenant, OwnershipLockRow.account_id == work.account_id,
        ).values(owner=other))
    with pytest.raises(PermissionDenied):
        await service.handle_reply_work(tenant, work.action_id, **command)
    assert await service.list_owned_reply_work(
        tenant, actor=actor, status=ReplyWorkStatus.IN_PROGRESS, limit=10,
    ) == ()
    wrong_tenant = TenantId(new_id("tn"))
    wrong_actor = InboxActor(wrong_tenant, actor.employee_id, "sales", InboxScope.SELF,
                             frozenset({actor.employee_id}))
    with pytest.raises(PermissionDenied):
        await service.handle_reply_work(wrong_tenant, work.action_id,
            **{**command, "actor": wrong_actor})


@pytest.mark.parametrize("initial", [ReplyWorkStatus.PENDING, ReplyWorkStatus.IN_PROGRESS])
async def test_blocked_parallel_replays_serialize_before_reading_history(integration_engine, initial):
    factory, tenant, actor, service, work = await owned_work(integration_engine)
    if initial is ReplyWorkStatus.IN_PROGRESS:
        await service.handle_reply_work(tenant, work.action_id, actor=actor,
            expected_status=ReplyWorkStatus.PENDING, status=ReplyWorkStatus.IN_PROGRESS)
    from infra.db.tables import ConversationReplyWorkRow
    tasks = []
    async with factory() as lock:
        await lock.execute(select(ConversationReplyWorkRow).where(
            ConversationReplyWorkRow.tenant_id == tenant,
            ConversationReplyWorkRow.action_id == work.action_id,
        ).with_for_update())
        try:
            tasks = [asyncio.create_task(service.handle_reply_work(
                tenant, work.action_id, actor=actor, expected_status=initial,
                status=ReplyWorkStatus.IN_PROGRESS, disposition="waiting_customer",
                note="测试：同一保存请求并发到达。", request_id="locked-parallel-request",
            )) for _ in range(4)]
            blocked = 0
            for _ in range(100):
                async with integration_engine.connect() as db:
                    blocked = await db.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock'"
                    ))
                if blocked >= 4:
                    break
                await asyncio.sleep(0.02)
            assert blocked >= 4, "四个处理连接应均被已持有的工作项行锁阻挡"
        finally:
            await lock.rollback()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(not isinstance(result, BaseException) for result in results)
    expected = 1 if initial is ReplyWorkStatus.PENDING else 2
    assert all(len(result.history) == expected for result in results)


async def test_transfer_between_list_and_history_cannot_reveal_new_owner_note(integration_engine, monkeypatch):
    from infra.db.repositories.conversations import ReplyWorkRepositoryImpl

    factory, tenant, actor, service, work = await owned_work(integration_engine)
    other = EmployeeId(new_id("emp"))
    native = ReplyWorkRepositoryImpl.list_owned

    async def transfer_after_snapshot(repository, *args, **kwargs):
        records = await native(repository, *args, **kwargs)
        async with factory.begin() as session:
            session.add(EmployeeRow(employee_id=other, tenant_id=tenant, name="新负责人",
                                    role="sales", is_active=True))
            await session.execute(update(OwnershipLockRow).where(
                OwnershipLockRow.tenant_id == tenant,
                OwnershipLockRow.account_id == work.account_id,
            ).values(owner=other))
        new_actor = InboxActor(tenant, other, "sales", InboxScope.SELF, frozenset({other}))
        await service.handle_reply_work(tenant, work.action_id, actor=new_actor,
            expected_status=ReplyWorkStatus.PENDING, status=ReplyWorkStatus.IN_PROGRESS,
            disposition="waiting_customer", note="新负责人处理说明，旧负责人不能读取。",
            request_id="new-owner-note")
        return records

    monkeypatch.setattr(ReplyWorkRepositoryImpl, "list_owned", transfer_after_snapshot)
    with pytest.raises(PermissionDenied):
        await service.list_owned_reply_work(
            tenant, actor=actor, status=ReplyWorkStatus.PENDING, limit=10,
        )


async def test_recent_completion_of_old_work_precedes_newer_legacy_history(integration_engine):
    from infra.db.tables import ConversationReplyWorkRow

    factory, tenant, actor, service, old = await owned_work(integration_engine)
    message = await _message(service, tenant, old.account_id)
    newer = await service.enqueue_reply_work_action(tenant, ReplyWorkActionRequest(
        message_id=message, outbound_message_id=OutboundMessageId(new_id("out")),
        enrollment_id=EnrollmentId(new_id("enr")), account_id=old.account_id,
        contact_point_id=ContactPointId(new_id("cp")), action=ReplyWorkAction.START_QUALIFICATION,
        idempotency_key=IdempotencyKey(f"reply:start_qualification:{message}"),
    ))
    async with factory.begin() as session:
        await session.execute(update(ConversationReplyWorkRow).where(
            ConversationReplyWorkRow.tenant_id == tenant,
            ConversationReplyWorkRow.action_id == old.action_id,
        ).values(created_at=NOW - timedelta(days=30)))
        await session.execute(update(ConversationReplyWorkRow).where(
            ConversationReplyWorkRow.tenant_id == tenant,
            ConversationReplyWorkRow.action_id == newer.action_id,
        ).values(created_at=NOW - timedelta(days=1), status="completed"))
    await service.handle_reply_work(tenant, old.action_id, actor=actor,
        expected_status=ReplyWorkStatus.PENDING, status=ReplyWorkStatus.COMPLETED,
        disposition="handled", note="今天完成旧事项。", request_id="old-work-completed")
    recent = await service.list_owned_reply_work(
        tenant, actor=actor, status=ReplyWorkStatus.COMPLETED, limit=1,
    )
    assert recent[0].action_id == old.action_id
    legacy = (await service.list_owned_reply_work(
        tenant, actor=actor, status=ReplyWorkStatus.COMPLETED, limit=10,
    ))[1]
    assert legacy.history == ()
