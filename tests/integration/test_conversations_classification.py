"""conversations 域分类留痕垂直切片（真实 PostgreSQL）。

TDD RED：``MessageClassification`` / ``ClassificationRepository`` /
``record_classification`` 服务实现尚不存在，本文件导入即失败。

契约（domains/conversations/AGENTS.md + service.py + 复审要求）：
- ``record_classification`` 落分类：message/source 引用（message_id）、
  classified_at、classified_by（=模型版本留痕，供按版本评估）；不存任何
  数值置信度；
- 返回 ``REPLY_ACTIONS`` 动作序列（动作决定在域，执行在工作流）；
- 发布 ``ReplyReceived``（共享事件契约，不带正文）——**同一 tenant+入站
  message 至多发布一次**（即使 record_classification 重试）；AUTO_REPLY
  永不发布；
- 幂等契约：同 (tenant, message, classified_by) 同类别 → 幂等 no-op（不重复
  落库/发布）；同 (tenant, message, classified_by) 不同类别 → 冲突 fail
  closed（拒绝覆盖）；**跨 model_version 重评 = 显式契约**：本切片显式拒绝
  （ValidationError，不静默幂等/覆盖；未来由显式 reclassify API 承担）；
- 每次查询显式 tenant 过滤。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.conversations.schemas import ReplyCategory
from shared.errors import ValidationError
from shared.schemas.identifiers import MessageId, TenantId, new_id

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

#: shared/events/catalog.ReplyReceived 的字段（事件契约，禁止自造平行事件）
_REPLY_EVENT_KEYS = {
    "tenant_id",
    "occurred_at",
    "run_id",
    "message_id",
    "conversation_id",
    "reply_category",
    "outbound_message_id",
}


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def conversations_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> object:
    module = __import__(
        "domains.conversations.service_impl", fromlist=["ConversationsServiceImpl"]
    )
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    return module.ConversationsServiceImpl(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


async def _outbox_events(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.OutboxEventRow).where(
                    tables.OutboxEventRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def _classification_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.ConversationClassificationRow).where(
                    tables.ConversationClassificationRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def test_record_classification_persists_with_provenance_and_publishes_contract_event(
    conversations_db: AsyncEngine,
) -> None:
    """落库留痕：message 引用 + classified_at + classified_by（模型版本）；事件用
    shared ReplyReceived 契约、不带正文。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_001")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    actions = await service.record_classification(  # type: ignore[attr-defined]
        tenant, message_id, ReplyCategory.UNSUBSCRIBE, classified_by="test-model-v1"
    )
    assert actions == ("stop_sequence", "suppress")  # 动作由域 REPLY_ACTIONS 决定

    rows = await _classification_rows(factory, tenant)
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == str(tenant)
    assert row.message_id == str(message_id)
    assert row.category == "unsubscribe"
    assert row.classified_by == "test-model-v1"
    assert row.classified_at == NOW

    replies = [e for e in await _outbox_events(factory, tenant) if e.event_type == "ReplyReceived"]
    assert len(replies) == 1
    payload = dict(replies[0].event_payload)
    assert set(payload) <= _REPLY_EVENT_KEYS  # 只用共享事件契约字段
    assert payload["message_id"] == str(message_id)
    assert payload["reply_category"] == "unsubscribe"
    assert "body" not in json.dumps(payload)
    assert "subject" not in json.dumps(payload)


async def test_same_message_and_version_is_idempotent_and_conflict_fails_closed(
    conversations_db: AsyncEngine,
) -> None:
    """同 (tenant, message, classified_by)：同类别幂等（不重复落库/发布）；
    不同类别 → 冲突 fail closed（拒绝覆盖，原记录不动）。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_002")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    first = await service.record_classification(  # type: ignore[attr-defined]
        tenant, message_id, ReplyCategory.COMPLAINT, classified_by="test-model-v1"
    )
    second = await service.record_classification(  # type: ignore[attr-defined]
        tenant, message_id, ReplyCategory.COMPLAINT, classified_by="test-model-v1"
    )
    assert first == second == ("stop_sequence", "suppress", "record_complaint")
    assert len(await _classification_rows(factory, tenant)) == 1
    replies = [
        e for e in await _outbox_events(factory, tenant) if e.event_type == "ReplyReceived"
    ]
    assert len(replies) == 1  # 至多发布一次

    with pytest.raises(ValidationError):
        await service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, ReplyCategory.REJECTION, classified_by="test-model-v1"
        )
    rows = await _classification_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].category == "complaint"  # 原记录不被覆盖


async def test_cross_model_version_re_evaluation_is_explicitly_rejected(
    conversations_db: AsyncEngine,
) -> None:
    """跨 model_version 重评 = 显式契约：本切片**拒绝**不同 classified_by 的重评
    （显式 ValidationError，不静默幂等、不静默覆盖；未来由显式 reclassify API
    承担——HANDBOOK/现有 schema 无跨版本重评的历史要求）。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_006")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    await service.record_classification(  # type: ignore[attr-defined]
        tenant, message_id, ReplyCategory.UNSUBSCRIBE, classified_by="test-model-v1"
    )
    with pytest.raises(ValidationError):
        await service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, ReplyCategory.REJECTION, classified_by="test-model-v2"
        )
    rows = await _classification_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].classified_by == "test-model-v1"  # 原记录不被覆盖
    replies = [
        e for e in await _outbox_events(factory, tenant) if e.event_type == "ReplyReceived"
    ]
    assert len(replies) == 1  # 事件不因被拒重评而重复


async def test_auto_reply_never_publishes(
    conversations_db: AsyncEngine,
) -> None:
    """AUTO_REPLY 永不发布 ReplyReceived（自动回复不算回复）；重试仍不发布。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_003")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    actions = await service.record_classification(  # type: ignore[attr-defined]
        tenant, message_id, ReplyCategory.AUTO_REPLY, classified_by="test-model-v1"
    )
    assert actions == ()
    await service.record_classification(  # type: ignore[attr-defined]
        tenant, message_id, ReplyCategory.AUTO_REPLY, classified_by="test-model-v1"
    )
    rows = await _classification_rows(factory, tenant)
    assert len(rows) == 1  # 分类留痕（可评估），重试不重复落库
    assert [
        e.event_type for e in await _outbox_events(factory, tenant)
    ] == []  # AUTO_REPLY 永不发布


async def test_concurrent_same_category_and_version_records_exactly_once(
    conversations_db: AsyncEngine,
) -> None:
    """并发同 (message, 版本, 类别)：asyncio.gather 双写 → 一行、ReplyReceived
    至多一次（advisory 串行化 + message 唯一约束，不依赖单线程）。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_007")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    results = await asyncio.gather(
        service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, ReplyCategory.UNSUBSCRIBE, classified_by="test-model-v1"
        ),
        service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, ReplyCategory.UNSUBSCRIBE, classified_by="test-model-v1"
        ),
        return_exceptions=True,
    )
    assert all(not isinstance(item, BaseException) for item in results)
    rows = await _classification_rows(factory, tenant)
    assert len(rows) == 1
    replies = [
        e for e in await _outbox_events(factory, tenant) if e.event_type == "ReplyReceived"
    ]
    assert len(replies) == 1  # exactly-once


async def test_concurrent_different_versions_one_fails_explicitly(
    conversations_db: AsyncEngine,
) -> None:
    """并发跨版本重评：一方显式 ValidationError（不双写、不静默幂等），
    一行、事件至多一次。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_008")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    results = await asyncio.gather(
        service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, ReplyCategory.UNSUBSCRIBE, classified_by="test-model-v1"
        ),
        service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, ReplyCategory.REJECTION, classified_by="test-model-v2"
        ),
        return_exceptions=True,
    )
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) == 1
    assert isinstance(failures[0], ValidationError)
    rows = await _classification_rows(factory, tenant)
    assert len(rows) == 1
    replies = [
        e for e in await _outbox_events(factory, tenant) if e.event_type == "ReplyReceived"
    ]
    assert len(replies) == 1


async def test_other_tenant_is_isolated(
    conversations_db: AsyncEngine,
) -> None:
    """硬边界 8：同 message_id 跨租户互不可见。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_004")
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    _service_b = _service(factory, tenant_b, clock)

    await service_a.record_classification(  # type: ignore[attr-defined]
        tenant_a, message_id, ReplyCategory.REJECTION, classified_by="test-model-v1"
    )
    assert await _classification_rows(factory, tenant_b) == []
    assert len(await _classification_rows(factory, tenant_a)) == 1


async def test_unknown_category_is_rejected(
    conversations_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId("msg_class_005")
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    with pytest.raises(ValidationError):
        await service.record_classification(  # type: ignore[attr-defined]
            tenant, message_id, "maybe_interesting", classified_by="test-model-v1"
        )
    assert await _classification_rows(factory, tenant) == []
