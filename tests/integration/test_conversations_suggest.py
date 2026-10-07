"""conversations 下一问选择服务集成（阶段 2；真实 PostgreSQL + 真实 UoW + 真实仓储）。

TDD RED：``ConversationServiceImpl.suggest_next_questions`` 尚未实现，
本文件所有测试先以 AttributeError（方法缺失）失败——预期失败原因是缺实现，
不是 fixture/import/环境错误。

契约（plan 2026-08-16-next-question-selection + service.py docstring）：
- missing_fields 顺序是上游优先级契约：稳定去重保留首次出现、只取前 2；
  本域不做白名单/不发明业务优先级
- 空 missing_fields 合法 → topics=[]（无缺失即无追问）
- completeness 是 0–5 确定性等级：必须 int 且非 bool；0/5 合法
- tenant_id/conversation_id：str、strip 非空、<=32；所有输入校验在开 UoW 前
- 即使 missing_fields 为空也检查 tenant 绑定会话存在；不存在/跨租户不可见
  → 统一 ValidationError("会话不存在")
- 只读：不发布事件、不写 outbox、不写日志
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.conversations.service import ConversationService
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ConversationId,
    ProspectAccountId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.conversations.models")

NOW = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    value: datetime
    calls: int = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


class _SubList(list):
    """list 子类：契约要求 ``type(...) is list`` 精确匹配，子类必须被拒绝。"""


@pytest_asyncio.fixture
async def suggest_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


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


async def _create_conversation(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> ConversationId:
    """真实仓储经真实 UoW 直接建会话（无事件噪声）。"""
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    conversation_id = ConversationId(new_id("con"))
    account_id = ProspectAccountId(new_id("acct"))
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.conversations.add(
            _models.Conversation(
                conversation_id=conversation_id,
                tenant_id=tenant,
                account_id=account_id,
                channel="email",
                created_at=NOW,
                last_inbound_at=NOW,
            )
        )
    return conversation_id


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


async def _counts(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> tuple[int, int]:
    """(conversation 行数, message 行数)，按 tenant 过滤。"""
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        conversations = (
            await session.execute(
                select(tables.ConversationRow).where(
                    tables.ConversationRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        messages = (
            await session.execute(
                select(tables.MessageRow).where(
                    tables.MessageRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return len(list(conversations)), len(list(messages))


async def test_returns_first_two_deduped_in_upstream_order(
    suggest_db: AsyncEngine,
) -> None:
    """稳定去重保留上游首次出现序，取前 2；reason 精确含完整度与两主题。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    suggestion = await service.suggest_next_questions(
        tenant, conversation_id, ["quantity", "quantity", "destination", "material"], 3
    )
    assert suggestion.topics == ["quantity", "destination"]
    assert suggestion.reason == (
        "完整度 3/5，缺失字段（按上游顺序取前 2）：quantity、destination"
    )


async def test_truncates_to_two(suggest_db: AsyncEngine) -> None:
    """4 个不同字段 → 恰前 2（不发明优先级，纯截断）。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    suggestion = await service.suggest_next_questions(
        tenant,
        conversation_id,
        ["material", "size_spec", "quantity", "destination"],
        2,
    )
    assert suggestion.topics == ["material", "size_spec"]
    assert suggestion.reason == (
        "完整度 2/5，缺失字段（按上游顺序取前 2）：material、size_spec"
    )


async def test_empty_missing_fields_returns_no_topics(suggest_db: AsyncEngine) -> None:
    """空缺失字段合法 → topics=[] + 固定 reason；仍校验会话存在。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    suggestion = await service.suggest_next_questions(
        tenant, conversation_id, [], 5
    )
    assert suggestion.topics == []
    assert suggestion.reason == "无缺失字段，无需追问"


async def test_completeness_boundaries_0_and_5(suggest_db: AsyncEngine) -> None:
    """完整度 0 与 5 均合法；reason 正确反映等级。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    low = await service.suggest_next_questions(tenant, conversation_id, ["quantity"], 0)
    assert low.reason == "完整度 0/5，缺失字段（按上游顺序取前 2）：quantity"
    high = await service.suggest_next_questions(tenant, conversation_id, [], 5)
    assert high.reason == "无缺失字段，无需追问"


async def test_rejects_invalid_completeness(suggest_db: AsyncEngine) -> None:
    """非 int / bool / 越界 → 固定 ValidationError（bool 是 int 子类，显式拒绝）。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    for label, value in [
        ("bool True", True),
        ("bool False", False),
        ("float", 2.5),
        ("str", "3"),
        ("越界 -1", -1),
        ("越界 6", 6),
    ]:
        with pytest.raises(ValidationError) as exc_info:
            await service.suggest_next_questions(
                tenant, conversation_id, ["quantity"], value  # type: ignore[arg-type]
            )
        assert str(exc_info.value) == "完整度必须为 0–5 的整数", label


async def test_rejects_invalid_missing_fields(suggest_db: AsyncEngine) -> None:
    """非精确 list（含 list 子类）/ 非 str / 空白 / 元素首尾空白 →
    固定 ValidationError。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    invalid_cases: list[tuple[str, object, str]] = [
        ("str 而非 list", "quantity", "缺失字段必须是列表"),
        ("tuple 而非 list", ("quantity",), "缺失字段必须是列表"),
        ("list 子类", _SubList(["quantity"]), "缺失字段必须是列表"),
        ("元素非 str", [123], "缺失字段无效"),
        ("空串元素", ["", "quantity"], "缺失字段无效"),
        ("全空白元素", ["   "], "缺失字段无效"),
        ("元素首尾空白", [" quantity "], "缺失字段无效"),
    ]
    for label, missing_fields, expected in invalid_cases:
        with pytest.raises(ValidationError) as exc_info:
            await service.suggest_next_questions(
                tenant, conversation_id, missing_fields, 3  # type: ignore[arg-type]
            )
        assert str(exc_info.value) == expected, label


async def test_rejects_invalid_or_oversized_ids(suggest_db: AsyncEngine) -> None:
    """tenant/conversation 非法或超 32 → 固定 ValidationError。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    invalid_cases: list[tuple[str, object, object, str]] = [
        ("空白 tenant", "   ", conversation_id, "会话租户无效"),
        ("tenant 超 32", "t" * 33, conversation_id, "会话租户超长"),
        ("空白 conversation", tenant, "   ", "会话标识无效"),
        ("conversation 超 32", tenant, "c" * 33, "会话标识超长"),
    ]
    for label, bad_tenant, bad_conv, expected in invalid_cases:
        with pytest.raises(ValidationError) as exc_info:
            await service.suggest_next_questions(
                bad_tenant, bad_conv, ["quantity"], 3  # type: ignore[arg-type]
            )
        assert str(exc_info.value) == expected, label


async def test_input_validation_precedes_db(suggest_db: AsyncEngine) -> None:
    """输入校验先于数据库存在性：conversation 不存在时，错误必须是输入摘要。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    ghost = ConversationId(new_id("con"))  # 从不存在的会话
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    cases: list[tuple[str, object, object, object, str]] = [
        ("空白 tenant", "   ", ghost, ["quantity"], 3, "会话租户无效"),
        ("tenant 超 32", "t" * 33, ghost, ["quantity"], 3, "会话租户超长"),
        ("空白 conversation", tenant, "", ["quantity"], 3, "会话标识无效"),
        ("missing_fields 非 list", tenant, ghost, "quantity", 3, "缺失字段必须是列表"),
        ("元素首尾空白", tenant, ghost, [" quantity "], 3, "缺失字段无效"),
        ("completeness bool", tenant, ghost, ["quantity"], True, "完整度必须为 0–5 的整数"),
    ]
    for case in cases:
        label, t, c, m, k, expected = case
        with pytest.raises(ValidationError) as exc_info:
            await service.suggest_next_questions(t, c, m, k)  # type: ignore[arg-type]
        assert str(exc_info.value) == expected, label


async def test_conversation_must_exist_tenant_bound(suggest_db: AsyncEngine) -> None:
    """不存在 / 跨租户不可见 → 统一 ValidationError("会话不存在")。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant_a)
    ghost = ConversationId(new_id("con"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    service_b = _service(factory, tenant_b, clock)

    # 本租户不存在的会话
    with pytest.raises(ValidationError) as exc_info:
        await service_a.suggest_next_questions(tenant_a, ghost, ["quantity"], 3)
    assert str(exc_info.value) == "会话不存在"

    # 跨租户不可见：B-bound 服务用 A 的会话 id（不得抛 TenantIsolationViolation）
    with pytest.raises(ValidationError) as exc_info:
        await service_b.suggest_next_questions(
            tenant_b, conversation_id, ["quantity"], 3
        )
    assert str(exc_info.value) == "会话不存在"

    # 双方均无副作用
    assert await _outbox_events(factory, tenant_a) == []
    assert await _outbox_events(factory, tenant_b) == []
    assert await _counts(factory, tenant_a) == (1, 0)
    assert await _counts(factory, tenant_b) == (0, 0)


async def test_no_event_no_log_no_write(
    suggest_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """只读：outbox/conversation/message 数量不变；零日志、零事件。"""
    factory = async_sessionmaker(suggest_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    conversation_id = await _create_conversation(factory, tenant)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    baseline_outbox = await _outbox_events(factory, tenant)
    baseline_counts = await _counts(factory, tenant)
    assert baseline_outbox == []
    assert baseline_counts == (1, 0)

    caplog.clear()
    suggestion = await service.suggest_next_questions(
        tenant, conversation_id, ["quantity", "material"], 3
    )
    assert suggestion.topics == ["quantity", "material"]
    assert caplog.records == []

    assert await _outbox_events(factory, tenant) == baseline_outbox
    assert await _counts(factory, tenant) == baseline_counts
