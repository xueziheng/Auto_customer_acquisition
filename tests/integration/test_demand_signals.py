"""demand 信号 capture/discard 集成（2026-08-17 计划 Task 3/4；真实 PostgreSQL +
真实 UoW/仓储，零 mock）。

RED 预期：Task 3 各测试在运行时经 ``importlib.import_module("infra.db.demand_uow")``
加载不存在的模块，以 ``ModuleNotFoundError: No module named 'infra.db.demand_uow'``
失败（文件可收集，失败发生在测试体内——缺实现而非 fixture/环境错误）；Task 4
以 ``discard_signal`` 缺失（AttributeError）失败。跨层访问域内 models/repository
用 importlib（check_boundaries domain-internals 规则）；schemas/service 可静态 import。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from shared.errors import (
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import TenantId, new_id

_models = importlib.import_module("domains.demand.models")
_demand_errors = importlib.import_module("domains.demand.errors")
MissingWebEvidenceError = _demand_errors.MissingWebEvidenceError

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

#: 生产内容 marker：outbox/log/error 均不得出现（规格 §5/§7/§9.2）
OBSERVATION_MARKER = "Acme SECRET-OBSERVATION-42 opened a new plant in Rotterdam."
NEED_MARKER = "stainless steel hinges SECRET-NEED-42"


@dataclass
class MutableClock:
    value: datetime
    calls: int = 0

    def now(self) -> datetime:
        self.calls += 1
        return self.value


@pytest_asyncio.fixture
async def demand_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
) -> DemandService:
    uow_type = importlib.import_module(
        "infra.db.demand_uow"
    ).SqlAlchemyDemandUnitOfWork
    impl_type = importlib.import_module(
        "domains.demand.service_impl"
    ).DemandServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


def _request(**overrides: object) -> SignalCaptureRequest:
    fields: dict[str, object] = {
        "signal_type": "product_line_expansion",
        "entity_name": "Acme Manufacturing",
        "raw_observation": OBSERVATION_MARKER,
        "observed_at": NOW,
        "source_type": "web_page",
        "source_id": "sha256:pagehash001",
        "extracted_by": "model-v1",
        "source_url": "https://example.com/acme-expansion",
        "page_hash": "sha256:pagehash001",
        "possible_need": NEED_MARKER,
    }
    fields.update(overrides)
    return SignalCaptureRequest(**fields)  # type: ignore[arg-type]


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


async def _signal_rows(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        rows = (
            await session.execute(
                select(tables.DemandSignalRow).where(
                    tables.DemandSignalRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(rows)


async def test_capture_roundtrip_persists_all_columns(demand_db: AsyncEngine) -> None:
    """capture 落库：18 列全往返（含 provenance 展开列、confirmed pair None、
    possible_need/account_id/discard_reason None）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    signal_id = await service.capture_signal(tenant, _request())
    assert signal_id.startswith("sig_")
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == str(tenant)
    assert row.signal_id == signal_id
    assert row.signal_type == "product_line_expansion"
    assert row.entity_name == "Acme Manufacturing"
    assert row.raw_observation == OBSERVATION_MARKER
    assert row.observed_at == NOW
    assert row.status == "captured"
    assert row.possible_need == NEED_MARKER
    assert row.account_id is None
    assert row.discard_reason is None
    assert row.source_type == "web_page"
    assert row.source_id == "sha256:pagehash001"
    assert row.extracted_by == "model-v1"
    assert row.extracted_at == NOW
    assert row.confirmed_by is None and row.confirmed_at is None
    assert row.source_url == "https://example.com/acme-expansion"
    assert row.page_hash == "sha256:pagehash001"


async def test_capture_web_evidence_fail_closed(demand_db: AsyncEngine) -> None:
    """WEB_PAGE 缺 url/hash 或 source_id != page_hash → MissingWebEvidenceError。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    cases = [
        {"source_url": None},
        {"page_hash": None},
        {"source_id": "sha256:other", "page_hash": "sha256:pagehash001"},
    ]
    for overrides in cases:
        with pytest.raises(MissingWebEvidenceError):
            await service.capture_signal(tenant, _request(**overrides))
    assert await _signal_rows(factory, tenant) == []


async def test_capture_non_web_different_source_ids_are_distinct(
    demand_db: AsyncEngine,
) -> None:
    """非网页：同 entity/type 不同 source_id → 两独立行、两事件。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    first = await service.capture_signal(
        tenant,
        _request(
            source_type="conversation",
            source_id="msg_conv_001",
            source_url=None,
            page_hash=None,
        ),
    )
    second = await service.capture_signal(
        tenant,
        _request(
            source_type="conversation",
            source_id="msg_conv_002",
            source_url=None,
            page_hash=None,
        ),
    )
    assert first != second
    assert len(await _signal_rows(factory, tenant)) == 2
    events = await _outbox_events(factory, tenant)
    assert len(events) == 2


async def test_capture_duplicate_replay_returns_first_id_single_event(
    demand_db: AsyncEngine,
) -> None:
    """同来源串行重放：返回首条 signal_id、1 行、1 事件、首条内容保留。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    first_id = await service.capture_signal(tenant, _request())
    replayed = await service.capture_signal(
        tenant, _request(raw_observation="DIFFERENT CONTENT")
    )
    assert replayed == first_id
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].raw_observation == OBSERVATION_MARKER  # 首条内容保留
    assert len(await _outbox_events(factory, tenant)) == 1


async def test_capture_concurrent_same_key_exactly_one_row_one_event(
    demand_db: AsyncEngine,
) -> None:
    """asyncio.gather 两个独立 UoW 同 key → 恰 1 行 1 事件、两结果 id 相同。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)

    results = await asyncio.gather(
        service_a.capture_signal(tenant, _request()),
        service_b.capture_signal(tenant, _request()),
        return_exceptions=True,
    )
    assert all(isinstance(r, str) for r in results), results
    assert results[0] == results[1]
    assert len(await _signal_rows(factory, tenant)) == 1
    assert len(await _outbox_events(factory, tenant)) == 1


async def test_capture_cross_tenant_same_key_two_rows(demand_db: AsyncEngine) -> None:
    """A/B 同 (entity,type,source_type,source_id) → 各 1 行（不互相去重）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    service_b = _service(factory, tenant_b, clock)

    await service_a.capture_signal(tenant_a, _request())
    await service_b.capture_signal(tenant_b, _request())
    assert len(await _signal_rows(factory, tenant_a)) == 1
    assert len(await _signal_rows(factory, tenant_b)) == 1


async def test_capture_optional_fields_none_and_present(demand_db: AsyncEngine) -> None:
    """possible_need None / 非 None 两态往返。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    await service.capture_signal(tenant, _request(possible_need=None))
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1 and rows[0].possible_need is None


async def test_demand_signals_db_checks_fail_closed(demand_db: AsyncEngine) -> None:
    """8 个 CHECK 违例 → IntegrityError（直插行验证约束语义）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    tables = importlib.import_module("infra.db.tables")

    def base() -> dict[str, object]:
        return {
            "tenant_id": str(tenant),
            "signal_id": new_id("sig"),
            "signal_type": "product_line_expansion",
            "entity_name": "Acme Manufacturing",
            "raw_observation": OBSERVATION_MARKER,
            "observed_at": NOW,
            "status": "captured",
            "source_type": "web_page",
            "source_id": "sha256:pagehash001",
            "extracted_by": "model-v1",
            "extracted_at": NOW,
            "source_url": "https://example.com/acme",
            "page_hash": "sha256:pagehash001",
        }

    bad_cases = [
        ("ck_demand_signals_type", {"signal_type": "not_a_type"}),
        ("ck_demand_signals_status", {"status": "bogus"}),
        ("ck_demand_signals_source_type", {"source_type": "bogus"}),
        (
            "ck_demand_signals_confirmed_pair",
            {"confirmed_by": "emp-1", "confirmed_at": None},
        ),
        (
            "ck_demand_signals_web_evidence",
            {"source_url": None, "page_hash": "sha256:pagehash001"},
        ),
        (
            "ck_demand_signals_web_evidence",
            {"source_id": "sha256:other", "page_hash": "sha256:pagehash001"},
        ),
        (
            "ck_demand_signals_discard_reason",
            {"status": "captured", "discard_reason": "noise"},
        ),
        (
            "ck_demand_signals_discard_reason",
            {"status": "discarded", "discard_reason": None},
        ),
        (
            "ck_demand_signals_core_nonblank",
            {"entity_name": "   "},
        ),
        (
            "ck_demand_signals_optional_nonblank",
            {"possible_need": "   "},
        ),
    ]
    for _label, overrides in bad_cases:
        values = base()
        values.update(overrides)
        async with factory() as session:
            with pytest.raises(IntegrityError):
                session.add(tables.DemandSignalRow(**values))  # type: ignore[arg-type]
                await session.flush()
            await session.rollback()
    assert await _signal_rows(factory, tenant) == []


async def test_capture_repo_tenant_mismatch_raises(demand_db: AsyncEngine) -> None:
    """repo 参数租户与绑定租户不一致 → TenantIsolationViolation。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    clock = MutableClock(NOW)
    signal = _models.DemandSignal(
        signal_id=_models.DemandSignalId(new_id("sig")),
        tenant_id=tenant_a,
        signal_type=_models.SignalType.PRODUCT_LINE_EXPANSION,
        entity_name="Acme Manufacturing",
        raw_observation=OBSERVATION_MARKER,
        observed_at=NOW,
        provenance=_provenance_for(),
    )
    async with uow_type(factory, tenant_b, now=clock.now) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.signals.add(signal)
        with pytest.raises(TenantIsolationViolation):
            await uow.signals.get(tenant_a, signal.signal_id)
        with pytest.raises(TenantIsolationViolation):
            await uow.signals.find_duplicate(
                tenant_a,
                "Acme Manufacturing",
                "product_line_expansion",
                "web_page",
                "sha256:pagehash001",
            )


def _provenance_for():
    """构造真实 Provenance（与 _request 同源）。"""
    from shared.schemas.provenance import Provenance, SourceType

    return Provenance(
        source_type=SourceType.WEB_PAGE,
        source_id="sha256:pagehash001",
        extracted_by="model-v1",
        extracted_at=NOW,
        source_url="https://example.com/acme",
        page_hash="sha256:pagehash001",
    )


async def test_capture_input_validation_precedes_uow(demand_db: AsyncEngine) -> None:
    """输入校验先于 UoW/DB：(label, field, value) 逐字段注入实际字段，
    空白/超长/非法枚举/naive 时间 → 固定摘要 ValidationError；WEB 证据
    检查不得掩盖 enum/extracted_by 等前置校验。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    cases: list[tuple[str, str, object]] = [
        ("空白 tenant", "tenant_id", ""),  # 实际空白字符串租户
        ("tenant 超 32", "tenant_id", "t" * 33),
        ("空白 entity", "entity_name", "   "),
        ("entity 超 200", "entity_name", "e" * 201),
        ("空白 raw_observation", "raw_observation", "   "),
        ("空白 source_id", "source_id", "   "),
        ("source_id 超 200", "source_id", "s" * 201),
        ("空白 extracted_by", "extracted_by", "   "),
        ("extracted_by 超 64", "extracted_by", "x" * 65),
        ("非法 signal_type", "signal_type", "bogus_type"),
        ("非法 source_type", "source_type", "bogus_source"),
        ("前后空白 source_id", "source_id", " sha256:pagehash001 "),
    ]
    for label, field, value in cases:
        with pytest.raises(ValidationError):
            if field == "tenant_id":
                await service.capture_signal(value, _request())  # type: ignore[arg-type]
            else:
                await service.capture_signal(
                    tenant, _request(**{field: value})  # type: ignore[arg-type]
                )
    # naive observed_at
    with pytest.raises(ValidationError):
        await service.capture_signal(tenant, _request(observed_at=NOW.replace(tzinfo=None)))
    assert await _signal_rows(factory, tenant) == []


async def test_capture_outbox_metadata_only_no_marker_leak(
    demand_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """outbox payload 只含既有 schema 键；生产内容 marker 不进 outbox/log/error。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    caplog.clear()

    await service.capture_signal(tenant, _request())
    events = await _outbox_events(factory, tenant)
    assert len(events) == 1
    payload = dict(events[0].event_payload)
    assert set(payload) <= {
        "tenant_id",
        "occurred_at",
        "run_id",
        "signal_id",
        "entity_name",
        "signal_type",
        "source_url",
    }
    blob = str(payload)
    assert OBSERVATION_MARKER not in blob
    assert NEED_MARKER not in blob
    assert "pagehash001" not in blob
    assert caplog.records == []


async def test_capture_publish_failure_rolls_back_signal(demand_db: AsyncEngine) -> None:
    """bus 发布失败 → 整个 UoW 回滚，业务行不落库、outbox 不变。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork
    impl_type = importlib.import_module("domains.demand.service_impl").DemandServiceImpl

    class _FailingBus:
        async def publish(self, event: object) -> None:
            raise RuntimeError("bus down")

        async def publish_many(self, events: list[object]) -> None:
            raise RuntimeError("bus down")

    class _BusFailureUoW:
        def __init__(self) -> None:
            self._inner = uow_type(factory, tenant, now=clock.now)

        async def __aenter__(self):
            await self._inner.__aenter__()
            self._inner.bus = _FailingBus()  # type: ignore[assignment]
            return self._inner

        async def __aexit__(self, *args: object):
            return await self._inner.__aexit__(*args)

    service = impl_type(lambda _t: _BusFailureUoW(), now=clock.now)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="bus down"):
        await service.capture_signal(tenant, _request())
    assert await _signal_rows(factory, tenant) == []  # 业务行回滚
    assert await _outbox_events(factory, tenant) == []


async def test_discard_captured_to_discarded_persists_first_reason(
    demand_db: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """captured→discarded：落 first reason；status/reason 往返；成功路径
    不新增 outbox 事件（仅 capture 的 1 条）且全程零日志。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    caplog.clear()

    signal_id = await service.capture_signal(tenant, _request())
    await service.discard_signal(tenant, signal_id, "noise")
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].status == "discarded"
    assert rows[0].discard_reason == "noise"
    events = await _outbox_events(factory, tenant)
    assert len(events) == 1  # 仅 capture 事件；discard 不写 outbox
    assert caplog.records == []  # 成功路径零日志


async def test_discard_idempotent_same_reason_and_conflict_on_different(
    demand_db: AsyncEngine,
) -> None:
    """同 reason 幂等 no-op；不同 reason → InvalidStateTransition 且 first 保留。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)

    signal_id = await service.capture_signal(tenant, _request())
    await service.discard_signal(tenant, signal_id, "noise")
    await service.discard_signal(tenant, signal_id, "noise")  # 幂等
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1 and rows[0].discard_reason == "noise"
    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.discard_signal(tenant, signal_id, "duplicate")
    assert str(exc_info.value) == "丢弃原因冲突，拒绝覆盖"
    rows = await _signal_rows(factory, tenant)
    assert rows[0].discard_reason == "noise"  # first reason 保留


async def test_discard_linked_rejected(demand_db: AsyncEngine) -> None:
    """LINKED_TO_HYPOTHESIS → InvalidStateTransition（保护证据链）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    tables = importlib.import_module("infra.db.tables")
    # 直插 linked 行（模拟假设已关联）
    signal_id = new_id("sig")
    async with factory() as session:
        session.add(
            tables.DemandSignalRow(
                tenant_id=str(tenant),
                signal_id=signal_id,
                signal_type="product_line_expansion",
                entity_name="Acme Manufacturing",
                raw_observation=OBSERVATION_MARKER,
                observed_at=NOW,
                status="linked_to_hypothesis",
                source_type="web_page",
                source_id="sha256:pagehash001",
                extracted_by="model-v1",
                extracted_at=NOW,
                source_url="https://example.com/acme",
                page_hash="sha256:pagehash001",
            )
        )
        await session.commit()
    clock = MutableClock(NOW)
    service = _service(factory, tenant, clock)
    with pytest.raises(InvalidStateTransition) as exc_info:
        await service.discard_signal(tenant, signal_id, "noise")
    assert str(exc_info.value) == "已关联假设的信号不可丢弃"
    rows = await _signal_rows(factory, tenant)
    assert rows[0].status == "linked_to_hypothesis"  # 未改动


async def test_discard_missing_or_cross_tenant_invisible(
    demand_db: AsyncEngine,
) -> None:
    """不存在/跨租户不可见 → 统一 ValidationError("需求信号不存在")。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant_a, clock)
    service_b = _service(factory, tenant_b, clock)

    ghost = new_id("sig")
    with pytest.raises(ValidationError) as exc_info:
        await service_a.discard_signal(tenant_a, ghost, "noise")
    assert str(exc_info.value) == "需求信号不存在"

    signal_id = await service_a.capture_signal(tenant_a, _request())
    with pytest.raises(ValidationError) as exc_info:
        await service_b.discard_signal(tenant_b, signal_id, "noise")
    assert str(exc_info.value) == "需求信号不存在"
    rows_a = await _signal_rows(factory, tenant_a)
    assert rows_a[0].status == "captured"  # B 的操作无副作用


async def test_discard_concurrent_same_reason_both_succeed_single_reason(
    demand_db: AsyncEngine,
) -> None:
    """并发同 reason → 双成功且单一 reason 落库。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)

    signal_id = await service_a.capture_signal(tenant, _request())
    results = await asyncio.gather(
        service_a.discard_signal(tenant, signal_id, "noise"),
        service_b.discard_signal(tenant, signal_id, "noise"),
        return_exceptions=True,
    )
    assert all(r is None for r in results), results
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 1
    assert rows[0].status == "discarded" and rows[0].discard_reason == "noise"


async def test_discard_concurrent_different_reasons_one_wins_one_conflict(
    demand_db: AsyncEngine,
) -> None:
    """并发不同 reason → 一胜一 InvalidStateTransition，first reason 保留。

    确定性编排（消除 SELECT 时序竞态）：A 的 discard 持 FOR UPDATE 行锁且
    未提交（固定 1s 窗口）时 B 才发起 discard——有锁则 B 的 SELECT 阻塞至
    A 提交（读到已 discarded 行 → 冲突）；无锁则 B 在窗口内立即读到转换前
    captured（双成功，本测试必 RED）。B 的冲突仍由服务层 InvalidStateTransition
    承担。
    """
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    service_a = _service(factory, tenant, clock)
    service_b = _service(factory, tenant, clock)
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork

    signal_id = await service_a.capture_signal(tenant, _request())
    a_locked = asyncio.Event()

    async def run_a() -> None:
        async with uow_type(factory, tenant, now=clock.now) as uow:
            snapshot = await uow.signals.discard(
                tenant, _models.DemandSignalId(signal_id), "noise"
            )
            assert snapshot is not None
            a_locked.set()
            await asyncio.sleep(1.0)  # 持锁窗口：B 的 discard 必然落在锁期内

    async def run_b() -> object:
        await a_locked.wait()  # A 已持锁未提交
        return await service_b.discard_signal(tenant, signal_id, "duplicate")

    results = await asyncio.gather(run_a(), run_b(), return_exceptions=True)
    assert [r for r in results if r is not None]  # 恰一方冲突
    assert any(isinstance(r, InvalidStateTransition) for r in results)
    rows = await _signal_rows(factory, tenant)
    assert rows[0].status == "discarded"
    assert rows[0].discard_reason in {"noise", "duplicate"}


async def test_discard_snapshot_semantics_and_rollback(demand_db: AsyncEngine) -> None:
    """repo 返回转换前快照；失败注入 → 整事务回滚（既有 committed 状态保留）。"""
    factory = async_sessionmaker(demand_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    clock = MutableClock(NOW)
    uow_type = importlib.import_module("infra.db.demand_uow").SqlAlchemyDemandUnitOfWork

    # 快照语义：repo 直接调用，captured 行返回快照 status=captured，但 DB 已更新
    signal = _models.DemandSignal(
        signal_id=_models.DemandSignalId(new_id("sig")),
        tenant_id=tenant,
        signal_type=_models.SignalType.PRODUCT_LINE_EXPANSION,
        entity_name="Acme Manufacturing",
        raw_observation=OBSERVATION_MARKER,
        observed_at=NOW,
        provenance=_provenance_for(),
    )
    async with uow_type(factory, tenant, now=clock.now) as uow:
        assert await uow.signals.add(signal) is True
    async with uow_type(factory, tenant, now=clock.now) as uow:
        snapshot = await uow.signals.discard(tenant, signal.signal_id, "noise")
        assert snapshot is not None
        assert snapshot.status is _models.SignalStatus.CAPTURED  # 转换前快照
    rows = await _signal_rows(factory, tenant)
    assert rows[0].status == "discarded"  # UoW 提交后 DB 已是 discarded

    # 回滚：新 CAPTURED 信号在同一失败事务内被 discard → 整个 UoW 回滚，
    # fresh 保持 captured 且无 reason；既有 committed 行（discarded/noise）不变
    fresh = _models.DemandSignal(
        signal_id=_models.DemandSignalId(new_id("sig")),
        tenant_id=tenant,
        signal_type=_models.SignalType.PRODUCT_LINE_EXPANSION,
        entity_name="Beta Manufacturing",
        raw_observation=OBSERVATION_MARKER,
        observed_at=NOW,
        provenance=_provenance_for(),
    )
    async with uow_type(factory, tenant, now=clock.now) as uow:
        assert await uow.signals.add(fresh) is True  # 提交：fresh 为 CAPTURED
    with pytest.raises(RuntimeError, match="boom"):
        async with uow_type(factory, tenant, now=clock.now) as uow:
            await uow.signals.discard(tenant, fresh.signal_id, "noise2")
            raise RuntimeError("boom")
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 2
    by_id = {str(row.signal_id): row for row in rows}
    assert by_id[str(signal.signal_id)].status == "discarded"  # 既有行不变
    assert by_id[str(signal.signal_id)].discard_reason == "noise"
    assert by_id[str(fresh.signal_id)].status == "captured"  # 回滚：未落 discarded
    assert by_id[str(fresh.signal_id)].discard_reason is None  # 回滚：无 reason
