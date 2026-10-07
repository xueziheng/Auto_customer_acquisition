"""S3-8 outbox 投递持久化迁移 RED 测试（0005 自管 guard，不改 0002）。

行为断言，不依赖实现细节：
- 0005 upgrade：``outbox_events`` 加列 ``next_attempt_at``/``last_error``；
  加 ``UNIQUE(tenant_id, event_id)``；DROP 重建 status CHECK 使其白名单含
  ``'pending'/'delivered'/'dead'``；``CREATE OR REPLACE`` guard 函数只允许
  ``status/delivered_at/attempt/next_attempt_at/last_error`` 变化；新建
  ``outbox_deliveries``（durable per-handler 状态 +
  ``UNIQUE(tenant_id, event_id, handler_name)`` + 复合 FK → outbox_events）。
- 0005 downgrade：恢复 0002 精确语义——加列/新表消失、status CHECK 回到
  仅 ``'pending'/'delivered'``、guard 回到仅 ``status/delivered_at`` 可变。
- 不修改已发布 ``0002_opportunities``；0005 自管兼容修复（F3/R7）。

**deliverer 行为**（``infra/db/outbox_delivery.OutboxDeliverer``，RED 时模块未建）：
- handler registry 与 EVENT_REGISTRY 分工：事件类型白名单 ≠ 订阅 handler，handler
  名显式注册；无注册 handler → event 标记 dead、``last_error`` 只写固定
  ``NO_REGISTERED_HANDLER`` + event_type（不含 payload）、记录结构化错误、不留
  pending 紧循环、不静默 delivered。
- event 仅当所有注册 handler 的 delivery 均 delivered 才 delivered；drain 用
  ``FOR UPDATE SKIP LOCKED``，多订阅者 crash/restart 后幂等续投、不重复已投递
  handler；TransientError 确定性正退避；永久错误/重试耗尽进可观测死信态且不毒化
  tight loop；未知事件类型保留 registry 失败语义（ValidationError）；租户过滤失败
  关闭；持久化错误全部脱敏（不含 payload/凭证文本）。

RED 前置：0005 迁移/OutboxDeliverer 未建 → 行为失败（非收集错误）。
全部用本地引擎（``create_engine_from`` + try/finally dispose），不触发 session 级
async fixture 的 teardown 问题。禁止打印/记录任何连接串（``db_url`` 经 conftest
的 RedactedUrl 脱敏 repr）。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from infra.db.tables import OutboxEventRow
from shared.errors import TransientError, ValidationError
from shared.events.catalog import (
    CountryPolicyVersionProposed,
    DomainEvent,
    OpportunityQualified,
    OpportunityWon,
)
from shared.schemas.identifiers import (
    CountryPolicyVersionId,
    EmployeeId,
    OpportunityId,
    RunId,
    TenantId,
)
from tests.migration_database_fixtures import (
    migration_database_url as _migration_database_url,
)

migration_database_url = _migration_database_url

_REPO_ROOT = Path(__file__).resolve().parents[2]

# outbox_deliveries 全列（Schema 附录 0005）。
DELIVERY_COLUMNS: set[str] = {
    "delivery_id",
    "tenant_id",
    "event_id",
    "handler_name",
    "status",
    "attempts",
    "next_attempt_at",
    "last_error",
    "delivered_at",
}

_INSERT_OUTBOX = text(
    "INSERT INTO outbox_events ("
    "event_id, tenant_id, event_type, event_payload, attempt, published_at, "
    "trace_id, run_id, occurred_at, status) "
    "VALUES (:event_id, :tenant_id, :event_type, CAST(:event_payload AS jsonb), "
    ":attempt, now(), :trace_id, :run_id, now(), :status)"
)

_INSERT_DELIVERY = text(
    "INSERT INTO outbox_deliveries (delivery_id, tenant_id, event_id, handler_name) "
    "VALUES (:delivery_id, :tenant_id, :event_id, :handler_name)"
)

_INSERT_DELIVERY_RETURNING = text(
    "INSERT INTO outbox_deliveries (delivery_id, tenant_id, event_id, handler_name) "
    "VALUES (:delivery_id, :tenant_id, :event_id, :handler_name) "
    "RETURNING status, attempts"
)


def _outbox_params(
    event_id: str,
    tenant_id: str,
    *,
    attempt: int = 1,
    status: str = "pending",
) -> dict[str, object]:
    """outbox_events 最小合法行参数（0002 列集合，0005 前后均适用）。"""
    return {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "event_type": "OpportunityWon",
        "event_payload": json.dumps({"opportunity_id": "opp-1", "closed_by": "emp-1"}),
        "attempt": attempt,
        "trace_id": f"trc-{event_id}",
        "run_id": f"run-{event_id}",
        "status": status,
    }


def _run_alembic(db_url: str, *command: str) -> None:
    """在仓库根运行 ``alembic <command>``；仅经 env 注入 DATABASE_URL，不输出连接内容。"""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *command],
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


async def _assert_outbox_insert(
    engine: AsyncEngine,
    params: dict[str, object],
    *,
    allowed: bool,
    label: str,
) -> None:
    """INSERT outbox_events：按 allowed 期望成功或被 CHECK/约束拒绝；独立事务回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(_INSERT_OUTBOX, params)
        except IntegrityError:
            await conn.rollback()
            if allowed:
                pytest.fail(label)
        else:
            await conn.rollback()
            if not allowed:
                pytest.fail(label)


async def _assert_outbox_update(
    engine: AsyncEngine,
    statement: str,
    params: dict[str, object],
    *,
    allowed: bool,
    label: str,
) -> None:
    """UPDATE outbox_events：按 allowed 期望被 guard 放行或拒绝；独立事务回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(text(statement), params)
        except DBAPIError:
            await conn.rollback()
            if allowed:
                pytest.fail(label)
        else:
            await conn.rollback()
            if not allowed:
                pytest.fail(label)


async def _assert_delivery_insert_rejected(
    engine: AsyncEngine,
    params: dict[str, object],
    label: str,
) -> None:
    """INSERT outbox_deliveries 应被约束（唯一/复合 FK/NOT NULL）拒绝。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(_INSERT_DELIVERY, params)
        except IntegrityError:
            await conn.rollback()
        else:
            await conn.rollback()
            pytest.fail(label)


async def _assert_outbox_deliveries_exists(engine: AsyncEngine) -> None:
    """前置：0005 已建 outbox_deliveries 表（RED 阶段缺表 → 干净断言失败）。"""
    names = await _table_names(engine)
    assert "outbox_deliveries" in names, "RED：0005 未创建 outbox_deliveries 表"


# --- 0002→0005 upgrade：schema / 约束 / guard ---------------------------------


async def test_0005_upgrade_adds_outbox_delivery_columns(migration_database_url: str) -> None:
    """0005 upgrade：outbox_events 加列 next_attempt_at/last_error（0002 基线无此列）。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")  # 基线：0005 之前的 outbox 状态
        cols = await _columns(engine, "outbox_events")
        assert "next_attempt_at" not in cols
        assert "last_error" not in cols

        _run_alembic(db_url, "upgrade", "head")  # 应用 0005
        cols = await _columns(engine, "outbox_events")
        assert "next_attempt_at" in cols
        assert "last_error" in cols
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0005_upgrade_creates_outbox_deliveries_table(migration_database_url: str) -> None:
    """0005 upgrade：outbox_deliveries 全列 + 默认值 + handler_name NOT NULL。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")
        _run_alembic(db_url, "upgrade", "head")
        await _assert_outbox_deliveries_exists(engine)
        cols = await _columns(engine, "outbox_deliveries")
        assert DELIVERY_COLUMNS <= cols, f"outbox_deliveries 缺列：{sorted(DELIVERY_COLUMNS - cols)}"
        assert "tenant_id" in cols  # 硬边界 8：全表带 tenant_id

        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params("evt-del-base", "tDel"))
        # status/attempts 走 server_default：pending / 0
        async with engine.begin() as conn:
            result = await conn.execute(
                _INSERT_DELIVERY_RETURNING,
                {
                    "delivery_id": "del-min-1",
                    "tenant_id": "tDel",
                    "event_id": "evt-del-base",
                    "handler_name": "h1",
                },
            )
            row = result.mappings().one()
            assert row["status"] == "pending"
            assert row["attempts"] == 0
        # handler_name NOT NULL
        await _assert_delivery_insert_rejected(
            engine,
            {
                "delivery_id": "del-null-h",
                "tenant_id": "tDel",
                "event_id": "evt-del-base",
                "handler_name": None,
            },
            "handler_name 为 NULL 应被 NOT NULL 拒绝",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0005_upgrade_status_check_allows_dead(migration_database_url: str) -> None:
    """0005 upgrade：status CHECK 重建为含 dead（0002 基线拒绝 dead）。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")
        await _assert_outbox_insert(
            engine,
            _outbox_params("evt-dead-base", "tDead", status="dead"),
            allowed=False,
            label="0002 基线：status='dead' 应被 CHECK 拒绝",
        )

        _run_alembic(db_url, "upgrade", "head")
        await _assert_outbox_insert(
            engine,
            _outbox_params("evt-dead-ok", "tDead", status="dead"),
            allowed=True,
            label="0005：status='dead' 应被 CHECK 允许",
        )
        await _assert_outbox_insert(
            engine,
            _outbox_params("evt-ship-1", "tDead", status="shipped"),
            allowed=False,
            label="0005：status='shipped' 应被 CHECK 拒绝",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0005_upgrade_guard_allows_delivery_fields(migration_database_url: str) -> None:
    """0005 upgrade：guard 允许 status/delivered_at/attempt/next_attempt_at/last_error，
    仍拒绝 event_type/event_payload（0002 基线拒绝 attempt 更新）。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params("evt-g-base", "tGuard"))
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET attempt = 2 WHERE event_id = :id",
            {"id": "evt-g-base"},
            allowed=False,
            label="0002 基线：attempt 更新应被 guard 拒绝",
        )

        _run_alembic(db_url, "upgrade", "head")
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET attempt = 2 WHERE event_id = :id",
            {"id": "evt-g-base"},
            allowed=True,
            label="0005：attempt 更新应被 guard 允许",
        )
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET next_attempt_at = now() WHERE event_id = :id",
            {"id": "evt-g-base"},
            allowed=True,
            label="0005：next_attempt_at 更新应被 guard 允许",
        )
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET last_error = :e WHERE event_id = :id",
            {"e": "sanitized", "id": "evt-g-base"},
            allowed=True,
            label="0005：last_error 更新应被 guard 允许",
        )
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET status = 'delivered', delivered_at = now() "
            "WHERE event_id = :id",
            {"id": "evt-g-base"},
            allowed=True,
            label="0005：status/delivered_at 更新应被 guard 允许",
        )
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET event_type = :t WHERE event_id = :id",
            {"t": "HandoffRequested", "id": "evt-g-base"},
            allowed=False,
            label="0005：event_type 更新应被 guard 拒绝",
        )
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET event_payload = CAST(:p AS jsonb) WHERE event_id = :id",
            {"p": json.dumps({"x": 1}), "id": "evt-g-base"},
            allowed=False,
            label="0005：event_payload 更新应被 guard 拒绝",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0005_upgrade_adds_unique_tenant_event(migration_database_url: str) -> None:
    """0005 upgrade：outbox_events 加 UNIQUE(tenant_id, event_id)（复合 FK 前置契约）。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")
        _run_alembic(db_url, "upgrade", "head")
        uniq = await _unique_column_sets(engine, "outbox_events")
        assert frozenset({"tenant_id", "event_id"}) in uniq, "0005 应加 UNIQUE(tenant_id, event_id)"
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_outbox_deliveries_unique_tenant_event_handler(migration_database_url: str) -> None:
    """outbox_deliveries UNIQUE(tenant_id, event_id, handler_name)：同租户同事件同 handler 拒重。

    outbox_events 的 ``event_id`` 是全局主键（0002），同一事件跨租户不共存；
    跨租户绑定由 ``test_outbox_deliveries_composite_fk_tenant_isolation`` 单独覆盖。
    """
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")
        _run_alembic(db_url, "upgrade", "head")
        await _assert_outbox_deliveries_exists(engine)
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params("evt-dup-1a", "tA"))
            await conn.execute(_INSERT_OUTBOX, _outbox_params("evt-dup-1b", "tB"))
        # 异租户不同事件（各自的 (tenant_id, event_id, handler_name) 互不冲突）→ 允许
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-a1", "tenant_id": "tA", "event_id": "evt-dup-1a", "handler_name": "h1"},
            )
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-b1", "tenant_id": "tB", "event_id": "evt-dup-1b", "handler_name": "h1"},
            )
        # 同租户同事件同 handler 重复 → UNIQUE 拒绝
        await _assert_delivery_insert_rejected(
            engine,
            {"delivery_id": "del-a1x", "tenant_id": "tA", "event_id": "evt-dup-1a", "handler_name": "h1"},
            "同租户同事件同 handler 重复应被 UNIQUE 拒绝",
        )
        # 同租户同事件不同 handler → 允许
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-a2", "tenant_id": "tA", "event_id": "evt-dup-1a", "handler_name": "h2"},
            )
        uniq = await _unique_column_sets(engine, "outbox_deliveries")
        assert frozenset({"tenant_id", "event_id", "handler_name"}) in uniq
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_outbox_deliveries_composite_fk_tenant_isolation(migration_database_url: str) -> None:
    """outbox_deliveries 复合 FK (tenant_id, event_id)→outbox_events：跨租户/悬空引用被拒。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")
        _run_alembic(db_url, "upgrade", "head")
        await _assert_outbox_deliveries_exists(engine)
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params("evt-fk-1", "tA"))
        # 同租户引用合法
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_DELIVERY,
                {"delivery_id": "del-fk-a1", "tenant_id": "tA", "event_id": "evt-fk-1", "handler_name": "h1"},
            )
        # 跨租户引用被拒（硬边界 8）
        await _assert_delivery_insert_rejected(
            engine,
            {"delivery_id": "del-fk-b1", "tenant_id": "tB", "event_id": "evt-fk-1", "handler_name": "h1"},
            "跨租户 delivery 引用应被复合 FK 拒绝",
        )
        # 悬空事件引用被拒
        await _assert_delivery_insert_rejected(
            engine,
            {"delivery_id": "del-fk-none", "tenant_id": "tA", "event_id": "evt-none", "handler_name": "h1"},
            "悬空事件引用应被复合 FK 拒绝",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


# --- 0005 downgrade：恢复 0002 精确语义 ---------------------------------------


async def test_0005_downgrade_restores_0002_semantics(migration_database_url: str) -> None:
    """0005 downgrade：加列/新表消失，status CHECK 与 guard 回到 0002 精确语义。"""
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "downgrade", "0004")  # 0005 之上 → 移除 0005
        cols = await _columns(engine, "outbox_events")
        assert "next_attempt_at" not in cols
        assert "last_error" not in cols
        names = await _table_names(engine)
        assert "outbox_deliveries" not in names

        # CHECK 回到 0002：dead 被拒
        await _assert_outbox_insert(
            engine,
            _outbox_params("evt-rest-dead", "tRest", status="dead"),
            allowed=False,
            label="downgrade 后 status='dead' 应被 0002 CHECK 拒绝",
        )
        # guard 回到 0002：attempt 更新被拒；status/delivered_at 更新仍允许
        async with engine.begin() as conn:
            await conn.execute(_INSERT_OUTBOX, _outbox_params("evt-rest-1", "tRest"))
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET attempt = 2 WHERE event_id = :id",
            {"id": "evt-rest-1"},
            allowed=False,
            label="downgrade 后 attempt 更新应被 0002 guard 拒绝",
        )
        await _assert_outbox_update(
            engine,
            "UPDATE outbox_events SET status = 'delivered', delivered_at = now() "
            "WHERE event_id = :id",
            {"id": "evt-rest-1"},
            allowed=True,
            label="downgrade 后 status/delivered_at 更新应仍被 0002 guard 允许",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_0005_downgrade_maps_dead_rows_to_pending(migration_database_url: str) -> None:
    """0005 downgrade 有存量 dead 行时：先确定性映射为 pending，再恢复 0002 精确约束。

    ``dead`` 是 0005 新增状态，0002 CHECK 只允许 ``pending/delivered``——直接重建
    CHECK 会因存量 dead 行违反新约束而失败。downgrade 必须先确定性映射
    dead→pending（dead 事件从未投递成功，回退为待投递是唯一语义无损选择），
    再恢复 0002 CHECK/guard。
    """
    db_url = migration_database_url
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        _run_alembic(db_url, "upgrade", "head")
        # 0005 下 status='dead' 合法（0005 CHECK 含 dead）
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_OUTBOX, _outbox_params("evt-dg-dead", "tDg", status="dead")
            )
            await conn.execute(
                _INSERT_OUTBOX,
                _outbox_params("evt-dg-del", "tDg", status="delivered"),
            )
            await conn.execute(
                _INSERT_OUTBOX, _outbox_params("evt-dg-pend", "tDg", status="pending")
            )

        _run_alembic(db_url, "downgrade", "0004")

        # dead 行确定性映射为 pending；delivered/pending 原状保留（均在 0002 语义内）
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT event_id, status FROM outbox_events "
                    "WHERE tenant_id = :t ORDER BY event_id"
                ),
                {"t": "tDg"},
            )
            rows = {row["event_id"]: row["status"] for row in result.mappings()}
        assert rows["evt-dg-dead"] == "pending"
        assert rows["evt-dg-del"] == "delivered"
        assert rows["evt-dg-pend"] == "pending"
        # 0002 精确约束已恢复：新插入 status='dead' 被拒
        await _assert_outbox_insert(
            engine,
            _outbox_params("evt-dg-dead2", "tDg", status="dead"),
            allowed=False,
            label="downgrade 后 status='dead' 应被 0002 CHECK 拒绝",
        )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


# --- OutboxDeliverer：handler registry / 投递语义 -------------------------------

_NOW = datetime(2026, 8, 9, 8, 0, 0, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "OutboxDeliverer": "infra.db.outbox_delivery",
    "NO_REGISTERED_HANDLER": "infra.db.outbox_delivery",
    "HANDLER_REMOVED_WHILE_PENDING": "infra.db.outbox_delivery",
    "PAYLOAD_DESERIALIZATION_FAILED": "infra.db.outbox_delivery",
}


def _load(symbol: str) -> Any:
    """按模块字符串导入符号；缺失转行为失败（RED 阶段未建 infra.db.outbox_delivery）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


class _Clock:
    """可推进的假时钟：让 backoff 调度时间确定可测。"""

    def __init__(self, start: datetime = _NOW) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, **kw: float) -> None:
        self._t = self._t + timedelta(**kw)


class _RecordingHandler:
    """EventHandler：记录调用的事件类型；可选抛错（TransientError 或永久错误）。"""

    def __init__(
        self,
        calls: list[str] | None = None,
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self._raise_exc = raise_exc

    async def handle(self, event: object) -> None:
        self.calls.append(type(event).__name__)
        if self._raise_exc is not None:
            raise self._raise_exc


class _OpportunityQualifiedShape(Protocol):
    """OpportunityQualified 最小静态形状（仅测试夹具用）。

    供 ``cast`` 后直接属性访问 ``opportunity_id``（避免 ruff B009 动态 getattr）；
    运行时值即 ``OpportunityId``（str 子类型），``object`` 标注保持形状最宽。
    """

    opportunity_id: object


class _SelectiveFailHandler:
    """EventHandler：仅对指定 opportunity_id 的事件抛永久错误；其余记录调用。"""

    def __init__(self, calls: list[str], *, fail_opp_ids: set[str]) -> None:
        self.calls = calls
        self._fail = fail_opp_ids

    async def handle(self, event: object) -> None:
        opp_id = str(cast(_OpportunityQualifiedShape, event).opportunity_id)
        self.calls.append(opp_id)
        if opp_id in self._fail:
            raise RuntimeError(f"permanent failure for {opp_id}")


class _FailingCommitSession(AsyncSession):
    """commit 必抛 DBAPIError 的 AsyncSession：模拟 drain 循环内 DB 级 commit 异常。

    仅用于 ``test_db_commit_failure_rolls_back_logs_and_continues`` 的注入式回归
    （select/execute 正常，只在 commit 阶段失败，最贴近真实提交失败点）。
    """

    async def commit(self) -> None:
        raise DBAPIError("COMMIT", {}, RuntimeError("db commit exploded"))


class _FailingSelectSession(AsyncSession):
    """execute 必抛 DBAPIError 的 AsyncSession：模拟 drain 在选中任何事件前就遭遇
    DB 级故障（首个 SELECT 即失败，``event_id`` 为空）。

    仅用于 ``test_db_select_failure_before_selection_terminates_cycle`` 的注入式
    回归：execute 恒失败 → 首个 SELECT 即抛 DB 异常，无 event 可推进循环，drain
    必须 fail closed 终止当前 cycle（而不是无限复现同一故障）。
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise DBAPIError(
            "SELECT",
            {},
            RuntimeError("db select exploded INTERNAL_DB_SELECT_LEAK_MARKER"),
        )


def _opp_qualified(tenant_id: str, opp_id: str) -> OpportunityQualified:
    """构造最小合法 OpportunityQualified（EVENT_REGISTRY 白名单内，bus 可发布）。"""
    return OpportunityQualified(
        tenant_id=TenantId(tenant_id),
        occurred_at=_NOW,
        run_id=RunId(f"run-{opp_id}"),
        opportunity_id=OpportunityId(opp_id),
        rank_bucket="high",
    )


# _make_deliverer 的会话类型参数：async_sessionmaker 的类型参数不变，显式泛型让
# _FailingCommitSession 工厂可传入（否则不能赋给 async_sessionmaker[AsyncSession]）。
def _make_deliverer[SessionT: AsyncSession](
    factory: async_sessionmaker[SessionT],
    tenant_id: str,
    *,
    clock: _Clock | None = None,
    max_attempts: int | None = None,
) -> Any:
    """构造 OutboxDeliverer；``max_attempts``/``now`` 为可选注入（测试契约）。"""
    OutboxDeliverer = _load("OutboxDeliverer")
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["now"] = clock.now
    if max_attempts is not None:
        kwargs["max_attempts"] = max_attempts
    return OutboxDeliverer(factory, TenantId(tenant_id), **kwargs)


async def _publish_event(engine: AsyncEngine, tenant_id: str, event: DomainEvent) -> str:
    """经真实 PostgresEventBus 落库一条 outbox 事件，返回其 event_id。"""
    from infra.db.outbox import PostgresEventBus

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        bus = PostgresEventBus(session, TenantId(tenant_id), now=lambda: _NOW)
        await bus.publish(event)
        await session.commit()
    finally:
        await session.close()

    marker = cast(str, cast(_OpportunityQualifiedShape, event).opportunity_id)
    verify = factory()
    try:
        rows = (
            await verify.execute(
                select(OutboxEventRow).where(OutboxEventRow.tenant_id == tenant_id)
            )
        ).scalars().all()
    finally:
        await verify.close()
    for row in rows:
        if row.event_payload.get("opportunity_id") == marker:
            return row.event_id
    raise AssertionError("publish 后未找到对应 outbox 行（测试夹具缺陷）")


async def _event_next_attempt_at(engine: AsyncEngine, event_id: str) -> datetime | None:
    """读 outbox_events 行的 next_attempt_at（未到期调度应保持 NULL/过去）。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT next_attempt_at FROM outbox_events WHERE event_id = :id"),
            {"id": event_id},
        )
        return cast(datetime | None, result.scalar_one())


async def _outbox_status(engine: AsyncEngine, event_id: str) -> tuple[str, str | None]:
    """读 outbox_events 行：status 与 last_error。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status, last_error FROM outbox_events WHERE event_id = :id"),
            {"id": event_id},
        )
        row = result.mappings().one()
        return cast(str, row["status"]), cast(str | None, row["last_error"])


async def _delivery_row(
    engine: AsyncEngine, event_id: str, handler_name: str
) -> dict[str, object] | None:
    """读 outbox_deliveries 行；不存在返回 None。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT status, attempts, next_attempt_at, last_error "
                "FROM outbox_deliveries WHERE event_id = :e AND handler_name = :h"
            ),
            {"e": event_id, "h": handler_name},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row is not None else None


async def _count_deliveries(engine: AsyncEngine, event_id: str) -> int:
    """outbox_deliveries 中该事件的行数。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM outbox_deliveries WHERE event_id = :e"),
            {"e": event_id},
        )
        return cast(int, result.scalar_one())


@pytest_asyncio.fixture
async def engine_fx(db_url: str) -> AsyncIterator[AsyncEngine]:
    """函数级本地引擎（deliverer 测试用），结束后 dispose。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


# --- handler registry 与 EVENT_REGISTRY 分工 -----------------------------------


async def test_handler_registry_distinct_from_event_registry(engine_fx: AsyncEngine) -> None:
    """EVENT_REGISTRY 白名单（publish 通过）≠ 订阅 handler：为别的类型注册不算订阅。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tDist", _opp_qualified("tDist", "opp-dist")
    )
    deliverer = _make_deliverer(factory, "tDist")
    # 只为 OpportunityWon 注册 handler：OpportunityQualified 仍无订阅 → NO_REGISTERED_HANDLER
    deliverer.register_handler(OpportunityWon, "only-won", _RecordingHandler())
    await deliverer.drain()
    NO_REGISTERED_HANDLER = cast(str, _load("NO_REGISTERED_HANDLER"))
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "dead"
    assert err is not None and NO_REGISTERED_HANDLER in err


async def test_duplicate_handler_name_registration_fails_closed(engine_fx: AsyncEngine) -> None:
    """同一 event_type 重复 handler_name 注册必须 fail closed（装配错误，抛 ValidationError）。

    重复 handler_name 会让两个 handler 映射到同一个 delivery 行（``UNIQUE
    (tenant_id, event_id, handler_name)`` 语义下投递状态被共享），是装配错误。
    不同 handler_name 的多个 handler（合法多订阅）与不同 event_type 下的同名
    handler（handler 名按 event_type 域隔离）仍可注册。
    """
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    deliverer = _make_deliverer(factory, "tDupName")
    deliverer.register_handler(OpportunityQualified, "h1", _RecordingHandler())
    with pytest.raises(ValidationError):
        deliverer.register_handler(OpportunityQualified, "h1", _RecordingHandler())
    # 合法：同类型不同 handler_name；不同类型同名 handler
    deliverer.register_handler(OpportunityQualified, "h2", _RecordingHandler())
    deliverer.register_handler(OpportunityWon, "h1", _RecordingHandler())


async def test_crash_after_proposal_commit_is_recovered_by_outbox_start(
    engine_fx: AsyncEngine,
) -> None:
    """API 未直接启动时，已提交 proposal event 仍创建唯一可运行流程。"""
    from infra.db.outbox import PostgresEventBus
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from workflows.country_policy_change import (
        CountryPolicyVersionProposedHandler,
        build_country_policy_change_definition,
    )

    class _StartOnly:
        async def execute(self, run) -> None:
            raise AssertionError(f"不应执行 start-only run {run.run_id}")

    tenant = TenantId("tenant-policy-recovery")
    version = CountryPolicyVersionId("cpp_01K00000000000000000000009")
    content_hash = "9" * 64
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    session = factory()
    try:
        await PostgresEventBus(session, tenant, now=lambda: _NOW).publish(
            CountryPolicyVersionProposed(
                tenant_id=tenant,
                occurred_at=_NOW,
                country_policy_version_id=version,
                country_key="synthetic recovery market",
                content_hash=content_hash,
                proposed_by=EmployeeId("emp_country_policy_recovery"),
            )
        )
        await session.commit()
    finally:
        await session.close()

    definition = build_country_policy_change_definition()
    start_only = _StartOnly()
    workflow = PostgresWorkflowEngine(
        factory,
        {step.handler_ref: start_only for step in definition.steps},
        now=lambda: _NOW,
    )
    workflow.register(definition)
    deliverer = _make_deliverer(factory, str(tenant))
    deliverer.register_handler(
        CountryPolicyVersionProposed,
        "country_policy_change.version_proposed",
        CountryPolicyVersionProposedHandler(workflow),
    )

    assert await deliverer.drain() == 1
    async with engine_fx.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT status, current_step, idempotency_key FROM workflow_runs "
                    "WHERE tenant_id=:tenant AND subject_ref=:version"
                ),
                {"tenant": str(tenant), "version": str(version)},
            )
        ).one()
    assert row == (
        "running",
        "assemble_package",
        f"country-policy-change:{tenant}:{version}",
    )


async def test_no_registered_handler_marks_dead_no_payload_no_tight_loop(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """无注册 handler：event→dead、last_error 只写固定 NO_REGISTERED_HANDLER + event_type、
    不含 payload；记录结构化错误；不再被轮询（无 tight loop）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tNoH", _opp_qualified("tNoH", "opp-noh")
    )
    deliverer = _make_deliverer(factory, "tNoH")  # 未注册任何 handler
    with caplog.at_level(logging.ERROR):
        await deliverer.drain()
    NO_REGISTERED_HANDLER = cast(str, _load("NO_REGISTERED_HANDLER"))

    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "dead"
    assert err is not None
    assert NO_REGISTERED_HANDLER in err
    assert "OpportunityQualified" in err  # 只带 event_type
    assert "opp-noh" not in err  # 不含 payload
    assert await _count_deliveries(engine_fx, event_id) == 0  # 不产生 delivery 行
    # 结构化错误已发出，且任何日志都不含 payload
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    for record in caplog.records:
        assert "opp-noh" not in record.getMessage()

    # 死信后不再被轮询（不毒化 tight loop）
    await deliverer.drain()
    st2, err2 = await _outbox_status(engine_fx, event_id)
    assert st2 == "dead"
    assert err2 == err


# --- 仅所有 handler delivered 才 event delivered --------------------------------


async def test_event_not_delivered_until_all_handlers_delivered(
    engine_fx: AsyncEngine,
) -> None:
    """仅所有注册 handler 均 delivered 才 event delivered：h1 成功而 h2 死信 → event 不得 delivered。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tGate", _opp_qualified("tGate", "opp-gate")
    )
    deliverer = _make_deliverer(factory, "tGate")
    deliverer.register_handler(OpportunityQualified, "h1", _RecordingHandler())
    deliverer.register_handler(
        OpportunityQualified, "h2", _RecordingHandler(raise_exc=RuntimeError("perm"))
    )
    await deliverer.drain()
    dh1 = await _delivery_row(engine_fx, event_id, "h1")
    dh2 = await _delivery_row(engine_fx, event_id, "h2")
    assert dh1 is not None and dh1["status"] == "delivered"
    assert dh2 is not None and dh2["status"] == "dead"
    st, _ = await _outbox_status(engine_fx, event_id)
    assert st != "delivered"
    # 死信后不再被轮询（无 tight loop）
    await deliverer.drain()
    dh2b = await _delivery_row(engine_fx, event_id, "h2")
    assert dh2b is not None and dh2b["attempts"] == dh2["attempts"]


async def test_event_delivered_after_all_handlers_succeed(engine_fx: AsyncEngine) -> None:
    """两个 handler 均成功 → event delivered，二者都被调用。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tPos", _opp_qualified("tPos", "opp-pos")
    )
    calls: list[str] = []
    deliverer = _make_deliverer(factory, "tPos")
    deliverer.register_handler(OpportunityQualified, "h1", _RecordingHandler(calls))
    deliverer.register_handler(OpportunityQualified, "h2", _RecordingHandler(calls))
    await deliverer.drain()
    dh1 = await _delivery_row(engine_fx, event_id, "h1")
    dh2 = await _delivery_row(engine_fx, event_id, "h2")
    assert dh1 is not None and dh1["status"] == "delivered"
    assert dh2 is not None and dh2["status"] == "delivered"
    st, _ = await _outbox_status(engine_fx, event_id)
    assert st == "delivered"
    assert calls == ["OpportunityQualified", "OpportunityQualified"]


# --- SKIP LOCKED 并发 / 单事件死信不饿死同批 -----------------------------------


async def test_skip_locked_concurrent_drain_no_double_delivery(engine_fx: AsyncEngine) -> None:
    """SKIP LOCKED：两个并发 drain 只投递一次，不重复调用 handler。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tConc", _opp_qualified("tConc", "opp-conc")
    )
    calls: list[str] = []

    def make() -> Any:
        deliverer = _make_deliverer(factory, "tConc")
        deliverer.register_handler(OpportunityQualified, "h", _RecordingHandler(calls))
        return deliverer

    d1, d2 = make(), make()
    await asyncio.gather(d1.drain(), d2.drain())
    assert len(calls) == 1
    st, _ = await _outbox_status(engine_fx, event_id)
    assert st == "delivered"


async def test_one_dead_handler_does_not_block_rest_of_batch(engine_fx: AsyncEngine) -> None:
    """单事件死信不饿死同批其他事件：坏事件→dead，好事件→delivered。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    ev_bad = await _publish_event(engine_fx, "tBatch", _opp_qualified("tBatch", "opp-bad"))
    ev_ok = await _publish_event(engine_fx, "tBatch", _opp_qualified("tBatch", "opp-ok"))
    calls: list[str] = []
    deliverer = _make_deliverer(factory, "tBatch")
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _SelectiveFailHandler(calls, fail_opp_ids={"opp-bad"}),
    )
    await deliverer.drain()
    st_bad, _ = await _outbox_status(engine_fx, ev_bad)
    st_ok, _ = await _outbox_status(engine_fx, ev_ok)
    assert st_bad == "dead"
    assert st_ok == "delivered"
    assert sorted(calls) == ["opp-bad", "opp-ok"]  # 坏事件未饿死好事件


# --- per-handler 持久化 / crash 后幂等续投 ---------------------------------------


async def test_per_handler_durable_resume_after_restart(engine_fx: AsyncEngine) -> None:
    """per-handler durable：h1 已 delivered 在重启后不被重复投递；h2 从 backoff 恢复续投。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tResume", _opp_qualified("tResume", "opp-resume")
    )
    h1_calls: list[str] = []

    # 第一轮：h1 成功、h2 临时失败（TransientError → 记录 backoff）
    d1 = _make_deliverer(factory, "tResume", clock=clock)
    d1.register_handler(OpportunityQualified, "h1", _RecordingHandler(h1_calls))
    d1.register_handler(
        OpportunityQualified, "h2", _RecordingHandler(raise_exc=TransientError("down"))
    )
    await d1.drain()
    dh1 = await _delivery_row(engine_fx, event_id, "h1")
    dh2 = await _delivery_row(engine_fx, event_id, "h2")
    assert dh1 is not None and dh1["status"] == "delivered"  # h1 状态已 durable
    assert dh2 is not None and dh2["attempts"] == 1
    assert dh2["next_attempt_at"] is not None
    st1, _ = await _outbox_status(engine_fx, event_id)
    assert st1 != "delivered"

    # “重启”：新 deliverer（同一 DB，durable 状态续投），h2 恢复可用
    clock.advance(seconds=86400)  # 确保越过 h2 的 backoff
    factory2 = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    d2 = _make_deliverer(factory2, "tResume", clock=clock)
    d2.register_handler(OpportunityQualified, "h1", _RecordingHandler(h1_calls))  # 同一 recorder
    d2.register_handler(OpportunityQualified, "h2", _RecordingHandler())  # 恢复成功
    await d2.drain()

    assert len(h1_calls) == 1  # h1 不重复投递
    dh1b = await _delivery_row(engine_fx, event_id, "h1")
    dh2b = await _delivery_row(engine_fx, event_id, "h2")
    assert dh1b is not None and dh1b["status"] == "delivered"
    assert dh2b is not None and dh2b["status"] == "delivered"
    st2, _ = await _outbox_status(engine_fx, event_id)
    assert st2 == "delivered"


async def test_removed_handler_with_pending_delivery_marks_event_dead(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """handler 在其 delivery 仍 pending 时被移除：事件 dead（固定标记），无 tight loop。

    场景：第一轮 h1 成功、h2 临时失败（h2 delivery pending，事件 pending）；
    “重启”后 h2 不再注册——事件终态判定必须考虑 h2 的 durable delivery 行：该
    pending 行永远无法经注册表完成，事件不得仅凭 h1 已 delivered 掩盖成
    delivered，也不得被反复领取（tight loop）。必须进 ``dead``，``last_error`` 只
    写固定 ``HANDLER_REMOVED_WHILE_PENDING`` + event_type（不含 payload/异常文本），
    结构化日志同样不含 payload。
    """
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tRm", _opp_qualified("tRm", "opp-rm")
    )
    # 第一轮：h1 成功、h2 临时失败 → h2 delivery pending，事件 pending
    d1 = _make_deliverer(factory, "tRm", clock=clock)
    d1.register_handler(OpportunityQualified, "h1", _RecordingHandler())
    d1.register_handler(
        OpportunityQualified, "h2", _RecordingHandler(raise_exc=TransientError("down"))
    )
    await d1.drain()
    st1, _ = await _outbox_status(engine_fx, event_id)
    assert st1 == "pending"
    dh2 = await _delivery_row(engine_fx, event_id, "h2")
    assert dh2 is not None and dh2["status"] == "pending"

    # “重启”：h2 被移除，只注册 h1；越过 h2 的 backoff 使事件可被领取。
    clock.advance(seconds=86400)
    d2 = _make_deliverer(factory, "tRm", clock=clock)
    d2.register_handler(OpportunityQualified, "h1", _RecordingHandler())
    with caplog.at_level(logging.ERROR):
        await d2.drain()

    HANDLER_REMOVED_WHILE_PENDING = cast(str, _load("HANDLER_REMOVED_WHILE_PENDING"))
    st2, err2 = await _outbox_status(engine_fx, event_id)
    assert st2 == "dead"
    assert err2 is not None and HANDLER_REMOVED_WHILE_PENDING in err2
    assert "opp-rm" not in err2  # 不含 payload
    assert "down" not in err2  # 不含异常文本
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    for record in caplog.records:
        assert "opp-rm" not in record.getMessage()  # 结构化日志不含 payload

    # 死信后不再被轮询（不毒化 tight loop）
    await d2.drain()
    st3, _ = await _outbox_status(engine_fx, event_id)
    assert st3 == "dead"


async def test_newly_registered_handler_gets_durable_delivery(
    engine_fx: AsyncEngine,
) -> None:
    """非终态事件上新注册 handler：drain 必须为其创建 durable delivery 行并投递。

    场景：第一轮 h1 临时失败（h1 delivery pending，事件 pending）；“重启”后 h1
    恢复、h2 新注册——事件终态判定必须为 h2 创建 durable delivery（否则 h2 的
    订阅被静默忽略，事件可能仅凭 h1 就 delivered）。
    """
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tAdd", _opp_qualified("tAdd", "opp-add")
    )
    # 第一轮：h1 临时失败 → h1 delivery pending，事件 pending
    d1 = _make_deliverer(factory, "tAdd", clock=clock)
    d1.register_handler(
        OpportunityQualified, "h1", _RecordingHandler(raise_exc=TransientError("down"))
    )
    await d1.drain()
    st1, _ = await _outbox_status(engine_fx, event_id)
    assert st1 == "pending"
    assert await _delivery_row(engine_fx, event_id, "h2") is None  # h2 尚无 delivery

    # “重启”：h1 恢复，h2 新注册；越过 h1 的 backoff 使事件可被领取。
    clock.advance(seconds=86400)
    calls: list[str] = []
    d2 = _make_deliverer(factory, "tAdd", clock=clock)
    d2.register_handler(OpportunityQualified, "h1", _RecordingHandler())
    d2.register_handler(OpportunityQualified, "h2", _RecordingHandler(calls))
    await d2.drain()

    dh2 = await _delivery_row(engine_fx, event_id, "h2")
    assert dh2 is not None and dh2["status"] == "delivered"
    assert calls == ["OpportunityQualified"]  # h2 被调用
    st2, _ = await _outbox_status(engine_fx, event_id)
    assert st2 == "delivered"


# --- TransientError backoff / 重试耗尽死信 --------------------------------------


async def test_transient_error_positive_backoff(engine_fx: AsyncEngine) -> None:
    """TransientError：确定性正退避（next_attempt_at 置将来）；未到点不重试；到点后递增。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tBack", _opp_qualified("tBack", "opp-back")
    )
    deliverer = _make_deliverer(factory, "tBack", clock=clock, max_attempts=10)
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=TransientError("down")),
    )

    t0 = clock.now()
    await deliverer.drain()  # 第一次失败：持久化 backoff
    d1 = await _delivery_row(engine_fx, event_id, "h")
    assert d1 is not None and d1["status"] == "pending"
    assert d1["attempts"] == 1
    assert d1["next_attempt_at"] is not None
    delay1 = cast(datetime, d1["next_attempt_at"]) - t0
    assert delay1 > timedelta(0)  # 正退避
    st, _ = await _outbox_status(engine_fx, event_id)
    assert st == "pending"  # 事件未被 delivered

    # 未到重试时间：再 drain 不重复调用
    await deliverer.drain()
    d2 = await _delivery_row(engine_fx, event_id, "h")
    assert d2 is not None and d2["attempts"] == 1

    # 到点后重试：attempt 递增，退避确定性增长（delay2 > delay1 > 0）
    clock.advance(seconds=86400)
    t2 = clock.now()
    await deliverer.drain()
    d3 = await _delivery_row(engine_fx, event_id, "h")
    assert d3 is not None and d3["attempts"] == 2
    assert d3["next_attempt_at"] is not None
    delay2 = cast(datetime, d3["next_attempt_at"]) - t2
    assert delay2 > delay1 > timedelta(0)


async def test_retry_exhaustion_dead_letter_no_tight_loop(engine_fx: AsyncEngine) -> None:
    """重试耗尽 → 可观测死信态（delivery 与 event 均 dead）；之后不再被轮询（无 tight loop）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tExh", _opp_qualified("tExh", "opp-exh")
    )
    deliverer = _make_deliverer(factory, "tExh", clock=clock, max_attempts=3)
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=TransientError("still down")),
    )

    for _ in range(6):
        await deliverer.drain()
        d = await _delivery_row(engine_fx, event_id, "h")
        if d is not None and d["status"] == "dead":
            break
        clock.advance(seconds=86400)

    d = await _delivery_row(engine_fx, event_id, "h")
    assert d is not None
    assert d["status"] == "dead"
    assert cast(int, d["attempts"]) >= 3  # max_attempts 耗尽
    st, _ = await _outbox_status(engine_fx, event_id)
    assert st == "dead"

    # 死信后不再被轮询（不毒化 tight loop）：attempts 不再变化
    await deliverer.drain()
    d2 = await _delivery_row(engine_fx, event_id, "h")
    assert d2 is not None and d2["attempts"] == d["attempts"]


# --- 永久错误死信 + 错误脱敏 ------------------------------------------------------


async def test_permanent_error_dead_letter_sanitized(engine_fx: AsyncEngine) -> None:
    """永久错误 → 死信；持久化错误脱敏，不含 payload/凭证文本；不毒化 tight loop。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tPerm", _opp_qualified("tPerm", "opp-perm")
    )
    deliverer = _make_deliverer(factory, "tPerm")
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=RuntimeError("downstream auth failed token=sk-abc123")),
    )
    await deliverer.drain()
    d = await _delivery_row(engine_fx, event_id, "h")
    assert d is not None and d["status"] == "dead"
    last_error = cast(str, d["last_error"] or "")
    assert "sk-abc123" not in last_error  # 凭证文本脱敏
    assert "downstream auth failed" not in last_error  # 原始异常消息不落库
    assert "opp-perm" not in last_error  # payload 不落库
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "dead"
    assert err is None or "opp-perm" not in err

    # 死信后不再被轮询（无 tight loop）
    await deliverer.drain()
    d2 = await _delivery_row(engine_fx, event_id, "h")
    assert d2 is not None and d2["attempts"] == d["attempts"]
    assert d2["last_error"] == d["last_error"]


# --- 未知事件类型 / 租户隔离 -----------------------------------------------------


async def test_unknown_event_type_preserves_registry_failure(engine_fx: AsyncEngine) -> None:
    """未知事件类型保留 registry 失败语义（ValidationError），不静默吞掉也不标记 delivered；
    同一批次中的合法事件不受饿死（要求 6 + 9 调和：一个坏事件不 starve 同批其余）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    calls: list[str] = []
    valid_id = await _publish_event(
        engine_fx, "tUnk", _opp_qualified("tUnk", "opp-unk-ok")
    )
    unknown_id = "evt_zzz-unknown"
    # 未知事件的时间戳放到合法事件之后（published_at/occurred_at 均晚、event_id 排序靠后），
    # 使任何按时间序/主键序的实现都会先处理合法事件——断言不依赖内部排序。
    later = _NOW + timedelta(hours=1)
    async with engine_fx.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_events (event_id, tenant_id, event_type, event_payload, "
                "attempt, published_at, trace_id, run_id, occurred_at, status) "
                "VALUES (:e, :t, :ty, CAST(:p AS jsonb), 1, :later, 'trc-x', NULL, :later, 'pending')"
            ),
            {
                "e": unknown_id,
                "t": "tUnk",
                "ty": "TotallyUnknown",
                "p": json.dumps({"x": 1}),
                "later": later,
            },
        )
    deliverer = _make_deliverer(factory, "tUnk")
    deliverer.register_handler(OpportunityQualified, "h", _RecordingHandler(calls))
    with pytest.raises(ValidationError):
        await deliverer.drain()
    # 未知事件：保留 registry 失败语义，不静默标记 delivered
    st_unknown, _ = await _outbox_status(engine_fx, unknown_id)
    assert st_unknown != "delivered"
    # 同批合法事件：已 delivered 且 handler 被调用（坏事件未饿死同批）
    st_valid, _ = await _outbox_status(engine_fx, valid_id)
    assert st_valid == "delivered"
    assert calls == ["OpportunityQualified"]


async def test_tenant_isolation_fail_closed(engine_fx: AsyncEngine) -> None:
    """租户隔离失败关闭：A 的 drain 只处理 A 的事件，B 不受影响。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    ev_a = await _publish_event(engine_fx, "tIsoA", _opp_qualified("tIsoA", "opp-iso-a"))
    ev_b = await _publish_event(engine_fx, "tIsoB", _opp_qualified("tIsoB", "opp-iso-b"))
    calls_a: list[str] = []
    calls_b: list[str] = []
    da = _make_deliverer(factory, "tIsoA")
    da.register_handler(OpportunityQualified, "h", _RecordingHandler(calls_a))
    db_ = _make_deliverer(factory, "tIsoB")
    db_.register_handler(OpportunityQualified, "h", _RecordingHandler(calls_b))

    await da.drain()
    st_a, _ = await _outbox_status(engine_fx, ev_a)
    st_b, _ = await _outbox_status(engine_fx, ev_b)
    assert st_a == "delivered"
    assert st_b == "pending"  # B 未被动到
    assert len(calls_a) == 1
    assert len(calls_b) == 0

    await db_.drain()
    st_b2, _ = await _outbox_status(engine_fx, ev_b)
    assert st_b2 == "delivered"
    assert len(calls_b) == 1


# --- 已知类型 payload 反序列化失败 → 死信（脱敏，与未知类型区分） -----------------


async def test_known_event_deserialization_failure_marks_dead_sanitized(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """已知事件类型（EVENT_REGISTRY 白名单内）且已有注册 handler，但其 payload 无法
    反序列化 → 事件 dead（固定 ``PAYLOAD_DESERIALIZATION_FAILED`` + event_type、
    ``next_attempt_at`` 置 NULL），结构化日志不含 payload/异常文本；同批好事件不受
    饿死（handler 只被好事件调用）；死信后不再被轮询（无 tight loop）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    good_id = await _publish_event(
        engine_fx, "tBadP", _opp_qualified("tBadP", "opp-badp-good")
    )
    bad_id = "evt_badp-bad"
    secret = "opp-badp-secret-leak"
    async with engine_fx.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_events (event_id, tenant_id, event_type, event_payload, "
                "attempt, published_at, trace_id, run_id, occurred_at, status) "
                "VALUES (:e, :t, :ty, CAST(:p AS jsonb), 1, now(), 'trc-bp', NULL, now(), 'pending')"
            ),
            {
                "e": bad_id,
                "t": "tBadP",
                "ty": "OpportunityQualified",
                "p": json.dumps({"secret": secret}),  # 缺必填字段 → 反序列化失败
            },
        )
    calls: list[str] = []
    deliverer = _make_deliverer(factory, "tBadP")
    deliverer.register_handler(OpportunityQualified, "h", _RecordingHandler(calls))
    with caplog.at_level(logging.ERROR):
        await deliverer.drain()  # 已知类型 payload 有损 → 不重新抛出
    PAYLOAD_DESERIALIZATION_FAILED = cast(str, _load("PAYLOAD_DESERIALIZATION_FAILED"))

    st_bad, err_bad = await _outbox_status(engine_fx, bad_id)
    assert st_bad == "dead"
    assert err_bad is not None and PAYLOAD_DESERIALIZATION_FAILED in err_bad
    assert "OpportunityQualified" in err_bad  # 只带 event_type
    assert secret not in err_bad  # 不含 payload
    assert await _event_next_attempt_at(engine_fx, bad_id) is None
    assert await _count_deliveries(engine_fx, bad_id) == 0  # 反序列化前不产生 delivery 行

    # 同批好事件照常投递（坏事件不饿死同批），坏事件 payload 不达 handler
    st_good, _ = await _outbox_status(engine_fx, good_id)
    assert st_good == "delivered"
    assert calls == ["OpportunityQualified"]

    # 结构化日志：固定消息、任何日志不含 payload/异常文本（log leak 断言）
    dead_records = [
        r
        for r in caplog.records
        if r.getMessage() == "outbox event dead: payload deserialization failed"
    ]
    assert len(dead_records) == 1
    assert dead_records[0].__dict__.get("event_id") == bad_id
    assert dead_records[0].__dict__.get("tenant_id") == "tBadP"
    assert dead_records[0].__dict__.get("event_type") == "OpportunityQualified"
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "badp-good" not in record.getMessage()

    # 死信后不再被轮询（不毒化 tight loop）
    await deliverer.drain()
    st2, err2 = await _outbox_status(engine_fx, bad_id)
    assert st2 == "dead"
    assert err2 == err_bad


# --- DB 级异常迭代内隔离：回滚 + 固定脱敏日志 + 继续同批 + 下次可重试 -------------


async def test_db_commit_failure_rolls_back_logs_and_continues(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """drain 循环内任何 DB 级 commit 异常：回滚该事件事务（保持 pending 可重试）、
    发出固定结构化脱敏日志（仅 event_id/tenant_id/event_type，不含 payload/异常
    文本）、继续处理同批其余事件（一个坏事务不中止整批）；下次 drain（正常会话）
    可全部投递成功。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    ev_a = await _publish_event(engine_fx, "tDbErr", _opp_qualified("tDbErr", "opp-db-a"))
    ev_b = await _publish_event(engine_fx, "tDbErr", _opp_qualified("tDbErr", "opp-db-b"))
    calls: list[str] = []
    poison_factory = async_sessionmaker(
        bind=engine_fx, expire_on_commit=False, class_=_FailingCommitSession
    )
    deliverer = _make_deliverer(poison_factory, "tDbErr")
    deliverer.register_handler(OpportunityQualified, "h", _RecordingHandler(calls))
    with caplog.at_level(logging.ERROR):
        await deliverer.drain()  # 两个事件都尝试、都回滚，不抛出

    # 同批两个事件都被尝试（坏事务不中止整批）；全部回滚 → 仍 pending，可重试
    assert len(calls) == 2
    st_a, _ = await _outbox_status(engine_fx, ev_a)
    st_b, _ = await _outbox_status(engine_fx, ev_b)
    assert st_a == "pending"
    assert st_b == "pending"

    # 固定结构化脱敏日志：每个失败事件一条、消息固定、仅标识键、无 payload/异常文本
    db_fail_records = [
        r
        for r in caplog.records
        if r.getMessage() == "outbox event skipped: db failure"
    ]
    assert len(db_fail_records) == 2
    for record in db_fail_records:
        d = record.__dict__
        assert d.get("event_id") in {ev_a, ev_b}
        assert d.get("tenant_id") == "tDbErr"
        assert d.get("event_type") == "OpportunityQualified"
        # 除三个标识键外的任何字段值都不含 payload/异常文本（log leak 断言）
        for key, value in d.items():
            if key in ("event_id", "tenant_id", "event_type"):
                continue
            if isinstance(value, str):
                assert "opp-db-a" not in value
                assert "opp-db-b" not in value
                assert "exploded" not in value

    # 失败事件可重试：正常会话再次 drain → 全部 delivered
    d2 = _make_deliverer(factory, "tDbErr")
    d2.register_handler(OpportunityQualified, "h", _RecordingHandler())
    await d2.drain()
    st_a2, _ = await _outbox_status(engine_fx, ev_a)
    st_b2, _ = await _outbox_status(engine_fx, ev_b)
    assert st_a2 == "delivered"
    assert st_b2 == "delivered"


async def test_db_select_failure_before_selection_terminates_cycle(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """drain 在选中任何事件前就遭遇 DB 级故障（首个 SELECT 失败）：fail closed。

    回归：修复前 ``event_id`` 为空、无 event 记入 ``tried_event_ids``，循环无法
    取得进展 → 无限重试同一故障（tight loop）。修复后：回滚、发出固定结构化脱敏
    日志一次、``finally`` 关闭会话后终止当前 drain cycle——drain 及时返回 0（零
    处理），事件保持 pending（未动过），日志不含 payload/异常文本/哨兵标记；随后用
    正常会话再次 drain 可全部投递成功（故障可恢复，不毒化后续投递）。
    """
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tSelErr", _opp_qualified("tSelErr", "opp-selerr")
    )
    secret = "INTERNAL_DB_SELECT_LEAK_MARKER"
    poison_factory = async_sessionmaker(
        bind=engine_fx,
        expire_on_commit=False,
        class_=_FailingSelectSession,
    )
    calls: list[str] = []
    deliverer = _make_deliverer(poison_factory, "tSelErr")
    deliverer.register_handler(OpportunityQualified, "h", _RecordingHandler(calls))
    with caplog.at_level(logging.ERROR):
        # wait_for：修复前该 drain 会无限循环（悬死），timeout 使「及时返回」被
        # 明确断言而不是让测试套件永久挂起。
        processed = await asyncio.wait_for(deliverer.drain(), timeout=5)

    # 零处理、handler 未被调用
    assert processed == 0
    assert calls == []
    # 事件未被触碰：保持 pending（可重试，无 last_error），无 delivery 行
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "pending"
    assert err is None
    assert await _count_deliveries(engine_fx, event_id) == 0

    # 固定结构化脱敏日志恰好一次；未选中事件 → 标识键为空；不含 payload/异常/哨兵标记
    db_fail_records = [
        r for r in caplog.records if r.getMessage() == "outbox event skipped: db failure"
    ]
    assert len(db_fail_records) == 1
    d = db_fail_records[0].__dict__
    assert d.get("event_id") == ""
    assert d.get("tenant_id") == ""
    assert d.get("event_type") == ""
    for record in caplog.records:
        message = record.getMessage()
        assert "opp-selerr" not in message  # 不含 payload
        assert "exploded" not in message  # 不含异常文本
        assert secret not in message  # 不含哨兵标记
    # 除三个标识键外的任何字段值都不含 payload/异常/哨兵标记（log leak 断言）
    for key, value in d.items():
        if key in ("event_id", "tenant_id", "event_type"):
            continue
        if isinstance(value, str):
            assert "opp-selerr" not in value
            assert "exploded" not in value
            assert secret not in value

    # 故障可恢复：正常会话再次 drain → 事件 delivered
    d2 = _make_deliverer(factory, "tSelErr")
    d2.register_handler(OpportunityQualified, "h", _RecordingHandler())
    await d2.drain()
    st2, _ = await _outbox_status(engine_fx, event_id)
    assert st2 == "delivered"


# --- 未知事件类型：保持 pending + 日志脱敏（registry 失败语义回归） -------------


async def test_unknown_event_type_stays_pending_log_sanitized(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未知事件类型保留 registry 失败语义：事件保持 pending（不标记 delivered/dead，
    ``next_attempt_at`` 保持 NULL），同批合法事件照常投递，drain 处理完本批后重新
    抛出原 ``ValidationError``；结构化错误日志不含 payload/异常文本（log leak 断言）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    secret = "opp-unk-secret-leak"
    good_id = await _publish_event(
        engine_fx, "tUnk2", _opp_qualified("tUnk2", "opp-unk2-ok")
    )
    unknown_id = "evt_unk2"
    later = _NOW + timedelta(hours=2)
    async with engine_fx.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO outbox_events (event_id, tenant_id, event_type, event_payload, "
                "attempt, published_at, trace_id, run_id, occurred_at, status) "
                "VALUES (:e, :t, :ty, CAST(:p AS jsonb), 1, :later, 'trc-u2', NULL, :later, 'pending')"
            ),
            {
                "e": unknown_id,
                "t": "tUnk2",
                "ty": "TotallyUnknown",
                "p": json.dumps({"secret": secret}),
                "later": later,
            },
        )
    calls: list[str] = []
    deliverer = _make_deliverer(factory, "tUnk2")
    deliverer.register_handler(OpportunityQualified, "h", _RecordingHandler(calls))
    with caplog.at_level(logging.ERROR), pytest.raises(ValidationError):
        await deliverer.drain()

    # 未知事件：保持 pending、无 last_error、next_attempt_at 保持 NULL
    st_unknown, err_unknown = await _outbox_status(engine_fx, unknown_id)
    assert st_unknown == "pending"
    assert err_unknown is None
    assert await _event_next_attempt_at(engine_fx, unknown_id) is None
    # 同批合法事件照常投递（坏事件不饿死同批）
    st_good, _ = await _outbox_status(engine_fx, good_id)
    assert st_good == "delivered"
    assert calls == ["OpportunityQualified"]

    # 结构化错误日志：固定消息、仅标识键、不含 payload/异常文本
    unknown_records = [
        r
        for r in caplog.records
        if r.getMessage() == "outbox event skipped: unknown event type"
    ]
    assert len(unknown_records) == 1
    d = unknown_records[0].__dict__
    assert d.get("event_id") == unknown_id
    assert d.get("tenant_id") == "tUnk2"
    assert d.get("event_type") == "TotallyUnknown"
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert "opp-unk2-ok" not in record.getMessage()


# --- S3-8 final cleanup：事件行同步/清空 + 结构化脱敏日志 -----------------------


async def test_transient_retry_syncs_event_next_attempt_and_sanitized_error(
    engine_fx: AsyncEngine,
) -> None:
    """TransientError 未耗尽：事件行 next_attempt_at/last_error 与 pending delivery
    聚合同步；last_error 脱敏（不含异常消息/payload）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tSync", _opp_qualified("tSync", "opp-sync")
    )
    deliverer = _make_deliverer(factory, "tSync", clock=clock)
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=TransientError("secret backend down")),
    )
    await deliverer.drain()
    d = await _delivery_row(engine_fx, event_id, "h")
    assert d is not None and d["status"] == "pending"
    assert d["next_attempt_at"] is not None
    # 事件行同步 pending delivery 聚合：退避时间一致、last_error 一致
    assert await _event_next_attempt_at(engine_fx, event_id) == d["next_attempt_at"]
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "pending"
    assert err is not None
    assert "secret backend down" not in err  # 不含异常消息
    assert "opp-sync" not in err  # 不含 payload
    dl_err = cast(str, d["last_error"] or "")
    assert err == dl_err  # 事件 last_error 与 delivery 同步（脱敏类型名）


async def test_delivery_success_clears_retry_fields_and_event_delivered_cleared(
    engine_fx: AsyncEngine,
) -> None:
    """投递成功：清空该 delivery 的 next_attempt_at/last_error；事件全部 delivered
    时清空事件行的 next_attempt_at/last_error（不残留旧 backoff/错误标记）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tClear", _opp_qualified("tClear", "opp-clear")
    )
    # 第一轮：h 临时失败 → 事件与 delivery 都记录退避与脱敏 last_error
    d1 = _make_deliverer(factory, "tClear", clock=clock)
    d1.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=TransientError("down")),
    )
    await d1.drain()
    d0 = await _delivery_row(engine_fx, event_id, "h")
    assert d0 is not None and d0["next_attempt_at"] is not None
    assert d0["last_error"] is not None
    st0, err0 = await _outbox_status(engine_fx, event_id)
    assert st0 == "pending"
    assert err0 is not None

    # 第二轮：到点后成功 → delivery 与事件均清空 next_attempt_at/last_error
    clock.advance(seconds=86400)
    d2 = _make_deliverer(factory, "tClear", clock=clock)
    d2.register_handler(OpportunityQualified, "h", _RecordingHandler())
    await d2.drain()
    dh = await _delivery_row(engine_fx, event_id, "h")
    assert dh is not None and dh["status"] == "delivered"
    assert dh["next_attempt_at"] is None
    assert dh["last_error"] is None
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "delivered"
    assert err is None
    assert await _event_next_attempt_at(engine_fx, event_id) is None


async def test_dead_outcome_clears_event_next_attempt_and_sets_sanitized_error(
    engine_fx: AsyncEngine,
) -> None:
    """永久错误死信：事件 next_attempt_at 置 NULL、last_error 写脱敏值（异常类型名，
    不含 payload/原始异常消息），与 dead delivery 的脱敏 last_error 同步。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tDeadE", _opp_qualified("tDeadE", "opp-deade")
    )
    deliverer = _make_deliverer(factory, "tDeadE")
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=RuntimeError("boom token=sk-leak")),
    )
    await deliverer.drain()
    d = await _delivery_row(engine_fx, event_id, "h")
    assert d is not None and d["status"] == "dead"
    assert d["next_attempt_at"] is None  # dead delivery 无重试调度
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "dead"
    assert await _event_next_attempt_at(engine_fx, event_id) is None
    assert err is not None
    assert "boom" not in err  # 不含异常消息
    assert "sk-leak" not in err  # 不含凭证文本
    assert "opp-deade" not in err  # 不含 payload
    dl_err = cast(str, d["last_error"] or "")
    assert err == dl_err  # 事件 last_error 与 dead delivery 同步（脱敏类型名）


async def test_permanent_handler_failure_log_sanitized(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """永久 handler 失败：结构化脱敏日志（固定消息 + 标识键，不含 payload/原始异常文本）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    event_id = await _publish_event(
        engine_fx, "tPermLog", _opp_qualified("tPermLog", "opp-permlog")
    )
    deliverer = _make_deliverer(factory, "tPermLog")
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=RuntimeError("boom sk-leak")),
    )
    with caplog.at_level(logging.ERROR):
        await deliverer.drain()
    records = [
        r
        for r in caplog.records
        if r.getMessage() == "outbox event dead: permanent handler failure"
    ]
    assert len(records) == 1
    d = records[0].__dict__
    assert d.get("event_id") == event_id
    assert d.get("tenant_id") == "tPermLog"
    assert d.get("event_type") == "OpportunityQualified"
    assert d.get("handler_name") == "h"
    # 日志安全：任何日志都不含异常消息/凭证文本/payload
    for record in caplog.records:
        assert "boom" not in record.getMessage()
        assert "sk-leak" not in record.getMessage()
        assert "opp-permlog" not in record.getMessage()


async def test_retry_exhaustion_log_sanitized_and_event_cleared(
    engine_fx: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重试耗尽：结构化脱敏日志（固定消息 + 标识键）；事件 dead、next_attempt_at NULL、
    last_error 固定脱敏（不含 payload/异常文本）。"""
    factory = async_sessionmaker(bind=engine_fx, expire_on_commit=False)
    clock = _Clock()
    event_id = await _publish_event(
        engine_fx, "tExhLog", _opp_qualified("tExhLog", "opp-exhlog")
    )
    deliverer = _make_deliverer(factory, "tExhLog", clock=clock, max_attempts=3)
    deliverer.register_handler(
        OpportunityQualified,
        "h",
        _RecordingHandler(raise_exc=TransientError("secret still down")),
    )
    with caplog.at_level(logging.ERROR):
        for _ in range(6):
            await deliverer.drain()
            d = await _delivery_row(engine_fx, event_id, "h")
            if d is not None and d["status"] == "dead":
                break
            clock.advance(seconds=86400)
    records = [
        r
        for r in caplog.records
        if r.getMessage() == "outbox event dead: retry exhausted"
    ]
    assert len(records) == 1
    log = records[0].__dict__
    assert log.get("event_id") == event_id
    assert log.get("tenant_id") == "tExhLog"
    assert log.get("event_type") == "OpportunityQualified"
    assert log.get("handler_name") == "h"
    # 日志安全：任何日志都不含异常消息/payload
    for record in caplog.records:
        assert "secret still down" not in record.getMessage()
        assert "opp-exhlog" not in record.getMessage()
    # 事件行：dead、next_attempt_at NULL、last_error 固定脱敏
    st, err = await _outbox_status(engine_fx, event_id)
    assert st == "dead"
    assert await _event_next_attempt_at(engine_fx, event_id) is None
    assert err is not None
    assert "secret still down" not in err
    assert "opp-exhlog" not in err
