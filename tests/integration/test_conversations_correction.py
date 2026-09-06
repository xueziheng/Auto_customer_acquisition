"""conversations 域分类纠正留痕（阶段 1/2/3 已覆盖：模型 + 仓储契约、
服务行为、真实并发；真实 PostgreSQL）。

TDD RED（分阶段）：本文件先写模型/仓储契约测试，预期失败是
``ClassificationCorrection`` 模型或 ``add_correction``/``list_corrections``
接口尚不存在（动态 getattr，明确行为断言，不用 collection ImportError）；
阶段 2 的 RED 是 ``ConversationServiceImpl.correct_classification`` 缺失
（AttributeError）；阶段 3 并发测试使用两个独立服务实例/UoW/AsyncSession
（``asyncio.gather``，绝不共享 session），初始即 GREEN 时属既有 DB 级
并发语义的行为确认，并补 mutation proof。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest

# ruff: noqa: F811 - 复用owned数据库fixture
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.conversations.schemas import ReplyCategory
from domains.conversations.service import ConversationService, InboxActor, InboxScope
from shared.errors import PermissionDenied, TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


def _correction_model():
    """动态解析领域模型：尚不存在时返回 None（测试明确断言，不 collection 失败）。"""
    return getattr(
        importlib.import_module("domains.conversations.models"),
        "ClassificationCorrection",
        None,
    )


@dataclass
class MutableClock:
    value: datetime
    calls: int = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


@pytest_asyncio.fixture
async def correction_db(owned_infrastructure) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(
        owned_infrastructure.config.database_url.get_secret_value()
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def _uow_factory(factory: async_sessionmaker[AsyncSession], tenant: TenantId):
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    return lambda requested: uow_type(factory, requested, now=lambda: NOW)


async def test_correction_model_valid_construction_preserves_fields(
    correction_db: AsyncEngine,
) -> None:
    """模型合法构造字段保真（行为测试，不只查符号存在）。"""
    model = _correction_model()
    if model is None:
        raise AssertionError("RED：ClassificationCorrection 模型尚未创建")
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    correction = model(
        correction_id="ccr_01KZXT00000000000000000001",
        tenant_id=tenant,
        message_id=message_id,
        corrected_category=ReplyCategory.UNSUBSCRIBE,
        corrected_by="emp-1",
        corrected_at=NOW,
    )
    assert correction.correction_id == "ccr_01KZXT00000000000000000001"
    assert correction.tenant_id == tenant
    assert correction.message_id == message_id
    assert correction.corrected_category is ReplyCategory.UNSUBSCRIBE
    assert correction.corrected_by == "emp-1"
    assert correction.corrected_at == NOW


async def test_correction_model_rejects_invalid_inputs(
    correction_db: AsyncEngine,
) -> None:
    """每个非法输入 ValidationError（硬边界 8：tenant/message 非空 str；
    长度 fail-closed 与 DB 列上限对齐：correction_id/tenant_id ≤ 32、
    message_id/corrected_by ≤ 100，边界长度合法输入不得被误拒）。"""
    model = _correction_model()
    if model is None:
        raise AssertionError("RED：ClassificationCorrection 模型尚未创建")
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))

    def build(**overrides: object) -> object:
        fields: dict[str, object] = {
            "correction_id": "ccr_01KZXT00000000000000000001",
            "tenant_id": tenant,
            "message_id": message_id,
            "corrected_category": ReplyCategory.UNSUBSCRIBE,
            "corrected_by": "emp-1",
            "corrected_at": NOW,
        }
        fields.update(overrides)
        return model(**fields)

    invalid_cases: list[tuple[str, dict[str, object]]] = [
        ("空 tenant_id", {"tenant_id": ""}),
        ("空 message_id", {"message_id": ""}),
        ("空 correction_id", {"correction_id": ""}),
        ("空 corrected_by", {"corrected_by": "  "}),
        ("非枚举 category", {"corrected_category": "unsubscribe"}),
        ("naive corrected_at", {"corrected_at": NOW.replace(tzinfo=None)}),
        ("correction_id 超长 33", {"correction_id": "x" * 33}),
        ("tenant_id 超长 33", {"tenant_id": "t" * 33}),
        ("message_id 超长 101", {"message_id": "m" * 101}),
        ("corrected_by 超长 101", {"corrected_by": "e" * 101}),
    ]
    for label, overrides in invalid_cases:
        try:
            build(**overrides)
        except ValidationError:
            continue
        raise AssertionError(f"RED：非法输入应被拒（{label}）")

    # 边界合法：长度恰好等于 DB 列上限时必须构造成功
    boundary_cases: list[tuple[str, dict[str, object]]] = [
        ("correction_id 恰好 32", {"correction_id": "x" * 32}),
        ("tenant_id 恰好 32", {"tenant_id": "t" * 32}),
        ("message_id 恰好 100", {"message_id": "m" * 100}),
        ("corrected_by 恰好 100", {"corrected_by": "e" * 100}),
    ]
    for label, overrides in boundary_cases:
        try:
            build(**overrides)
        except ValidationError:
            raise AssertionError(f"RED：边界合法输入被误拒（{label}）")


def _repo_class():
    return getattr(
        importlib.import_module("infra.db.repositories.conversations"),
        "ClassificationRepositoryImpl",
        None,
    )


async def _insert_through_repo(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    correction: object,
) -> bool:
    """通过真实 UoW 调用 add_correction（当前应缺接口 → RED）。"""
    model = _correction_model()
    if model is None:
        raise AssertionError("RED：ClassificationCorrection 模型尚未创建")
    repo_type = _repo_class()
    if repo_type is None:
        raise AssertionError("RED：ClassificationRepositoryImpl 不存在")
    add = getattr(repo_type, "add_correction", None)
    if add is None:
        raise AssertionError("RED：ClassificationRepositoryImpl.add_correction 不存在")
    uow_factory = _uow_factory(factory, tenant)
    async with uow_factory(tenant) as uow:
        return await uow.classifications.add_correction(correction)


async def _list_through_repo(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    message_id: MessageId,
) -> list[object]:
    repo_type = _repo_class()
    if repo_type is None:
        raise AssertionError("RED：ClassificationRepositoryImpl 不存在")
    list_method = getattr(repo_type, "list_corrections", None)
    if list_method is None:
        raise AssertionError(
            "RED：ClassificationRepositoryImpl.list_corrections 不存在"
        )
    uow_factory = _uow_factory(factory, tenant)
    async with uow_factory(tenant) as uow:
        return await uow.classifications.list_corrections(tenant, message_id)


async def test_correction_repository_insert_and_list(
    correction_db: AsyncEngine,
) -> None:
    """确定性排序与幂等（真实 PostgreSQL；顺序不依赖插入顺序）。

    - 先插 B 再插 A（同 corrected_at）→ list 必须按 (corrected_at,
      correction_id) 升序返回 [A, B]；若仓储只按 corrected_at 排序，
      同刻行的返回顺序不确定，此断言即失效
    - 更早 corrected_at 的 Z（id 字典序最大）必须排最前
    - 同幂等键（tenant/message/by/category 相同）、不同 correction_id →
      add 返回 False 且行数不变
    """
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    model = _correction_model()
    if model is None:
        raise AssertionError("RED：ClassificationCorrection 模型尚未创建")
    ids = {
        "a": "ccr_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        "b": "ccr_bbbbbbbbbbbbbbbbbbbbbbbbbb",
        "c": "ccr_cccccccccccccccccccccccccc",
        "z": "ccr_zzzzzzzzzzzzzzzzzzzzzzzzzz",
    }
    earlier = NOW - timedelta(hours=1)

    def make(
        correction_id: str,
        category: ReplyCategory,
        corrected_by: str,
        corrected_at: datetime,
    ) -> object:
        return model(
            correction_id=correction_id,
            tenant_id=tenant,
            message_id=message_id,
            corrected_category=category,
            corrected_by=corrected_by,
            corrected_at=corrected_at,
        )

    # 先插 B 再插 A：同 corrected_at，必须按 correction_id 升序 → [A, B]
    b = make(ids["b"], ReplyCategory.UNSUBSCRIBE, "emp-1", NOW)
    assert await _insert_through_repo(factory, tenant, b) is True
    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 1
    assert rows[0].correction_id == ids["b"]

    a = make(ids["a"], ReplyCategory.REJECTION, "emp-1", NOW)
    assert await _insert_through_repo(factory, tenant, a) is True
    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 2
    assert [row.correction_id for row in rows] == [ids["a"], ids["b"]]
    assert [row.corrected_category for row in rows] == [
        ReplyCategory.REJECTION,
        ReplyCategory.UNSUBSCRIBE,
    ]

    # 同幂等键、不同 correction_id → False，行数不变
    dup = make(ids["c"], ReplyCategory.REJECTION, "emp-1", NOW)
    assert await _insert_through_repo(factory, tenant, dup) is False
    assert len(await _list_through_repo(factory, tenant, message_id)) == 2

    # 更早 corrected_at 的 Z 排最前：full 顺序 = (corrected_at, correction_id)
    z = make(ids["z"], ReplyCategory.COMPLAINT, "emp-2", earlier)
    assert await _insert_through_repo(factory, tenant, z) is True
    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 3
    assert rows[0].corrected_at == earlier
    assert [row.correction_id for row in rows] == [ids["z"], ids["a"], ids["b"]]
    assert [row.corrected_category for row in rows] == [
        ReplyCategory.COMPLAINT,
        ReplyCategory.REJECTION,
        ReplyCategory.UNSUBSCRIBE,
    ]


async def test_correction_repository_cross_tenant_fails_closed(
    correction_db: AsyncEngine,
) -> None:
    """仓储参数租户不匹配：add/list 均 TenantIsolationViolation。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    model = _correction_model()
    if model is None:
        raise AssertionError("RED：ClassificationCorrection 模型尚未创建")
    correction = model(
        correction_id=new_id("ccr"),
        tenant_id=tenant_a,
        message_id=message_id,
        corrected_category=ReplyCategory.UNSUBSCRIBE,
        corrected_by="emp-1",
        corrected_at=NOW,
    )
    repo_type = _repo_class()
    if repo_type is None:
        raise AssertionError("RED：ClassificationRepositoryImpl 不存在")
    async with _uow_factory(factory, tenant_b)(tenant_b) as uow:
        try:
            await uow.classifications.add_correction(correction)
        except TenantIsolationViolation:
            pass
        else:
            raise AssertionError("RED：跨租户 add 应抛 TenantIsolationViolation")
        try:
            await uow.classifications.list_corrections(tenant_a, message_id)
        except TenantIsolationViolation:
            pass
        else:
            raise AssertionError("RED：跨租户 list 应抛 TenantIsolationViolation")


def _employee_id(tenant, index=1):
    return EmployeeId(f"e{index}_{tenant[3:]}")


def _actor(tenant, index=1):
    return InboxActor(tenant, _employee_id(tenant, index), "boss", InboxScope.TENANT)


async def _inbox_fixture(service, factory, tenant, config):
    from apps.api.controlled import initialize_identities

    identities = []
    boss_index = 0
    for identity in config.identities:
        if identity.role == "boss":
            boss_index += 1
        identities.append(
            identity.model_copy(
                update={
                    "employee_id": _employee_id(tenant, boss_index)
                    if identity.role == "boss"
                    else new_id("emp"),
                    "user_id": new_id("usr"),
                }
            )
        )
    assert boss_index >= 2
    await initialize_identities(
        config.model_copy(update={"tenant_id": tenant, "identities": tuple(identities)})
    )
    return await service.ingest_inbound(
        tenant,
        None,
        ProspectAccountId(new_id("acc")),
        new_id("raw"),
        f"<{new_id('ext')}@example.test>",
        NOW,
    )


# --- 阶段 2：ConversationServiceImpl.correct_classification（真实服务+UoW） ---


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> ConversationService:
    """真实 ConversationServiceImpl + 真实 UoW（now=clock.now，可计数可篡改）。"""
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


async def _outbox_events(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    """outbox 事件（按 tenant 过滤，不越租户）。"""
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(tables.OutboxEventRow).where(
                        tables.OutboxEventRow.tenant_id == str(tenant)
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _classification_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    """原分类行直读（按 tenant 过滤）。"""
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(tables.ConversationClassificationRow).where(
                        tables.ConversationClassificationRow.tenant_id == str(tenant)
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def _correction_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    """纠正行直读（按 tenant 过滤）。"""
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    select(tables.ConversationClassificationCorrectionRow).where(
                        tables.ConversationClassificationCorrectionRow.tenant_id
                        == str(tenant)
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def test_correct_classification_persists_and_preserves_original_classification(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """happy path：先落分类（REQUESTS_MATERIALS/model-v1），纠正到
    UNSUBSCRIBE/emp-1 → 恰一条纠正（correction_id 为 new_id("ccr") 前缀、
    长度<=32、corrected_at=时钟值）；原分类行 category/classified_by/
    classified_at 完全不变；成功纠正不发布任何 outbox 事件。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service, factory, tenant, owned_infrastructure.config
    )

    await service.record_classification(
        tenant,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )
    baseline = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]

    clock.calls = 0
    result = await service.correct_classification(
        tenant,
        message_id,
        ReplyCategory.UNSUBSCRIBE,
        corrected_by=_employee_id(tenant),
        actor=_actor(tenant),
    )
    assert result is None
    assert clock.calls == 1  # now 只调用/验证一次

    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 1
    correction = rows[0]
    assert correction.correction_id.startswith("ccr_")
    assert len(correction.correction_id) <= 32
    assert correction.corrected_category is ReplyCategory.UNSUBSCRIBE
    assert correction.corrected_by == _employee_id(tenant)
    assert correction.corrected_at == NOW

    original = await _classification_rows(factory, tenant)
    assert len(original) == 1
    assert original[0].category == "requests_materials"
    assert original[0].classified_by == "model-v1"
    assert original[0].classified_at == NOW

    after = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]
    assert after == baseline  # 不发布任何纠正事件


async def test_correct_classification_unclassified_message_fails_closed(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """未分类消息：ValidationError 精确摘要“消息尚未分类”，零纠正、零事件。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service, factory, tenant, owned_infrastructure.config
    )

    baseline = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]
    with pytest.raises(ValidationError) as exc_info:
        await service.correct_classification(
            tenant,
            message_id,
            ReplyCategory.UNSUBSCRIBE,
            corrected_by=_employee_id(tenant),
            actor=_actor(tenant),
        )
    assert str(exc_info.value) == "消息尚未分类"
    assert await _correction_rows(factory, tenant) == []
    assert [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ] == baseline


async def test_correct_classification_sequential_idempotency(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """顺序重复幂等：同 (by, category) 两次均正常返回 None 且恰一条；
    同 by 不同 category、同 category 不同 by（固定时钟）各自保留一行。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service, factory, tenant, owned_infrastructure.config
    )

    await service.record_classification(
        tenant,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )

    first = await service.correct_classification(
        tenant,
        message_id,
        ReplyCategory.UNSUBSCRIBE,
        corrected_by=_employee_id(tenant),
        actor=_actor(tenant),
    )
    second = await service.correct_classification(
        tenant,
        message_id,
        ReplyCategory.UNSUBSCRIBE,
        corrected_by=_employee_id(tenant),
        actor=_actor(tenant),
    )
    assert first is None and second is None
    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 1
    assert rows[0].corrected_category is ReplyCategory.UNSUBSCRIBE
    assert rows[0].corrected_by == _employee_id(tenant)

    await service.correct_classification(
        tenant,
        message_id,
        ReplyCategory.REJECTION,
        corrected_by=_employee_id(tenant),
        actor=_actor(tenant),
    )
    await service.correct_classification(
        tenant,
        message_id,
        ReplyCategory.UNSUBSCRIBE,
        corrected_by=_employee_id(tenant, 2),
        actor=_actor(tenant, 2),
    )
    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 3
    assert {(r.corrected_category, r.corrected_by) for r in rows} == {
        (ReplyCategory.UNSUBSCRIBE, _employee_id(tenant)),
        (ReplyCategory.REJECTION, _employee_id(tenant)),
        (ReplyCategory.UNSUBSCRIBE, _employee_id(tenant, 2)),
    }
    assert {r.corrected_at for r in rows} == {NOW}  # 固定时钟下多条并存


async def test_correct_classification_input_fail_closed(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """输入 fail-closed：空值/空白/超长/非枚举/时钟非法 → 固定摘要
    ValidationError（不回显输入）；错误发生在任何外部副作用之前：零纠正、
    outbox 不变。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service, factory, tenant, owned_infrastructure.config
    )

    # 先落原分类：证明输入校验先于“消息尚未分类”路径与任何 UoW 副作用
    await service.record_classification(
        tenant,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )
    baseline = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]

    invalid_cases: list[tuple[str, dict[str, object]]] = [
        ("空 tenant", {"tenant_id": ""}),
        ("空 message", {"message_id": ""}),
        ("非枚举 category", {"corrected_category": "unsubscribe"}),
        ("空 corrected_by", {"corrected_by": "   "}),
        ("tenant 超长 33", {"tenant_id": "t" * 33}),
        ("message 超长 101", {"message_id": "m" * 101}),
        ("corrected_by 超长 101", {"corrected_by": "e" * 101}),
    ]
    for label, overrides in invalid_cases:
        kwargs: dict[str, object] = {
            "tenant_id": tenant,
            "message_id": message_id,
            "corrected_category": ReplyCategory.UNSUBSCRIBE,
            "corrected_by": _employee_id(tenant),
            "actor": _actor(tenant),
            **overrides,
        }
        with pytest.raises(ValidationError) as exc_info:
            await service.correct_classification(**kwargs)  # type: ignore[arg-type]
        assert len(str(exc_info.value)) < 40, f"非固定摘要，疑似回显输入（{label}）"

    # 空白 tenant/message：必须精确拒绝为“纠正租户无效/纠正消息无效”
    # （str+strip 语义，与 ClassificationCorrection 一致），且不得走到
    # “消息尚未分类”路径或打开 UoW 产生副作用
    for label, overrides, expected in [
        ("空白 tenant", {"tenant_id": "   "}, "纠正租户无效"),
        ("空白 message", {"message_id": "   "}, "纠正消息无效"),
    ]:
        kwargs = {
            "tenant_id": tenant,
            "message_id": message_id,
            "corrected_category": ReplyCategory.UNSUBSCRIBE,
            "corrected_by": _employee_id(tenant),
            "actor": _actor(tenant),
            **overrides,
        }
        with pytest.raises(ValidationError) as exc_info:
            await service.correct_classification(**kwargs)  # type: ignore[arg-type]
        assert str(exc_info.value) == expected

    # 服务时钟：naive / 非 UTC offset 均固定拒绝（沿用 _validate_now 契约）
    for bad_now in [
        NOW.replace(tzinfo=None),
        NOW.astimezone(timezone(timedelta(hours=2))),
    ]:
        clock.value = bad_now
        with pytest.raises(ValidationError) as exc_info:
            await service.correct_classification(
                tenant,
                message_id,
                ReplyCategory.UNSUBSCRIBE,
                corrected_by=_employee_id(tenant),
                actor=_actor(tenant),
            )
        assert str(exc_info.value) == "服务时钟必须为 UTC"
    clock.value = NOW

    assert await _correction_rows(factory, tenant) == []
    assert [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ] == baseline


async def test_correct_classification_cross_tenant_invisible(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """服务级租户不可见：A 有原分类；B-bound 服务以相同 message 纠正 →
    ValidationError“消息尚未分类”（不是 TenantIsolationViolation，那是仓储
    参数越界测试）；A/B 均零纠正，A 原分类不变。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    service_b = _service(factory, tenant_b, clock)
    message_id = await _inbox_fixture(
        service_a, factory, tenant_a, owned_infrastructure.config
    )
    await _inbox_fixture(service_b, factory, tenant_b, owned_infrastructure.config)

    await service_a.record_classification(
        tenant_a,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )

    with pytest.raises(PermissionDenied) as exc_info:
        await service_b.correct_classification(
            tenant_b,
            message_id,
            ReplyCategory.UNSUBSCRIBE,
            corrected_by=_employee_id(tenant_b),
            actor=_actor(tenant_b),
        )
    assert str(exc_info.value) == "收件箱访问拒绝"

    assert await _correction_rows(factory, tenant_a) == []
    assert await _correction_rows(factory, tenant_b) == []
    original = await _classification_rows(factory, tenant_a)
    assert len(original) == 1
    assert original[0].category == "requests_materials"


async def test_correct_classification_no_event_no_leak(
    correction_db: AsyncEngine,
    owned_infrastructure,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """no-leak/no-event：超长 corrected_by 用凭证 marker（101 字符）→ 固定
    ValidationError，异常文本/日志/outbox 均不含 marker、marker 不落库；
    合法纠正同样零事件、零日志。"""
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service, factory, tenant, owned_infrastructure.config
    )
    marker = "sk-prod-" + "x" * 93  # 101 字符 > corrected_by 上限 100

    await service.record_classification(
        tenant,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )
    baseline = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]
    assert {item[0] for item in baseline} == {"InboundMessageStored", "ReplyReceived"}

    caplog.clear()
    with pytest.raises(ValidationError) as exc_info:
        await service.correct_classification(
            tenant,
            message_id,
            ReplyCategory.UNSUBSCRIBE,
            corrected_by=marker,
            actor=_actor(tenant),
        )
    assert str(exc_info.value) == "纠正人超长"
    assert marker not in str(exc_info.value)
    assert caplog.records == []
    assert all(
        marker not in getattr(record, "message", "") for record in caplog.records
    )

    # 成功路径同样零事件、零日志
    caplog.clear()
    await service.correct_classification(
        tenant,
        message_id,
        ReplyCategory.UNSUBSCRIBE,
        corrected_by=_employee_id(tenant),
        actor=_actor(tenant),
    )
    assert caplog.records == []
    after = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]
    assert after == baseline

    rows = await _correction_rows(factory, tenant)
    assert len(rows) == 1  # 只有 emp-1 的合法纠正；marker 从未落库
    assert all(marker not in r.corrected_by for r in rows)


# --- 阶段 3：真实并发（两个独立服务实例/UoW/AsyncSession，绝不共享 session） ---


async def test_correction_concurrent_same_key_exactly_one_row(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """同幂等键并发：恰一行。

    两个独立 ``ConversationServiceImpl`` 实例，各自 ``uow_factory`` 每调用
    创建独立的 ``SqlAlchemyConversationsUnitOfWork``（其 ``__aenter__`` 新建
    AsyncSession）——**绝不复用同一 AsyncSession**。``asyncio.gather`` 同时
    纠正同 message/category/by、同固定时钟；两结果均非异常且都是 None；
    新 session 回读恰一行且 key 正确；outbox 等于并发前 baseline（无事件）。
    """
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service_a, factory, tenant, owned_infrastructure.config
    )

    await service_a.record_classification(
        tenant,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )
    baseline = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]

    results = await asyncio.gather(
        service_a.correct_classification(
            tenant,
            message_id,
            ReplyCategory.UNSUBSCRIBE,
            corrected_by=_employee_id(tenant),
            actor=_actor(tenant),
        ),
        service_b.correct_classification(
            tenant,
            message_id,
            ReplyCategory.UNSUBSCRIBE,
            corrected_by=_employee_id(tenant),
            actor=_actor(tenant),
        ),
        return_exceptions=True,
    )
    assert all(result is None for result in results), results

    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 1
    assert rows[0].corrected_category is ReplyCategory.UNSUBSCRIBE
    assert rows[0].corrected_by == _employee_id(tenant)
    assert rows[0].corrected_at == NOW
    assert [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ] == baseline


async def test_correction_concurrent_different_keys_two_rows_same_clock(
    correction_db: AsyncEngine,
    owned_infrastructure,
) -> None:
    """不同幂等键并发（同固定时钟）：恰两行。

    两个独立服务实例/UoW/AsyncSession（同上，绝不共享 session）；同时纠正
    同 message 但不同 category（同 corrected_by、同 clock）；两结果均非异常；
    新 session 回读恰两行、两个 key 完整、corrected_at 均 NOW；原分类不变、
    outbox 等于 baseline（无事件）。
    """
    factory = async_sessionmaker(correction_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    message_id = MessageId(new_id("msg"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)
    message_id = await _inbox_fixture(
        service_a, factory, tenant, owned_infrastructure.config
    )

    await service_a.record_classification(
        tenant,
        message_id,
        ReplyCategory.REQUESTS_MATERIALS,
        classified_by="model-v1",
    )
    baseline = [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ]
    original_before = await _classification_rows(factory, tenant)

    results = await asyncio.gather(
        service_a.correct_classification(
            tenant,
            message_id,
            ReplyCategory.UNSUBSCRIBE,
            corrected_by=_employee_id(tenant),
            actor=_actor(tenant),
        ),
        service_b.correct_classification(
            tenant,
            message_id,
            ReplyCategory.REJECTION,
            corrected_by=_employee_id(tenant),
            actor=_actor(tenant),
        ),
        return_exceptions=True,
    )
    assert all(result is None for result in results), results

    rows = await _list_through_repo(factory, tenant, message_id)
    assert len(rows) == 2
    assert {(r.corrected_category, r.corrected_by) for r in rows} == {
        (ReplyCategory.UNSUBSCRIBE, _employee_id(tenant)),
        (ReplyCategory.REJECTION, _employee_id(tenant)),
    }
    assert {r.corrected_at for r in rows} == {NOW}

    original_after = await _classification_rows(factory, tenant)

    # 两次直读是不同 ORM 实例（identity 比较恒不等），按字段值比较
    def _row_fields(rows: list[object]) -> list[tuple[object, ...]]:
        return [
            (r.tenant_id, r.message_id, r.category, r.classified_by, r.classified_at)
            for r in rows
        ]

    assert _row_fields(original_after) == _row_fields(original_before)  # 原分类不变
    assert [
        (e.event_type, e.event_payload) for e in await _outbox_events(factory, tenant)
    ] == baseline
