"""S3-9 notification dedup 持久化 RED 测试（integration）：0006 迁移 + Postgres dedup store。

行为断言，不依赖实现细节：
- 0006 upgrade：创建 ``notification_deliveries``（delivery_id PK、tenant_id、
  dedup_key VARCHAR(200)、channel_name VARCHAR(64)、status VARCHAR(16) DEFAULT
  'pending'、attempts INT DEFAULT 0、next_attempt_at、last_error、delivered_at）；
  全表带 ``tenant_id``（硬边界 8）。
- ``UNIQUE(tenant_id, dedup_key, channel_name)``：部分渠道失败可 durable resume。
- 0006 downgrade round-trip：回到 0005 表消失，再 upgrade head 恢复。
- ``PostgresNotificationDedupStore``（``infra/db/repositories/notifications``，
  RED 时未建）：``should_dispatch`` 建 durable 行并**以 ``next_attempt_at`` 作有限
  租约原子认领**（并发同 (tenant, dedup, channel) 仅一个 True；租约期内抑制重投、
  租约到期可重试——进程崩溃后可恢复）；``record_failure`` 递增 attempts、置正确定
  性退避（next_attempt_at，随 attempts 增长）、写脱敏 last_error（不含异常消息/
  payload/凭证）；``record_success`` 标记 delivered 并清空重试字段；部分渠道失败仅
  续投失败渠道（不重复投递已成功渠道）；租户隔离失败关闭（查询显式带 tenant_id）。

RED 前置：0006 迁移/仓储未建 → 行为失败（非收集错误）。全部用本地引擎
（create_engine_from + try/finally dispose），不触发 session 级 async fixture 的
teardown 问题。禁止打印/记录任何连接串（db_url 经 conftest RedactedUrl 脱敏 repr）。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import TransientError
from shared.schemas.identifiers import TenantId

_REPO_ROOT = Path(__file__).resolve().parents[2]

# notification_deliveries 全列（Schema 附录 0006）。
NOTIFICATION_COLUMNS: set[str] = {
    "delivery_id",
    "tenant_id",
    "dedup_key",
    "channel_name",
    "status",
    "attempts",
    "claim_token",
    "next_attempt_at",
    "last_error",
    "delivered_at",
}

_INSERT_DELIVERY = text(
    "INSERT INTO notification_deliveries (delivery_id, tenant_id, dedup_key, channel_name) "
    "VALUES (:delivery_id, :tenant_id, :dedup_key, :channel_name)"
)

_INSERT_DELIVERY_RETURNING = text(
    "INSERT INTO notification_deliveries (delivery_id, tenant_id, dedup_key, channel_name) "
    "VALUES (:delivery_id, :tenant_id, :dedup_key, :channel_name) "
    "RETURNING status, attempts"
)

_MODULE_BY_SYMBOL = {
    "PostgresNotificationDedupStore": "infra.db.repositories.notifications",
    "NotificationDedupStore": "notification_gateway.dedup",
}

_NOW = datetime(2026, 8, 9, 8, 0, 0, tzinfo=UTC)


def _load(symbol: str) -> Any:
    """按模块字符串导入符号；缺失转行为失败（RED，非收集错误）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _run_alembic(db_url: str, *command: str) -> None:
    """在仓库根运行 ``alembic <command>``；仅经 env 注入 DATABASE_URL，不输出连接内容。"""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        ["alembic", *command],
        capture_output=True,
        check=False,
        env=env,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, f"alembic {' '.join(command)} 失败（不输出连接内容）"


def _sync_table_names(conn: Connection) -> list[str]:
    """同步 inspect：当前 schema 的表名列表。"""
    return inspect(conn).get_table_names()


def _sync_columns(conn: Connection, table: str) -> set[str]:
    """同步 inspect：指定表的列名集合。"""
    return {str(col["name"]) for col in inspect(conn).get_columns(table)}


def _sync_unique_sets(conn: Connection, table: str) -> set[frozenset[str]]:
    """同步 inspect：指定表的唯一约束/唯一索引列组合（不含 PK）。"""
    sets: set[frozenset[str]] = set()
    for uc in inspect(conn).get_unique_constraints(table):
        column_names = uc["column_names"]
        if column_names is not None:
            sets.add(frozenset(column_names))
    for idx in inspect(conn).get_indexes(table):
        index_cols = idx["column_names"]
        if index_cols is not None:
            sets.add(frozenset(col for col in index_cols if col is not None))
    return sets


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(_sync_table_names))


async def _columns(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(_sync_columns, table)


async def _unique_column_sets(engine: AsyncEngine, table: str) -> set[frozenset[str]]:
    async with engine.connect() as conn:
        return await conn.run_sync(_sync_unique_sets, table)


async def _assert_notification_deliveries_exists(engine: AsyncEngine) -> None:
    """前置：0006 已建 notification_deliveries 表（RED 阶段缺表 → 干净断言失败）。"""
    names = await _table_names(engine)
    assert "notification_deliveries" in names, "RED：0006 未创建 notification_deliveries 表"


async def _assert_delivery_insert_rejected(
    engine: AsyncEngine,
    params: dict[str, object],
    label: str,
) -> None:
    """INSERT notification_deliveries 应被约束（唯一/NOT NULL）拒绝。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(_INSERT_DELIVERY, params)
        except IntegrityError:
            await conn.rollback()
        else:
            await conn.rollback()
            pytest.fail(label)


async def _delivery_row(
    engine: AsyncEngine,
    tenant_id: str,
    dedup_key: str,
    channel_name: str,
) -> dict[str, object] | None:
    """读 notification_deliveries 行；不存在返回 None。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT status, attempts, next_attempt_at, last_error, delivered_at "
                "FROM notification_deliveries "
                "WHERE tenant_id = :t AND dedup_key = :k AND channel_name = :c"
            ),
            {"t": tenant_id, "k": dedup_key, "c": channel_name},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None


async def _count_deliveries(
    engine: AsyncEngine,
    tenant_id: str,
    dedup_key: str,
    channel_name: str,
) -> int:
    """notification_deliveries 中该 (tenant, dedup, channel) 的行数。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM notification_deliveries "
                "WHERE tenant_id = :t AND dedup_key = :k AND channel_name = :c"
            ),
            {"t": tenant_id, "k": dedup_key, "c": channel_name},
        )
        return result.scalar_one()


class _Clock:
    """可推进的假时钟：让 backoff 调度时间确定可测。"""

    def __init__(self, start: datetime = _NOW) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, **kw: float) -> None:
        self._t = self._t + timedelta(**kw)


def _make_store(
    factory: async_sessionmaker[AsyncSession],
    *,
    clock: _Clock | None = None,
) -> Any:
    """构造 PostgresNotificationDedupStore；``now`` 为可选注入（测试契约）。"""
    PostgresNotificationDedupStore = _load("PostgresNotificationDedupStore")
    if clock is not None:
        return PostgresNotificationDedupStore(factory, now=clock.now)
    return PostgresNotificationDedupStore(factory)


# --- NotificationDedupStore Protocol 契约（RED：dedup.py 未建）---------------------


def test_dedup_store_protocol_contract() -> None:
    """NotificationDedupStore Protocol 定义 fencing 与永久拒绝所需方法。"""
    NotificationDedupStore = _load("NotificationDedupStore")
    for name in (
        "should_dispatch",
        "record_failure",
        "record_success",
        "record_rejection",
    ):
        assert hasattr(NotificationDedupStore, name), f"NotificationDedupStore 缺 {name}"


# --- 0002→0005→0006 upgrade / downgrade round-trip -------------------------------


async def test_0006_upgrade_creates_notification_deliveries(db_url: str) -> None:
    """0006 upgrade：notification_deliveries 全列 + tenant_id + status/attempts 默认值。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0005")  # 基线：0006 之前无该表
        names = await _table_names(engine)
        assert "notification_deliveries" not in names

        _run_alembic(db_url, "upgrade", "head")  # 应用 0006
        await _assert_notification_deliveries_exists(engine)
        cols = await _columns(engine, "notification_deliveries")
        assert NOTIFICATION_COLUMNS <= cols, f"notification_deliveries 缺列：{sorted(NOTIFICATION_COLUMNS - cols)}"
        assert "tenant_id" in cols  # 硬边界 8：全表带 tenant_id

        # status/attempts 走 server_default：pending / 0
        async with engine.begin() as conn:
            result = await conn.execute(
                _INSERT_DELIVERY_RETURNING,
                {
                    "delivery_id": "del-0006-1",
                    "tenant_id": "t0006",
                    "dedup_key": "key-1",
                    "channel_name": "structured_log",
                },
            )
            row = result.mappings().one()
            assert row["status"] == "pending"
            assert row["attempts"] == 0
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0006_unique_tenant_dedup_channel(db_url: str) -> None:
    """UNIQUE(tenant_id, dedup_key, channel_name)：同租户同 dedup 同渠道拒重；
    不同渠道 / 不同租户允许（部分渠道失败可 durable resume）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0005")
        _run_alembic(db_url, "upgrade", "head")
        await _assert_notification_deliveries_exists(engine)

        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-u1", "tenant_id": "tA", "dedup_key": "key-1", "channel_name": "structured_log"},
            )
        # 同租户同 dedup 同渠道重复 → UNIQUE 拒绝
        await _assert_delivery_insert_rejected(
            engine,
            {"delivery_id": "del-u1x", "tenant_id": "tA", "dedup_key": "key-1", "channel_name": "structured_log"},
            "同租户同 dedup 同渠道重复应被 UNIQUE 拒绝",
        )
        # 同租户同 dedup 不同渠道 → 允许
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-u2", "tenant_id": "tA", "dedup_key": "key-1", "channel_name": "email"},
            )
        # 不同租户同 dedup 同渠道 → 允许
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-u3", "tenant_id": "tB", "dedup_key": "key-1", "channel_name": "structured_log"},
            )
        uniq = await _unique_column_sets(engine, "notification_deliveries")
        assert frozenset({"tenant_id", "dedup_key", "channel_name"}) in uniq
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0006_downgrade_removes_table_roundtrip(db_url: str) -> None:
    """0006 downgrade round-trip：回到 0005 表消失，再 upgrade head 恢复。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "upgrade", "head")
        await _assert_notification_deliveries_exists(engine)
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-rt1", "tenant_id": "tRT", "dedup_key": "key-1", "channel_name": "structured_log"},
            )

        _run_alembic(db_url, "downgrade", "0005")
        names = await _table_names(engine)
        assert "notification_deliveries" not in names

        _run_alembic(db_url, "upgrade", "head")
        await _assert_notification_deliveries_exists(engine)
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


# --- PostgresNotificationDedupStore：durable 状态 / retry / 幂等 / 租户隔离 -------------


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    """函数级本地引擎（store 测试用），结束后 dispose。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_store_should_dispatch_creates_durable_row_and_suppresses_duplicate(
    engine_fx: AsyncEngine,
) -> None:
    """should_dispatch 建 durable 行（pending/attempts=0）并置有限租约；delivered 后抑制重复投递。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    store = _make_store(factory)
    key = "key-store-create-suppress"

    claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert claim is not None
    # 原子认领：租约期内再次调用被抑制（不重复投递）；不建重复行（UNIQUE + ON CONFLICT 语义）
    assert not await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert await _count_deliveries(engine_fx, "tA", key, "structured_log") == 1

    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row is not None and row["status"] == "pending"  # 认领不改 status
    assert row["attempts"] == 0
    assert row["next_attempt_at"] is not None  # 有限租约已置

    assert await store.record_success(
        TenantId("tA"), key, "structured_log", claim_token=claim
    )
    assert not await store.should_dispatch(TenantId("tA"), key, "structured_log")


async def test_store_lease_expires_allowing_retry_after_crash(engine_fx: AsyncEngine) -> None:
    """有限租约：认领后租约期内他人不可重投；进程崩溃后租约到期可重新认领重试。

    ``status`` 保持 ``pending``——``next_attempt_at`` 即租约，不新增 claimed 状态。
    """
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-lease-expiry"

    first_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert first_claim is not None
    # 租约期内：不重复认领（进程未崩溃但仍在投递，他人不可抢）
    assert not await store.should_dispatch(TenantId("tA"), key, "structured_log")
    # 进程崩溃后：租约到期可重新认领重试
    clock.advance(seconds=86400)
    second_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert second_claim is not None and second_claim != first_claim

    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row is not None
    assert row["status"] == "pending"  # 认领不改 status，next_attempt_at 即租约
    assert row["next_attempt_at"] is not None


async def test_store_concurrent_claim_allows_only_one_true(engine_fx: AsyncEngine) -> None:
    """真实 Postgres 并发认领：同 (tenant, dedup, channel) 并发 should_dispatch
    仅一个 True（原子条件 UPDATE 抢占到期行），杜绝 check-then-act 双重投递。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-concurrent-claim"
    # 预置 pending 行（聚焦认领原子性，避免 INSERT 竞争叠加干扰）
    async with engine_fx.begin() as conn:
        await conn.execute(
            _INSERT_DELIVERY,
            {
                "delivery_id": "del-conc-claim",
                "tenant_id": "tA",
                "dedup_key": key,
                "channel_name": "structured_log",
            },
        )

    results = await asyncio.gather(
        store.should_dispatch(TenantId("tA"), key, "structured_log"),
        store.should_dispatch(TenantId("tA"), key, "structured_log"),
    )
    assert sum(claim is not None for claim in results) == 1  # 原子认领：仅一个 claim

    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row is not None
    assert row["status"] == "pending"  # 认领不改 status
    assert row["next_attempt_at"] is not None  # 有限租约已置


async def test_store_stale_claim_cannot_overwrite_new_owner_result(
    engine_fx: AsyncEngine,
) -> None:
    """真实 Postgres fencing：租约到期后旧 owner 的失败不能覆盖新 owner 的成功。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-stale-claim-fencing"

    old_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert isinstance(old_claim, str) and old_claim
    clock.advance(seconds=86400)
    new_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert isinstance(new_claim, str) and new_claim != old_claim

    stale, current = await asyncio.gather(
        store.record_failure(
            TenantId("tA"),
            key,
            "structured_log",
            claim_token=old_claim,
            error=TransientError("old owner late failure"),
        ),
        store.record_success(
            TenantId("tA"), key, "structured_log", claim_token=new_claim
        ),
    )

    assert stale is False
    assert current is True
    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row == {
        "status": "delivered",
        "attempts": 0,
        "next_attempt_at": None,
        "last_error": None,
        "delivered_at": clock.now(),
    }


async def test_store_stale_completion_cannot_override_new_owner_failure(
    engine_fx: AsyncEngine,
) -> None:
    """真实 Postgres fencing：旧 owner 的晚到成功不能把新 owner 的失败伪装为 delivered。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-stale-completion-fencing"

    old_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert isinstance(old_claim, str) and old_claim
    clock.advance(seconds=86400)
    new_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert isinstance(new_claim, str) and new_claim != old_claim

    stale, current = await asyncio.gather(
        store.record_success(
            TenantId("tA"), key, "structured_log", claim_token=old_claim
        ),
        store.record_failure(
            TenantId("tA"),
            key,
            "structured_log",
            claim_token=new_claim,
            error=TransientError("new owner failure"),
        ),
    )

    assert stale is False
    assert current is True
    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row is not None
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["delivered_at"] is None
    assert row["next_attempt_at"] is not None
    assert row["last_error"] == "TransientError"


async def test_store_failure_increments_attempts_backoff_and_sanitized_error(
    engine_fx: AsyncEngine,
) -> None:
    """record_failure：attempts 递增、next_attempt_at 置正退避、last_error 脱敏；
    退避期内 should_dispatch False，到点后恢复。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-store-failure-backoff"
    secret = "backend" + "-secret-token"

    claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert claim is not None
    assert await store.record_failure(
        TenantId("tA"), key, "structured_log", claim_token=claim, error=RuntimeError(secret)
    )

    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row is not None
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    delay = row["next_attempt_at"] - clock.now()
    assert delay > timedelta(0)  # 正退避
    assert row["last_error"] is not None
    assert len(row["last_error"]) < 128  # 脱敏固定值（短），非异常消息全文
    assert secret not in row["last_error"]  # 不含异常消息/凭证文本

    # 退避期内：不重复投递
    assert not await store.should_dispatch(TenantId("tA"), key, "structured_log")
    # 到点后：可续投
    clock.advance(seconds=86400)
    assert await store.should_dispatch(TenantId("tA"), key, "structured_log")


async def test_store_backoff_grows_deterministically(engine_fx: AsyncEngine) -> None:
    """TransientError 正退避确定性且随 attempts 增长（delay2 > delay1 > 0）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-store-backoff-grows"

    first_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert first_claim is not None
    t0 = clock.now()
    assert await store.record_failure(
        TenantId("tA"), key, "structured_log", claim_token=first_claim, error=TransientError("down")
    )
    d1 = (await _delivery_row(engine_fx, "tA", key, "structured_log"))["next_attempt_at"] - t0
    assert d1 > timedelta(0)

    clock.advance(seconds=86400)
    second_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert second_claim is not None
    assert await store.record_failure(
        TenantId("tA"), key, "structured_log", claim_token=second_claim, error=TransientError("down")
    )
    d2 = (await _delivery_row(engine_fx, "tA", key, "structured_log"))["next_attempt_at"] - clock.now()
    assert d2 > d1 > timedelta(0)


async def test_store_success_clears_retry_fields(engine_fx: AsyncEngine) -> None:
    """record_success：标记 delivered、写 delivered_at、清空 next_attempt_at/last_error；
    之后 should_dispatch False（不重复投递）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-store-success-clears"

    first_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert first_claim is not None
    assert await store.record_failure(
        TenantId("tA"), key, "structured_log", claim_token=first_claim, error=TransientError("down")
    )
    clock.advance(seconds=86400)
    second_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert second_claim is not None
    assert await store.record_success(
        TenantId("tA"), key, "structured_log", claim_token=second_claim
    )

    row = await _delivery_row(engine_fx, "tA", key, "structured_log")
    assert row is not None
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None
    assert row["next_attempt_at"] is None
    assert row["last_error"] is None
    assert not await store.should_dispatch(TenantId("tA"), key, "structured_log")


async def test_partial_channel_failure_retries_only_failed_channel(engine_fx: AsyncEngine) -> None:
    """部分渠道失败可 durable resume：仅续投失败渠道，不重复投递已成功渠道。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    store = _make_store(factory, clock=clock)
    key = "key-partial-channel-retry"

    failed_claim = await store.should_dispatch(TenantId("tA"), key, "ch-a")
    assert failed_claim is not None
    assert await store.record_failure(
        TenantId("tA"), key, "ch-a", claim_token=failed_claim, error=TransientError("down")
    )
    succeeded_claim = await store.should_dispatch(TenantId("tA"), key, "ch-b")
    assert succeeded_claim is not None
    assert await store.record_success(
        TenantId("tA"), key, "ch-b", claim_token=succeeded_claim
    )

    # 失败渠道在退避期、成功渠道已投递 → 均不可重复投递
    assert not await store.should_dispatch(TenantId("tA"), key, "ch-a")
    assert not await store.should_dispatch(TenantId("tA"), key, "ch-b")

    # 到点后：只有失败渠道可续投；成功渠道保持 delivered 抑制
    clock.advance(seconds=86400)
    retry_claim = await store.should_dispatch(TenantId("tA"), key, "ch-a")
    assert retry_claim is not None
    assert not await store.should_dispatch(TenantId("tA"), key, "ch-b")

    assert await store.record_success(
        TenantId("tA"), key, "ch-a", claim_token=retry_claim
    )
    assert not await store.should_dispatch(TenantId("tA"), key, "ch-a")


async def test_store_tenant_isolation_fail_closed(engine_fx: AsyncEngine) -> None:
    """租户隔离失败关闭：A 的操作不影响 B；B 的 delivered 也不掩盖 A 的待投递状态。

    dedup key 用本测试独有值：集成测试共享 session 级容器库，其他测试在同一
    ``(tenant, dedup_key, channel)`` 上留下的 delivered 状态会污染本测试断言
    （跨测试状态泄漏），故不与它们共用 key。
    """
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    store = _make_store(factory)
    key = "key-iso-tenant-isolation"

    tenant_a_claim = await store.should_dispatch(TenantId("tA"), key, "structured_log")
    assert tenant_a_claim is not None
    assert await store.record_success(
        TenantId("tA"), key, "structured_log", claim_token=tenant_a_claim
    )

    # B 不受 A 影响：同 key 同渠道在 B 仍可投递
    tenant_b_claim = await store.should_dispatch(TenantId("tB"), key, "structured_log")
    assert tenant_b_claim is not None
    assert await store.record_success(
        TenantId("tB"), key, "structured_log", claim_token=tenant_b_claim
    )
    assert not await store.should_dispatch(TenantId("tB"), key, "structured_log")

    # A 的 delivered 状态未被 B 操作掩盖
    assert not await store.should_dispatch(TenantId("tA"), key, "structured_log")
    # A 的行数不受 B 影响（B 只应有自己的行）
    assert await _count_deliveries(engine_fx, "tA", key, "structured_log") == 1
    assert await _count_deliveries(engine_fx, "tB", key, "structured_log") == 1
