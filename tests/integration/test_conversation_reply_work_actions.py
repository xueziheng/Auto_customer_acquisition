"""回复业务动作的 tenant-bound 耐久 owner queue。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def reply_work_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
):
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    impl_type = importlib.import_module(
        "domains.conversations.service_impl"
    ).ConversationServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=lambda: NOW),
        now=lambda: NOW,
    )


async def _message(service, tenant: TenantId, account: ProspectAccountId):
    return await service.ingest_inbound(
        tenant,
        None,
        account,
        "art_reply_work_metadata_only",
        f"<{new_id('msg')}@reply.invalid>",
        NOW,
    )


async def test_four_reply_actions_are_durable_idempotent_and_owner_visible(
    reply_work_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(reply_work_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    service = _service(factory, tenant)
    message_id = await _message(service, tenant, account)
    schemas = importlib.import_module("domains.conversations.schemas")
    action_type = schemas.ReplyWorkAction
    request_type = schemas.ReplyWorkActionRequest
    status_type = schemas.ReplyWorkStatus
    expected_queues = {
        "start_qualification": "need_qualification",
        "mark_future_restart": "future_restart_review",
        "create_follow_up": "follow_up",
        "intake_new_contact": "verified_contact_intake_review",
    }

    for action, expected_queue in expected_queues.items():
        request = request_type(
            message_id=message_id,
            outbound_message_id=OutboundMessageId(new_id("out")),
            enrollment_id=EnrollmentId(new_id("enr")),
            account_id=account,
            contact_point_id=ContactPointId(new_id("cp")),
            action=action_type(action),
            idempotency_key=IdempotencyKey(f"reply:{action}:{message_id}"),
        )
        first = await service.enqueue_reply_work_action(tenant, request)
        second = await service.enqueue_reply_work_action(tenant, request)
        assert first == second
        assert first.status.value == "pending"
        assert first.owner_queue.value == expected_queue

    queue = await service.list_reply_work_queue(
        tenant,
        status=status_type.PENDING,
        limit=10,
    )
    assert {item.action.value: item.owner_queue.value for item in queue} == expected_queues
    assert all(item.message_id == message_id for item in queue)
    assert all(
        set(vars(item)).isdisjoint({"body", "subject", "customer_verbatim"})
        for item in queue
    )
    assert await _service(factory, other_tenant).list_reply_work_queue(
        other_tenant,
        status=status_type.PENDING,
        limit=10,
    ) == ()

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.ConversationReplyWorkRow)
            .where(rows.ConversationReplyWorkRow.tenant_id == str(tenant))
        ) == 4


async def test_reply_work_retries_do_not_duplicate_and_conflicts_fail_closed(
    reply_work_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(reply_work_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    service = _service(factory, tenant)
    message_id = await _message(service, tenant, account)
    schemas = importlib.import_module("domains.conversations.schemas")
    action_type = schemas.ReplyWorkAction
    request_type = schemas.ReplyWorkActionRequest
    request = request_type(
        message_id=message_id,
        outbound_message_id=OutboundMessageId(new_id("out")),
        enrollment_id=EnrollmentId(new_id("enr")),
        account_id=account,
        contact_point_id=ContactPointId(new_id("cp")),
        action=action_type.CREATE_FOLLOW_UP,
        idempotency_key=IdempotencyKey(f"reply:create_follow_up:{message_id}"),
    )

    results = await asyncio.gather(
        *(service.enqueue_reply_work_action(tenant, request) for _ in range(12))
    )
    assert len({item.action_id for item in results}) == 1

    conflict = request_type(
        message_id=message_id,
        outbound_message_id=request.outbound_message_id,
        enrollment_id=request.enrollment_id,
        account_id=account,
        contact_point_id=request.contact_point_id,
        action=action_type.CREATE_FOLLOW_UP,
        idempotency_key=IdempotencyKey(f"reply:create_follow_up:{message_id}:changed"),
    )
    with pytest.raises(ValidationError, match="回复工作动作幂等冲突"):
        await service.enqueue_reply_work_action(tenant, conflict)
