"""conversations 消息持久化 + InboundMessageStored 事件（真实 PostgreSQL）。

TDD RED：0019 迁移/仓储/ingest_inbound 尚未实现，本文件导入即失败。

契约（HANDBOOK 切片 6「domains/conversations 消息、分类留痕」+ 收敛计划）：
- ingest_inbound：原文先落 artifact_store（上游契约，本片只存引用）→
  conversations get-or-create（tenant+account+channel=email，并发安全）→
  messages 落库（external_message_id 必填非空，UNIQUE(tenant, external)）→
  同事务发布 InboundMessageStored（metadata-only，不含 raw_artifact_ref/
  正文/地址/凭证）。
- 幂等/冲突：同 tenant 同 external_message_id 且语义完全一致（conversation/
  account、raw_artifact_ref、sent_at、outbound_message_id）→ 返回既有
  MessageId 且不再发事件；任一不一致 → ReingestConflictError fail-closed
  （固定安全摘要，不回显内容）。
- conversation_id 非 None：必须存在且 tenant/account/channel=email 匹配，
  否则 fail-closed；None 才 get-or-create。
- last_inbound_at = max(existing, sent_at)：迟到旧消息不回退。
- 精确匹配：external_message_id/outbound_message_id 含尖括号形态原样比较，
  禁止 normalize。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.conversations.service import ConversationService
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.conversations.models")
MessageDirection = _models.MessageDirection

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
#: 正文/凭证/ref marker：事件与表中都不得出现
BODY_MARKER = "please stop contacting us SECRET-MARKER-77"
REF_MARKER = "art_0123456789abcdefghjkmnpqrsvwxyz"
CRED_MARKER = "api_key=sk-test-77"

#: InboundMessageStored 事件契约键（禁止平行事件/多余字段）
_INBOUND_EVENT_KEYS = {
    "tenant_id",
    "occurred_at",
    "run_id",
    "message_id",
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


@pytest_asyncio.fixture
def tenant() -> TenantId:
    return TenantId(new_id("tn"))


@pytest_asyncio.fixture
def other_tenant() -> TenantId:
    return TenantId(new_id("tn"))


@pytest_asyncio.fixture
def account() -> ProspectAccountId:
    return ProspectAccountId(new_id("acc"))


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> ConversationService:
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    impl_type = importlib.import_module(
        "domains.conversations.service_impl"
    ).ConversationServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


async def _outbox_payloads(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[dict]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.OutboxEventRow).where(
                    tables.OutboxEventRow.tenant_id == str(tenant),
                    tables.OutboxEventRow.event_type == "InboundMessageStored",
                )
            )
        ).scalars().all()
    return [dict(row.event_payload) for row in rows]


async def _message_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.MessageRow).where(
                    tables.MessageRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def _conversation_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.ConversationRow).where(
                    tables.ConversationRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


def _assert_event_clean(payload: dict) -> None:
    """事件最小披露：仅契约键；无正文/凭证/raw_artifact_ref/地址泄漏。"""
    assert set(payload) == _INBOUND_EVENT_KEYS
    dump = json.dumps(payload)
    assert BODY_MARKER not in dump
    assert CRED_MARKER not in dump
    assert REF_MARKER not in dump
    assert "raw_artifact_ref" not in dump


async def test_ingest_creates_conversation_and_message_and_publishes_clean_event(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """None 会话 → get-or-create；消息全列落库；同事务发布干净事件。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    outbound = OutboundMessageId(
        "<route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid>"
    )
    message_id = await service.ingest_inbound(
        tenant,
        None,
        account,
        REF_MARKER,
        "<msg-id-001@example.test>",
        NOW,
        outbound_message_id=outbound,
    )
    assert isinstance(message_id, str) and message_id.startswith("msg_")

    messages = await _message_rows(factory, tenant)
    conversations = await _conversation_rows(factory, tenant)
    assert len(messages) == 1
    assert len(conversations) == 1
    row = messages[0]
    assert row.message_id == str(message_id)
    assert row.direction == "inbound"
    assert row.sent_at == NOW
    assert row.raw_artifact_ref == REF_MARKER  # 原样精确持久化
    assert row.external_message_id == "<msg-id-001@example.test>"
    assert row.outbound_message_id == str(outbound)
    assert row.conversation_id == str(conversations[0].conversation_id)
    conv = conversations[0]
    assert conv.account_id == str(account)
    assert conv.channel == "email"
    assert conv.last_inbound_at == NOW

    payloads = await _outbox_payloads(factory, tenant)
    assert len(payloads) == 1
    _assert_event_clean(payloads[0])
    assert payloads[0]["message_id"] == str(message_id)
    assert payloads[0]["outbound_message_id"] == str(outbound)


async def test_reingest_identical_external_id_returns_same_id_without_second_event(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """同 external_message_id 语义完全一致 → 同 MessageId，不再发事件。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    outbound = OutboundMessageId("<route-v1.abc@messages.tradeos.invalid>")
    first = await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<dup-001@example.test>", NOW,
        outbound_message_id=outbound,
    )
    again = await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<dup-001@example.test>", NOW,
        outbound_message_id=outbound,
    )
    assert again == first
    assert len(await _message_rows(factory, tenant)) == 1
    assert len(await _outbox_payloads(factory, tenant)) == 1


async def test_reingest_semantic_mismatch_fails_closed(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """同 external_message_id 但任一语义不一致 → 专用冲突错误，无新事件。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    conflict_type = importlib.import_module(
        "domains.conversations.errors"
    ).ReingestConflictError
    outbound = OutboundMessageId("<route-v1.abc@messages.tradeos.invalid>")
    await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<dup-002@example.test>", NOW,
        outbound_message_id=outbound,
    )
    # raw_artifact_ref 不一致
    with pytest.raises(conflict_type):
        await service.ingest_inbound(
            tenant, None, account, "art_different", "<dup-002@example.test>", NOW,
            outbound_message_id=outbound,
        )
    # sent_at 不一致
    with pytest.raises(conflict_type):
        await service.ingest_inbound(
            tenant, None, account, REF_MARKER, "<dup-002@example.test>",
            NOW + timedelta(hours=1), outbound_message_id=outbound,
        )
    # outbound_message_id 不一致（原 None 现非 None / 值不同）
    with pytest.raises(conflict_type):
        await service.ingest_inbound(
            tenant, None, account, REF_MARKER, "<dup-002@example.test>", NOW,
        )
    # account 不一致（不同账户 → conversation 不同 → 冲突）
    other_account = ProspectAccountId(new_id("acc"))
    with pytest.raises(conflict_type):
        await service.ingest_inbound(
            tenant, None, other_account, REF_MARKER, "<dup-002@example.test>", NOW,
            outbound_message_id=outbound,
        )
    # 错误消息不回显正文/凭证/ref
    assert len(await _message_rows(factory, tenant)) == 1
    assert len(await _outbox_payloads(factory, tenant)) == 1


async def test_explicit_conversation_id_must_exist_and_match(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """conversation_id 非 None：必须存在且 tenant/account/channel 匹配。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    # 先建一个会话
    message_id = await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<seed-003@example.test>", NOW,
    )
    conversations = await _conversation_rows(factory, tenant)
    existing_id = conversations[0].conversation_id

    # 不存在的 conversation_id → fail-closed
    with pytest.raises(ValidationError):
        await service.ingest_inbound(
            tenant, ConversationId(new_id("con")), account,
            REF_MARKER, "<new-003@example.test>", NOW,
        )
    # 存在但 account 不匹配 → fail-closed
    other_account = ProspectAccountId(new_id("acc"))
    with pytest.raises(ValidationError):
        await service.ingest_inbound(
            tenant, ConversationId(existing_id), other_account,
            REF_MARKER, "<new-003@example.test>", NOW,
        )
    # 匹配 → 复用同一会话，不新建
    second = await service.ingest_inbound(
        tenant, ConversationId(existing_id), account,
        REF_MARKER, "<new-003@example.test>", NOW + timedelta(minutes=5),
    )
    assert second != message_id
    conversations_after = await _conversation_rows(factory, tenant)
    assert len(conversations_after) == 1
    assert conversations_after[0].last_inbound_at == NOW + timedelta(minutes=5)


async def test_last_inbound_at_is_max_never_regresses(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """迟到旧消息不回退 last_inbound_at。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<late-004@example.test>",
        NOW + timedelta(hours=2),
    )
    await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<late-004b@example.test>",
        NOW,  # 更早的迟到消息
    )
    conversations = await _conversation_rows(factory, tenant)
    assert len(conversations) == 1
    assert conversations[0].last_inbound_at == NOW + timedelta(hours=2)


async def test_blank_or_invalid_inputs_fail_closed(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """空白 external_message_id/raw_artifact_ref、非 UTC sent_at → 拒绝。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    with pytest.raises(ValidationError):
        await service.ingest_inbound(
            tenant, None, account, REF_MARKER, "   ", NOW,
        )
    with pytest.raises(ValidationError):
        await service.ingest_inbound(
            tenant, None, account, "", "<blank-ref@example.test>", NOW,
        )
    with pytest.raises(ValidationError):
        await service.ingest_inbound(
            tenant, None, account, REF_MARKER, "<naive@example.test>",
            NOW.replace(tzinfo=None),  # naive
        )
    assert len(await _message_rows(factory, tenant)) == 0


async def test_external_message_id_is_exact_no_normalize(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """尖括号形态原样精确比较：<a@b> 与 a@b 是不同 external id。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    first = await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<exact-005@example.test>", NOW,
    )
    second = await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "exact-005@example.test", NOW,
    )
    assert first != second
    assert len(await _message_rows(factory, tenant)) == 2


async def test_cross_tenant_isolation_fails_closed(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId, other_tenant: TenantId,
) -> None:
    """跨租户：A 的数据在 B 的仓储视角不存在（find 返回 None，不抛泄漏）。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<iso-006@example.test>", NOW,
    )
    other_service = _service(factory, other_tenant, clock)
    other_id = await other_service.ingest_inbound(
        other_tenant, None, account, REF_MARKER, "<iso-006b@example.test>", NOW,
    )
    assert other_id.startswith("msg_")
    assert len(await _message_rows(factory, tenant)) == 1
    assert len(await _message_rows(factory, other_tenant)) == 1
    assert len(await _outbox_payloads(factory, tenant)) == 1
    assert len(await _outbox_payloads(factory, other_tenant)) == 1


async def test_concurrent_ingest_same_external_id_exactly_once(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """并发同 external_message_id：恰一行、恰一条事件、两调用同 MessageId。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    async def ingest() -> str:
        return await service.ingest_inbound(
            tenant, None, account, REF_MARKER, "<conc-007@example.test>", NOW,
            outbound_message_id=OutboundMessageId(
                "<route-v1.conc@messages.tradeos.invalid>"
            ),
        )

    first, second = await asyncio.gather(ingest(), ingest())
    assert first == second
    assert len(await _message_rows(factory, tenant)) == 1
    assert len(await _outbox_payloads(factory, tenant)) == 1


async def test_concurrent_ingest_same_conversation_different_messages(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """并发不同 external id 同账户：恰一个 conversation，两条消息。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    async def ingest(n: int) -> str:
        return await service.ingest_inbound(
            tenant, None, account, REF_MARKER, f"<conc-008-{n}@example.test>", NOW,
        )

    await asyncio.gather(ingest(1), ingest(2))
    assert len(await _conversation_rows(factory, tenant)) == 1
    assert len(await _message_rows(factory, tenant)) == 2


async def test_advance_last_inbound_at_is_monotonic_under_stale_read_interleaving(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """P2 受控交错（确定性，非概率）：T2 先读到旧 last_inbound_at（stale），
    T1 独立事务推进到更新的 sent_at 并提交；T2 随后按旧值推进较旧 sent_at
    并提交 —— 单调 UPDATE（GREATEST/COALESCE）必须保持 max，不回退。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<mono-009@example.test>", NOW,
    )
    conv_rows = await _conversation_rows(factory, tenant)
    assert len(conv_rows) == 1
    conv_id = ConversationId(conv_rows[0].conversation_id)

    repo_type = importlib.import_module(
        "infra.db.repositories.conversations"
    ).ConversationRepositoryImpl

    # T2 先读：stale 快照（last_inbound_at == NOW）
    async with factory() as session_t2:
        repo_t2 = repo_type(session_t2, tenant)
        stale = await repo_t2.get(tenant, conv_id)
        assert stale is not None and stale.last_inbound_at == NOW
        # T1 独立事务推进到 NOW+2h 并提交
        async with factory() as session_t1:
            repo_t1 = repo_type(session_t1, tenant)
            await repo_t1.advance_last_inbound_at(
                tenant, conv_id, NOW + timedelta(hours=2)
            )
            await session_t1.commit()
        # T2 用旧值推进较旧 sent_at（NOW+1h）并提交
        await repo_t2.advance_last_inbound_at(
            tenant, conv_id, NOW + timedelta(hours=1)
        )
        await session_t2.commit()

    after = (await _conversation_rows(factory, tenant))[0]
    assert after.last_inbound_at == NOW + timedelta(hours=2)


async def test_concurrent_ingest_different_sent_at_keeps_max(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """P2 服务层不变式：并发不同 sent_at 入站，最终 last_inbound_at == max。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    async def ingest(n: int) -> str:
        sent = NOW + timedelta(hours=n)
        return await service.ingest_inbound(
            tenant, None, account, REF_MARKER,
            f"<mono-010-{n}@example.test>", sent,
        )

    await asyncio.gather(ingest(1), ingest(2))
    convs = await _conversation_rows(factory, tenant)
    assert len(convs) == 1
    assert convs[0].last_inbound_at == NOW + timedelta(hours=2)


async def test_message_repository_rejects_blank_external_message_id(
    conversations_db: AsyncEngine, tenant: TenantId, account: ProspectAccountId,
) -> None:
    """P3 仓储边界 fail-closed：external_message_id 为 None/空白时抛固定摘要
    领域/验证错误（不回显值），不得以 "" 撞 DB CHECK 产生未分类 IntegrityError。"""
    factory = async_sessionmaker(conversations_db, expire_on_commit=False)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    await service.ingest_inbound(
        tenant, None, account, REF_MARKER, "<seed-011@example.test>", NOW,
    )
    conv = (await _conversation_rows(factory, tenant))[0]
    models = importlib.import_module("domains.conversations.models")
    repo_type = importlib.import_module(
        "infra.db.repositories.conversations"
    ).MessageRepositoryImpl

    async with factory() as session:
        repo = repo_type(session, tenant)
        for bad_value in (None, "   "):
            bad = models.Message(
                message_id=MessageId(new_id("msg")),
                tenant_id=tenant,
                conversation_id=ConversationId(conv.conversation_id),
                direction=models.MessageDirection.INBOUND,
                sent_at=NOW,
                raw_artifact_ref=REF_MARKER,
                external_message_id=bad_value,
            )
            with pytest.raises(ValidationError, match="external Message-ID"):
                await repo.add(bad)
        await session.rollback()
    assert len(await _message_rows(factory, tenant)) == 1  # 仅 seed，无坏行
