"""S3-7 workflow 引擎集成测试（0004 迁移 + PostgresWorkflowEngine）。

行为断言，不依赖实现细节：
- 0004 两表存在且各含 tenant_id NOT NULL；列集合与 Schema 附录一致；两个调度索引存在。
- 0003→0004→0003 round-trip，finally 恢复 head。
- workflow_runs UNIQUE(tenant_id, idempotency_key)：同租户同 key 被拒、异租户同 key 允许。
- workflow_steps 复合 FK (tenant_id, run_id)→workflow_runs ON DELETE CASCADE：
  跨租户引用被拒、同租户合法、删除 run 级联删步骤。
- workflow_steps UNIQUE(tenant_id, idempotency_key)。
- register/start 幂等：同租户同 key 返回既有 run；异租户互不影响；空白 key 拒绝。
- poll_due：FOR UPDATE SKIP LOCKED 并发不重复领取；每步独立事务（一步永久失败
  不阻塞本批其他 run）；TransientError 指数退避与耗尽 FAILED；永久错误 last_error
  脱敏（不含异常消息/payload）；非法 transition 永久失败。
- deliver_event：仅同租户、WAITING_EVENT、事件类型匹配时推进；同事件幂等 no-op；
  事件指纹 durable 去重；错误租户/未知 run no-op。
- cancel：只取消同租户目标；重复 cancel 幂等；已完成/失败终态不被复活。

RED 前置：0004 迁移/PostgresWorkflowEngine 未建 → 表缺失断言失败 + 引擎符号缺失
转行为失败（非收集错误）。全部用本地引擎（create_engine_from + try/finally dispose），
不触发 session 级 async fixture 的 teardown 问题。禁止打印/记录任何连接串。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import TextClause, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import RunId, TenantId
from workflows.account_discovery.flow import (
    build_legacy_account_discovery_definition,
    register_account_discovery,
)
from workflows.engine.runner import (
    StepDefinition,
    StepStatus,
    WorkflowDefinition,
    WorkflowRun,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 8, 9, 8, 0, 0, tzinfo=UTC)

# 两表（Schema 附录 0004）。
EXPECTED_TABLES: tuple[str, ...] = ("workflow_runs", "workflow_steps")

RUN_COLUMNS = {
    "run_id", "tenant_id", "workflow_type", "workflow_version", "subject_ref",
    "current_step", "status", "created_at", "next_poll_at", "retry_count",
    "context", "last_error", "idempotency_key",
}
STEP_COLUMNS = {
    "step_id", "run_id", "tenant_id", "step_name", "status", "data", "attempt",
    "error", "due_at", "idempotency_key", "created_at", "updated_at",
}

# 0004 明确要求的调度索引。
EXPECTED_RUN_INDEXES = {"ix_workflow_runs_tenant_status_poll"}
EXPECTED_STEP_INDEXES = {"ix_workflow_steps_tenant_status_due"}


class _Clock:
    """可推进的假时钟：让调度时间确定可测。"""

    def __init__(self, start: datetime = _NOW) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, **kw: float) -> None:
        self._t = self._t + timedelta(**kw)


def _load_engine_cls():
    """按模块字符串导入引擎类；缺失转行为失败（RED 阶段未建）。"""
    try:
        module = importlib.import_module("infra.db.workflow_engine")
        return module.PostgresWorkflowEngine
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：PostgresWorkflowEngine 尚未创建（{exc}）")


def _handler(fn):
    """把同步 callable 包成 StepHandler（async execute 语义）。"""

    class _Handler:
        async def execute(
            self, run: WorkflowRun
        ) -> tuple[str, str | None, dict[str, object]]:
            return fn(run)

    return _Handler()


def _make_engine(db_url: str, handlers: dict, clock: _Clock | None = None):
    """构建引擎 + 底层 AsyncEngine（调用方负责 dispose）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    now_fn = clock.now if clock is not None else None
    return _load_engine_cls()(factory, handlers, now=now_fn), engine


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
    return inspect(conn).get_table_names()


def _sync_columns(conn: Connection, table: str) -> list[dict[str, object]]:
    return [dict(c) for c in inspect(conn).get_columns(table)]


def _sync_indexes(conn: Connection, table: str) -> set[str]:
    return {str(ix["name"]) for ix in inspect(conn).get_indexes(table)}


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(_sync_table_names))


async def _columns(engine: AsyncEngine, table: str) -> dict[str, bool]:
    """列名 → 是否 nullable。"""
    async with engine.connect() as conn:
        cols = await conn.run_sync(_sync_columns, table)
    return {str(c["name"]): bool(c["nullable"]) for c in cols}


async def _indexes(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(_sync_indexes, table)


async def _query_run(
    engine: AsyncEngine, tenant: str, run_id: str
) -> dict[str, Any] | None:
    """当前 run 行状态（用于行为断言，不依赖 ORM 行类）。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT run_id, tenant_id, workflow_type, workflow_version, subject_ref, "
                "current_step, status, retry_count, next_poll_at, context, last_error "
                "FROM workflow_runs WHERE tenant_id = :t AND run_id = :r"
            ),
            {"t": tenant, "r": run_id},
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None


async def _query_steps(
    engine: AsyncEngine, tenant: str, run_id: str
) -> list[dict[str, Any]]:
    """当前 run 的全部步骤（按 created_at/step_id 排序，行为断言用）。"""
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT step_id, run_id, tenant_id, step_name, status, data, attempt, "
                "error, due_at, idempotency_key "
                "FROM workflow_steps WHERE tenant_id = :t AND run_id = :r "
                "ORDER BY created_at, step_id"
            ),
            {"t": tenant, "r": run_id},
        )
        return [dict(row) for row in result.mappings().all()]


_INSERT_RUN = text(
    "INSERT INTO workflow_runs ("
    "run_id, tenant_id, workflow_type, workflow_version, subject_ref, current_step, "
    "status, next_poll_at, retry_count, context, last_error, idempotency_key) "
    "VALUES (:run_id, :tenant_id, :workflow_type, :workflow_version, :subject_ref, "
    ":current_step, :status, :next_poll_at, :retry_count, CAST(:context AS jsonb), "
    ":last_error, :idempotency_key)"
)


def _run_params(
    run_id: str,
    tenant: str,
    *,
    key: str = "k1",
    current_step: str = "first",
    status: str = "running",
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    """workflow_runs 最小合法行参数（created_at 走 now()）。"""
    return {
        "run_id": run_id,
        "tenant_id": tenant,
        "workflow_type": "wf",
        "workflow_version": 1,
        "subject_ref": "subj-1",
        "current_step": current_step,
        "status": status,
        "next_poll_at": None,
        "retry_count": 0,
        "context": json.dumps(context or {}),
        "last_error": None,
        "idempotency_key": key,
    }


_INSERT_STEP = text(
    "INSERT INTO workflow_steps ("
    "step_id, run_id, tenant_id, step_name, status, data, attempt, error, due_at, "
    "idempotency_key) "
    "VALUES (:step_id, :run_id, :tenant_id, :step_name, :status, CAST(:data AS jsonb), "
    ":attempt, :error, now(), :idempotency_key)"
)


def _step_params(
    step_id: str,
    run_id: str,
    tenant: str,
    *,
    step_name: str = "first",
    key: str = "sk1",
    status: str = "pending",
) -> dict[str, object]:
    """workflow_steps 最小合法行参数（due_at/created_at/updated_at 走 now()）。"""
    return {
        "step_id": step_id,
        "run_id": run_id,
        "tenant_id": tenant,
        "step_name": step_name,
        "status": status,
        "data": json.dumps({}),
        "attempt": 0,
        "error": None,
        "idempotency_key": key,
    }


async def _assert_statement_integrity_rejected(
    engine: AsyncEngine,
    statement: TextClause,
    params: dict[str, object],
    label: str,
) -> None:
    """INSERT 应被唯一/FK/NOT NULL 约束拒绝（IntegrityError）；独立事务回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(statement, params)
            await conn.rollback()
            pytest.fail(label)
        except IntegrityError:
            await conn.rollback()


# --- 迁移：表结构 / 列集合 / 索引 / round-trip / 约束 / 复合 FK -----------------------


async def test_workflow_tables_exist_with_tenant_id_not_null(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        missing = set(EXPECTED_TABLES) - names
        assert not missing, f"缺失表：{sorted(missing)}"
        for table in EXPECTED_TABLES:
            cols = await _columns(engine, table)
            assert "tenant_id" in cols, f"{table} 缺 tenant_id（硬边界 8）"
            assert cols["tenant_id"] is False, f"{table}.tenant_id 应 NOT NULL"
    finally:
        await engine.dispose()


async def test_workflow_table_column_sets_match_appendix(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        missing = set(EXPECTED_TABLES) - names
        assert not missing, f"缺失表：{sorted(missing)}"
        run_cols = await _columns(engine, "workflow_runs")
        step_cols = await _columns(engine, "workflow_steps")
        assert set(run_cols) == RUN_COLUMNS, f"workflow_runs 列集合不符：{sorted(set(run_cols))}"
        assert set(step_cols) == STEP_COLUMNS, f"workflow_steps 列集合不符：{sorted(set(step_cols))}"
    finally:
        await engine.dispose()


async def test_workflow_scheduling_indexes_present(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        run_ix = await _indexes(engine, "workflow_runs")
        step_ix = await _indexes(engine, "workflow_steps")
        assert EXPECTED_RUN_INDEXES <= run_ix, f"workflow_runs 缺索引：{EXPECTED_RUN_INDEXES - run_ix}"
        assert EXPECTED_STEP_INDEXES <= step_ix, f"workflow_steps 缺索引：{EXPECTED_STEP_INDEXES - step_ix}"
    finally:
        await engine.dispose()


async def test_roundtrip_downgrade_0003_then_upgrade_head(db_url: str) -> None:
    """迁移 round-trip：head 两表在 → downgrade 0003 消失 → upgrade head 恢复。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        assert set(EXPECTED_TABLES) <= names, "roundtrip 前置：head 应含两表"
        _run_alembic(db_url, "downgrade", "0003")
        names = await _table_names(engine)
        assert not (set(EXPECTED_TABLES) & names), "downgrade 0003 后两表应消失"
        _run_alembic(db_url, "upgrade", "head")
        names = await _table_names(engine)
        assert set(EXPECTED_TABLES) <= names, "upgrade head 后两表应恢复"
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_run_idempotency_key_unique_per_tenant(db_url: str) -> None:
    """(tenant_id, idempotency_key) 唯一：异租户同 key 允许、同租户重复被拒（并发幂等兜底）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(_INSERT_RUN, _run_params("run-uq-a", "tA", key="key-1"))
            await conn.execute(_INSERT_RUN, _run_params("run-uq-b", "tB", key="key-1"))
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_RUN,
            _run_params("run-uq-a2", "tA", key="key-1"),
            "同租户重复 idempotency_key 应被 UNIQUE(tenant_id, idempotency_key) 拒绝",
        )
    finally:
        await engine.dispose()


async def test_step_idempotency_unique_per_tenant(db_url: str) -> None:
    """(tenant_id, idempotency_key) 唯一：异租户同 step key 允许、同租户重复被拒。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(_INSERT_RUN, _run_params("run-stp-a", "tA", key="rk-a"))
            await conn.execute(_INSERT_RUN, _run_params("run-stp-b", "tB", key="rk-b"))
            await conn.execute(_INSERT_STEP, _step_params("stp-a1", "run-stp-a", "tA", key="sk-a"))
            await conn.execute(_INSERT_STEP, _step_params("stp-b1", "run-stp-b", "tB", key="sk-a"))
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_STEP,
            _step_params("stp-a2", "run-stp-a", "tA", key="sk-a"),
            "同租户重复 step idempotency_key 应被 UNIQUE(tenant_id, idempotency_key) 拒绝",
        )
    finally:
        await engine.dispose()


async def test_step_composite_fk_tenant_isolation_and_cascade(db_url: str) -> None:
    """复合 FK (tenant_id, run_id)：跨租户步骤引用被拒、同租户合法、DELETE run 级联删步骤。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(_INSERT_RUN, _run_params("run-fk-a", "tFK_a", key="fk-a"))
            await conn.execute(_INSERT_STEP, _step_params("stp-fk-a", "run-fk-a", "tFK_a", key="fk-sa"))
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_STEP,
            _step_params("stp-fk-b", "run-fk-a", "tFK_b", key="fk-sb"),
            "跨租户步骤引用应被复合 FK (tenant_id, run_id) 拒绝",
        )
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM workflow_runs WHERE run_id = :r AND tenant_id = :t"),
                {"r": "run-fk-a", "t": "tFK_a"},
            )
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM workflow_steps "
                        "WHERE tenant_id = :t AND run_id = :r"
                    ),
                    {"t": "tFK_a", "r": "run-fk-a"},
                )
            ).scalar_one()
        assert count == 0, "删除 run 应级联删除其步骤（ON DELETE CASCADE）"
    finally:
        await engine.dispose()


# --- register / start 幂等与租户隔离 ------------------------------------------------


def _simple_def(workflow_type: str = "wf") -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type=workflow_type,
        version=1,
        steps=(StepDefinition(step_name="first", handler_ref="h"),),
        transitions={},
    )


async def test_register_duplicate_idempotent_and_conflict(db_url: str) -> None:
    """register：重复注册同一定义幂等 no-op；同 (type, version) 不同定义抛错；
    未注册 handler_ref / 空 steps / 未注册流程类型 start 均 fail closed。"""
    calls: list[str] = []

    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        calls.append(run.run_id)
        return ("complete", None, {})

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        definition = _simple_def()
        engine.register(definition)
        engine.register(definition)  # 幂等 no-op

        conflict = WorkflowDefinition(
            workflow_type="wf",
            version=1,
            steps=(
                StepDefinition(step_name="first", handler_ref="h"),
                StepDefinition(step_name="second", handler_ref="h"),
            ),
            transitions={},
        )
        with pytest.raises(ValueError):
            engine.register(conflict)

        bad_handler = WorkflowDefinition(
            workflow_type="wf2",
            version=1,
            steps=(StepDefinition(step_name="first", handler_ref="missing"),),
            transitions={},
        )
        with pytest.raises(ValueError):
            engine.register(bad_handler)

        empty = WorkflowDefinition(workflow_type="wf3", version=1, steps=(), transitions={})
        with pytest.raises(ValueError):
            engine.register(empty)

        with pytest.raises(ValidationError):
            await engine.start(TenantId("tReg"), "not_registered", "s1", {}, "reg-key")
    finally:
        await handle.dispose()


async def test_start_idempotent_same_tenant_and_isolated_across_tenants(db_url: str) -> None:
    """start 幂等：同 (tenant, key) 返回既有 run_id 且 context 不被覆盖；异租户同 key 独立；
    空白 key 拒绝。"""
    calls: list[str] = []

    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        calls.append(run.run_id)
        return ("complete", None, {})

    clock = _Clock()
    engine, handle = _make_engine(db_url, {"h": _handler(_h)}, clock=clock)
    try:
        engine.register(_simple_def())
        tenant_a = "tStartA"
        tenant_b = "tStartB"

        rid1 = await engine.start(TenantId(tenant_a), "wf", "subj-a", {"k": "v"}, "idem-1")
        rid2 = await engine.start(
            TenantId(tenant_a), "wf", "subj-a", {"k": "overwrite"}, "idem-1"
        )
        assert rid2 == rid1, "同 (tenant, idempotency_key) 应返回既有 run_id"

        run_row = await _query_run(handle, tenant_a, rid1)
        assert run_row is not None
        assert run_row["context"] == {"k": "v"}, "幂等 start 不得覆盖既有 context"
        steps = await _query_steps(handle, tenant_a, rid1)
        assert len(steps) == 1, "幂等 start 不得重复创建首步"

        rid3 = await engine.start(TenantId(tenant_b), "wf", "subj-b", {}, "idem-1")
        assert rid3 != rid1, "异租户同 key 应各自独立"

        with pytest.raises(ValidationError):
            await engine.start(TenantId(tenant_a), "wf", "subj-a", {}, "   ")
    finally:
        await handle.dispose()


@pytest.mark.parametrize("mismatch", ("subject", "type", "version"))
async def test_concurrent_start_same_key_rejects_mismatched_binding(
    db_url: str,
    mismatch: str,
) -> None:
    """并发共 key 只能复用完全一致的 type/version/subject，且错误固定脱敏。"""

    tenant = TenantId(f"tIdemBinding{mismatch}")
    key = f"private-idempotency-{mismatch}"
    type_a = "binding-a"
    type_b = "binding-b" if mismatch == "type" else type_a
    subject_a = "binding-subject-a"
    subject_b = "binding-subject-b" if mismatch == "subject" else subject_a
    version_a = 1
    version_b = 2 if mismatch == "version" else version_a
    definition_a = WorkflowDefinition(
        workflow_type=type_a,
        version=version_a,
        steps=(StepDefinition(step_name="first", handler_ref="h"),),
        transitions={},
    )
    definition_b = WorkflowDefinition(
        workflow_type=type_b,
        version=version_b,
        steps=(StepDefinition(step_name="first", handler_ref="h"),),
        transitions={},
    )
    engine_a, handle_a = _make_engine(db_url, {"h": _handler(lambda _: ("complete", None, {}))})
    engine_b, handle_b = _make_engine(db_url, {"h": _handler(lambda _: ("complete", None, {}))})
    engine_a.register(definition_a)
    engine_b.register(definition_b)
    try:
        results = await asyncio.gather(
            engine_a.start(tenant, type_a, subject_a, {"owner": "a"}, key),
            engine_b.start(tenant, type_b, subject_b, {"owner": "b"}, key),
            return_exceptions=True,
        )

        errors = [result for result in results if isinstance(result, ValidationError)]
        run_ids = [result for result in results if isinstance(result, str)]
        assert len(errors) == 1
        assert len(run_ids) == 1
        assert str(errors[0]) == "workflow 幂等键与既有 Run 绑定不一致"
        assert key not in str(errors[0])
        assert subject_b not in str(errors[0])
        async with handle_a.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM workflow_runs "
                        "WHERE tenant_id = :tenant AND idempotency_key = :key"
                    ),
                    {"tenant": str(tenant), "key": key},
                )
                == 1
            )
    finally:
        await handle_a.dispose()
        await handle_b.dispose()


async def test_deployment_keeps_persisted_account_discovery_v1_executable_and_starts_v2(
    db_url: str,
) -> None:
    """旧部署已持久化 v1；新部署同时注册 v1/v2 后旧 run 可完成，新 run 选 v2。"""

    def _complete(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {"completed_version": run.workflow_version})

    handler = _handler(_complete)
    handlers = {
        "account_discovery.find_company_details": handler,
        "account_discovery.resolve_account": handler,
        "account_discovery.find_contacts": handler,
        "account_discovery.verify_contacts": handler,
        "account_discovery.assign_owner": handler,
        "account_discovery.enroll_campaign": handler,
        "account_discovery.bind_campaign": handler,
        "account_discovery.assign_owner_v2": handler,
        "account_discovery.await_campaign_activation": handler,
        "account_discovery.enroll_campaign_v2": handler,
    }
    tenant = TenantId("tAccountCompat")
    old_engine, old_handle = _make_engine(db_url, handlers)
    try:
        old_engine.register(build_legacy_account_discovery_definition())
        old_run_id = await old_engine.start(
            tenant,
            "account_discovery",
            "legacy-subject",
            {"legacy_context": True},
            "account-compat-v1",
        )
    finally:
        await old_handle.dispose()

    new_engine, new_handle = _make_engine(db_url, handlers)
    try:
        register_account_discovery(new_engine)
        new_run_id = await new_engine.start(
            tenant,
            "account_discovery",
            "new-subject",
            {"new_context": True},
            "account-compat-v2",
        )
        assert await new_engine.poll_due(tenant, limit=10) == 2

        old_row = await _query_run(new_handle, str(tenant), old_run_id)
        new_row = await _query_run(new_handle, str(tenant), new_run_id)
        assert old_row is not None and old_row["workflow_version"] == 1
        assert old_row["status"] == "completed"
        assert old_row["context"]["completed_version"] == 1
        assert new_row is not None and new_row["workflow_version"] == 2
        assert new_row["status"] == "completed"
        assert new_row["context"]["completed_version"] == 2
    finally:
        await new_handle.dispose()


# --- poll_due：推进 / 并发 / 独立事务 / backoff / 永久失败 ---------------------------


def _advance_flow() -> WorkflowDefinition:
    """init→wait(approval)→done 线性流程。"""
    return WorkflowDefinition(
        workflow_type="wf",
        version=1,
        steps=(
            StepDefinition(step_name="init", handler_ref="h_init"),
            StepDefinition(step_name="wait", handler_ref="h_wait", wait_event_type="approval"),
            StepDefinition(step_name="done", handler_ref="h_done"),
        ),
        transitions={"init": ("wait",), "wait": ("done",)},
    )


async def test_poll_due_advances_flow_with_context_merge(db_url: str) -> None:
    """poll_due 推进：init 执行 → advance 到 wait；patch 确定性合并进 run.context。"""
    init_calls: list[str] = []

    def _h_init(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        init_calls.append(run.run_id)
        return ("advance", "wait", {"init_done": True})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("wait", None, {})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {"finished": True})

    clock = _Clock()
    engine, handle = _make_engine(
        db_url,
        {"h_init": _handler(_h_init), "h_wait": _handler(_h_wait), "h_done": _handler(_h_done)},
        clock=clock,
    )
    try:
        engine.register(_advance_flow())
        tenant = "tPoll"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {"seed": 1}, "poll-key")

        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None
        assert run_row["current_step"] == "wait"
        assert run_row["status"] == "running"
        assert run_row["context"] == {"seed": 1, "init_done": True}
        assert run_row["next_poll_at"] is None
        assert len(init_calls) == 1

        steps = await _query_steps(handle, tenant, rid)
        by_name = {str(s["step_name"]): s for s in steps}
        assert by_name["init"]["status"] == "completed"
        assert by_name["wait"]["status"] == "waiting_event"

        assert await engine.poll_due(TenantId(tenant), 10) == 0, "waiting 步骤不应被扫描领取"
        assert await engine.poll_due(TenantId(tenant), 0) == 0, "limit<=0 应 no-op"
    finally:
        await handle.dispose()


async def test_poll_due_skip_locked_no_double_claim(db_url: str) -> None:
    """两个并发 poll_due：FOR UPDATE SKIP LOCKED 保证同一批不重复领取、handler 只跑一次。"""
    calls: list[str] = []

    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        calls.append(run.run_id)
        return ("complete", None, {})

    engine_a, handle_a = _make_engine(db_url, {"h": _handler(_h)})
    engine_b, handle_b = _make_engine(db_url, {"h": _handler(_h)})
    try:
        definition = _simple_def()
        engine_a.register(definition)
        engine_b.register(definition)
        tenant = "tSkipLock"
        rid = await engine_a.start(TenantId(tenant), "wf", "subj", {}, "skip-lock-key")
        results = await asyncio.gather(
            engine_a.poll_due(TenantId(tenant), 10),
            engine_b.poll_due(TenantId(tenant), 10),
        )
        assert sum(results) == 1, f"SKIP LOCKED 下应恰好一个领取：{results}"
        assert len(calls) == 1, "handler 不得被重复执行"
        run_row = await _query_run(handle_a, tenant, rid)
        assert run_row is not None and run_row["status"] == "completed"
    finally:
        await handle_a.dispose()
        await handle_b.dispose()


async def test_poll_due_does_not_claim_workflow_types_unregistered_on_this_engine(
    db_url: str,
) -> None:
    """同租户多 worker 可注册互斥流程；各自只能领取自己的 (type, version)。"""
    calls: list[str] = []

    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        calls.append(run.workflow_type)
        return ("complete", None, {})

    engine_a, handle_a = _make_engine(db_url, {"h": _handler(_h)})
    engine_b, handle_b = _make_engine(db_url, {"h": _handler(_h)})
    definition_a = WorkflowDefinition(
        workflow_type="worker-a",
        version=1,
        steps=(StepDefinition("first", "h"),),
        transitions={},
    )
    definition_b = WorkflowDefinition(
        workflow_type="worker-b",
        version=1,
        steps=(StepDefinition("first", "h"),),
        transitions={},
    )
    try:
        engine_a.register(definition_a)
        engine_b.register(definition_b)
        tenant = TenantId("tDisjointWorkers")
        run_id = await engine_a.start(
            tenant,
            "worker-a",
            "owned-by-a",
            {},
            "disjoint-worker-a",
        )

        assert await engine_b.poll_due(tenant, 10) == 0
        untouched = await _query_run(handle_a, str(tenant), run_id)
        assert untouched is not None
        assert untouched["status"] == "running"
        assert untouched["last_error"] is None
        assert calls == []

        assert await engine_a.poll_due(tenant, 10) == 1
        completed = await _query_run(handle_a, str(tenant), run_id)
        assert completed is not None and completed["status"] == "completed"
        assert calls == ["worker-a"]
    finally:
        await handle_a.dispose()
        await handle_b.dispose()


async def test_poll_due_one_step_failure_does_not_block_batch(db_url: str) -> None:
    """每步独立事务：一个 run 永久失败不阻塞本批其他 run 的推进。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        if run.subject_ref == "a":
            raise RuntimeError("A-boom-secret")
        return ("complete", None, {"ok": True})

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        engine.register(_simple_def())
        tenant = "tBatch"
        rid_a = await engine.start(TenantId(tenant), "wf", "a", {}, "batch-a")
        rid_b = await engine.start(TenantId(tenant), "wf", "b", {}, "batch-b")

        processed = await engine.poll_due(TenantId(tenant), 10)
        assert processed == 2, "一步失败不应阻塞本批其他步骤"

        run_a = await _query_run(handle, tenant, rid_a)
        run_b = await _query_run(handle, tenant, rid_b)
        assert run_a is not None and run_a["status"] == "failed"
        assert run_b is not None and run_b["status"] == "completed"
        assert "A-boom-secret" not in (run_a["last_error"] or ""), "last_error 不得泄露异常消息"
        assert "RuntimeError" in (run_a["last_error"] or "")
    finally:
        await handle.dispose()


async def test_transient_error_backoff_and_exhaustion(db_url: str) -> None:
    """TransientError：指数退避更新 attempt/retry_count/next_poll_at/error；
    耗尽 max_retries 转 FAILED 且不再 tight loop；last_error 脱敏。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        raise TransientError("boom-secret")

    clock = _Clock()
    definition = WorkflowDefinition(
        workflow_type="wf",
        version=1,
        steps=(
            StepDefinition(
                step_name="first",
                handler_ref="h",
                max_retries=2,
                retry_backoff=timedelta(seconds=10),
            ),
        ),
        transitions={},
    )
    engine, handle = _make_engine(db_url, {"h": _handler(_h)}, clock=clock)
    try:
        engine.register(definition)
        tenant = "tRetry"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "retry-key")

        # 第一次 TransientError：attempt=1，退避 10s（2^0 * base）
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "running"
        assert run_row["retry_count"] == 1
        assert run_row["next_poll_at"] == _NOW + timedelta(seconds=10)
        assert "boom-secret" not in (run_row["last_error"] or "")
        step = (await _query_steps(handle, tenant, rid))[0]
        assert step["attempt"] == 1
        assert step["due_at"] == _NOW + timedelta(seconds=10)
        assert step["error"] == "TransientError"

        # 第二次 TransientError：attempt=2，退避 20s（2^1 * base）
        clock.advance(seconds=10)
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["retry_count"] == 2
        assert run_row["next_poll_at"] == _NOW + timedelta(seconds=30)
        step = (await _query_steps(handle, tenant, rid))[0]
        assert step["attempt"] == 2
        assert step["due_at"] == _NOW + timedelta(seconds=30)

        # 第三次：attempt=3 > max_retries=2 → FAILED
        clock.advance(seconds=20)
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "failed"
        assert "boom-secret" not in (run_row["last_error"] or "")
        step = (await _query_steps(handle, tenant, rid))[0]
        assert step["status"] == "failed"

        # 不再 tight loop：无 pending/running 到期步骤
        assert await engine.poll_due(TenantId(tenant), 10) == 0
    finally:
        await handle.dispose()


async def test_permanent_error_sanitized(db_url: str) -> None:
    """非 TransientError 一律永久失败：last_error 只留类型名、不泄露异常消息/payload。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        raise ValueError("leaked-secret-xyz")

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        engine.register(_simple_def())
        tenant = "tPerm"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "perm-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "failed"
        assert "leaked-secret-xyz" not in (run_row["last_error"] or "")
        assert "ValueError" in (run_row["last_error"] or "")
        step = (await _query_steps(handle, tenant, rid))[0]
        assert step["status"] == "failed"
    finally:
        await handle.dispose()


async def test_invalid_transition_is_permanent_failure(db_url: str) -> None:
    """handler 跳到未声明/未知步骤：永久失败、不越权推进。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "ghost", {})

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        engine.register(_simple_def())  # transitions={}：不允许 advance
        tenant = "tBadTxn"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "badtxn-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "failed"
        assert run_row["current_step"] == "first", "非法 transition 不得改写 current_step"
    finally:
        await handle.dispose()


# --- deliver_event：推进 / 幂等 / 指纹去重 / 越权 no-op ------------------------------


async def test_deliver_event_advances_and_duplicate_noop(db_url: str) -> None:
    """deliver_event：同租户 WAITING_EVENT + 事件类型匹配时推进；重复投递同一事件幂等 no-op。"""
    wait_calls: list[str] = []

    def _h_init(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        wait_calls.append(run.run_id)
        payload = run.context["event"]["payload"]
        return ("advance", "done", {"approved": payload["by"]})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {"finished": True})

    clock = _Clock()
    engine, handle = _make_engine(
        db_url,
        {"h_init": _handler(_h_init), "h_wait": _handler(_h_wait), "h_done": _handler(_h_done)},
        clock=clock,
    )
    try:
        engine.register(_advance_flow())
        tenant = "tEvt"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "evt-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1  # init → wait

        assert await engine.deliver_event(
            TenantId(tenant), rid, "approval", {"by": "bob"}
        )
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None
        assert run_row["current_step"] == "done"
        assert run_row["context"]["approved"] == "bob"
        assert len(wait_calls) == 1

        assert not await engine.deliver_event(
            TenantId(tenant), rid, "approval", {"by": "bob"}
        )
        assert len(wait_calls) == 1, "重复投递同一事件不得重复推进"

        assert await engine.poll_due(TenantId(tenant), 10) == 1  # done → complete
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "completed"
    finally:
        await handle.dispose()


async def test_deliver_event_wrong_type_tenant_unknown_noop(db_url: str) -> None:
    """deliver_event：错误租户/未知 run/事件类型不匹配均 no-op，不越权读取或修改。"""
    wait_calls: list[str] = []

    def _h_init(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        wait_calls.append(run.run_id)
        return ("advance", "done", {})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(
        db_url,
        {"h_init": _handler(_h_init), "h_wait": _handler(_h_wait), "h_done": _handler(_h_done)},
    )
    try:
        engine.register(_advance_flow())
        tenant = "tNoop"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "noop-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1  # → wait

        assert not await engine.deliver_event(
            TenantId("tOther"), rid, "approval", {"by": "bob"}
        )
        assert not await engine.deliver_event(
            TenantId(tenant), "run-unknown", "approval", {"by": "bob"}
        )
        assert not await engine.deliver_event(
            TenantId(tenant), rid, "wrong_type", {"by": "bob"}
        )
        assert wait_calls == []

        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None
        assert run_row["current_step"] == "wait"
        assert run_row["status"] == "running"
    finally:
        await handle.dispose()


async def test_deliver_event_fingerprint_dedup_same_type_two_steps(db_url: str) -> None:
    """两个连续 WAITING_EVENT 步骤等待同一事件类型：同 payload 幂等 no-op、不同 payload 才推进。"""
    reply_calls: list[str] = []

    def _h_start(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait1", {})

    def _h_reply(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        reply_calls.append(run.run_id)
        if run.current_step == "wait1":
            return ("advance", "wait2", {"seen_1": True})
        return ("advance", "end", {"seen_2": True})

    def _h_end(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    definition = WorkflowDefinition(
        workflow_type="wf",
        version=1,
        steps=(
            StepDefinition(step_name="start", handler_ref="h_start"),
            StepDefinition(step_name="wait1", handler_ref="h_reply", wait_event_type="reply"),
            StepDefinition(step_name="wait2", handler_ref="h_reply", wait_event_type="reply"),
            StepDefinition(step_name="end", handler_ref="h_end"),
        ),
        transitions={"start": ("wait1",), "wait1": ("wait2",), "wait2": ("end",)},
    )
    engine, handle = _make_engine(
        db_url,
        {"h_start": _handler(_h_start), "h_reply": _handler(_h_reply), "h_end": _handler(_h_end)},
    )
    try:
        engine.register(definition)
        tenant = "tFp"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "fp-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1  # start → wait1

        await engine.deliver_event(TenantId(tenant), rid, "reply", {"id": "1"})
        assert len(reply_calls) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["current_step"] == "wait2"

        await engine.deliver_event(TenantId(tenant), rid, "reply", {"id": "1"})
        assert len(reply_calls) == 1, "同一事件（同 payload）重复投递必须 no-op，不可重复推进"
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["current_step"] == "wait2"

        await engine.deliver_event(TenantId(tenant), rid, "reply", {"id": "2"})
        assert len(reply_calls) == 2, "不同事件（不同 payload）应推进下一等待步骤"
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["current_step"] == "end"
    finally:
        await handle.dispose()


async def test_two_distinct_wait_events_persist_ledger_copy_on_write(
    db_url: str,
) -> None:
    """连续空 patch 事件必须在新 session/engine 中留下两条 durable fingerprint。"""
    def _wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        next_step = "wait_two" if run.current_step == "wait_one" else "end"
        return ("advance", next_step, {})

    handlers = {
        "h_start": _handler(lambda run: ("advance", "wait_one", {})),
        "h_wait": _handler(_wait),
        "h_end": _handler(lambda run: ("complete", None, {})),
    }
    definition = WorkflowDefinition(
        workflow_type="wf-ledger-copy",
        version=1,
        steps=(
            StepDefinition("start", "h_start"),
            StepDefinition("wait_one", "h_wait", wait_event_type="approval_one"),
            StepDefinition("wait_two", "h_wait", wait_event_type="approval_two"),
            StepDefinition("end", "h_end"),
        ),
        transitions={
            "start": ("wait_one",),
            "wait_one": ("wait_two",),
            "wait_two": ("end",),
        },
    )
    engine, handle = _make_engine(db_url, handlers)
    try:
        engine.register(definition)
        tenant = TenantId("tLedgerCopy")
        first_payload = {"approved_by": "employee-one"}
        second_payload = {"approved_by": "employee-two"}
        run_id = await engine.start(
            tenant, "wf-ledger-copy", "subject", {}, "ledger-copy"
        )
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.deliver_event(
            tenant, run_id, "approval_one", first_payload
        )
        assert await engine.deliver_event(
            tenant, run_id, "approval_two", second_payload
        )
        assert await engine.poll_due(tenant, 1) == 1

        fresh_factory = async_sessionmaker(bind=handle, expire_on_commit=False)
        fresh_engine = _load_engine_cls()(fresh_factory, handlers)
        fresh_engine.register(definition)
        fresh_session = fresh_factory()
        try:
            persisted = (
                await fresh_session.execute(
                    text(
                        "SELECT status, context FROM workflow_runs "
                        "WHERE tenant_id = :tenant AND run_id = :run_id"
                    ),
                    {"tenant": str(tenant), "run_id": str(run_id)},
                )
            ).mappings().one()
        finally:
            await fresh_session.close()

        assert persisted["status"] == "completed"
        ledger = persisted["context"]["__wf_delivered_events"]
        assert len(ledger) == 2
        assert ledger[0] != ledger[1]
        assert all(_is_hex64(item) for item in ledger)
        assert "event" not in persisted["context"]
        assert await fresh_engine.has_delivered_event(
            tenant,
            "wf-ledger-copy",
            "subject",
            "approval_one",
            first_payload,
        )
        assert await fresh_engine.has_delivered_event(
            tenant,
            "wf-ledger-copy",
            "subject",
            "approval_two",
            second_payload,
        )
    finally:
        await handle.dispose()


async def test_delivered_event_evidence_survives_later_active_step_and_terminal(
    db_url: str,
) -> None:
    """严格 owning Run 过滤显式允许跨当前 step 与终态查询精确指纹。"""

    handlers = {
        "h_init": _handler(lambda run: ("advance", "wait", {})),
        "h_wait": _handler(lambda run: ("advance", "done", {})),
        "h_done": _handler(lambda run: ("complete", None, {})),
    }
    engine, handle = _make_engine(db_url, handlers)
    try:
        engine.register(replace(_advance_flow(), version=2))
        tenant = TenantId("tDurableEvidenceProgress")
        payload = {"plan_id": "spl_exact", "plan_hash": "a" * 64}
        run_id = await engine.start(
            tenant,
            "wf",
            "case-subject",
            {"case_id": "case-subject"},
            "durable-event",
        )
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.deliver_event(tenant, run_id, "approval", payload)

        progressed = await engine.get_run(tenant, run_id)
        assert progressed is not None
        assert progressed.current_step == "done"
        assert progressed.status is StepStatus.RUNNING
        assert await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context={"case_id": "case-subject"},
        )
        assert not await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            {"plan_id": "spl_exact", "plan_hash": "b" * 64},
            workflow_version=2,
            required_context={"case_id": "case-subject"},
        )

        assert await engine.poll_due(tenant, 1) == 1
        terminal = await engine.get_run(tenant, run_id)
        assert terminal is not None and terminal.status is StepStatus.COMPLETED
        assert await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context={"case_id": "case-subject"},
        )
    finally:
        await handle.dispose()


async def test_delivered_event_history_filters_owning_version_and_immutable_context(
    db_url: str,
) -> None:
    """历史指纹查询必须在 SQL 边界绑定 owning Run 版本与不可变 context。"""

    handlers = {
        "h_init": _handler(lambda run: ("advance", "wait", {})),
        "h_wait": _handler(lambda run: ("advance", "done", {})),
        "h_done": _handler(lambda run: ("complete", None, {})),
    }
    engine, handle = _make_engine(db_url, handlers)
    try:
        engine.register(replace(_advance_flow(), version=2))
        tenant = TenantId("tBoundDurableEventHistory")
        payload = {"plan_id": "spl_exact", "plan_hash": "a" * 64}
        generation_context = {
            "case_id": "case-subject",
            "supplier_candidate_ids": ["spc-first", "spc-second"],
            "candidate_case_version": 8,
            "candidate_set_hash": "c" * 64,
        }
        run_id = await engine.start(
            tenant,
            "wf",
            "case-subject",
            generation_context,
            "bound-durable-event",
        )
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.deliver_event(tenant, run_id, "approval", payload)

        assert await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context={"case_id": "case-subject"},
        )
        assert not await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=1,
            required_context={"case_id": "case-subject"},
        )
        assert not await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context={"case_id": "src_case-other"},
        )

        assert await engine.poll_due(tenant, 1) == 1
        next_run_id = await engine.start(
            tenant,
            "wf",
            "case-subject",
            generation_context,
            "bound-durable-event-next-generation",
        )
        assert not await engine.has_delivered_event(
            tenant, "wf", "case-subject", "approval", payload
        )
        assert await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context={"case_id": "case-subject"},
        )
        assert await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context=generation_context,
            run_id=run_id,
        )
        assert not await engine.has_delivered_event(
            tenant,
            "wf",
            "case-subject",
            "approval",
            payload,
            workflow_version=2,
            required_context=generation_context,
            run_id=next_run_id,
        )
    finally:
        await handle.dispose()


@pytest.mark.parametrize(
    "filters",
    [
        {"workflow_version": 2},
        {"required_context": {"case_id": "case-subject"}},
        {"workflow_version": 2, "required_context": {}},
    ],
)
async def test_delivered_event_history_filters_must_be_complete_and_nonempty(
    db_url: str, filters: dict[str, object]
) -> None:
    """部分或空 owning Run 过滤不得意外开启跨 generation 历史查询。"""

    engine, handle = _make_engine(db_url, {})
    try:
        with pytest.raises(ValidationError, match="过滤条件无效"):
            await engine.has_delivered_event(
                TenantId("tInvalidDurableEventFilters"),
                "wf",
                "case-subject",
                "approval",
                {"plan_id": "spl_exact", "plan_hash": "a" * 64},
                **filters,
            )
    finally:
        await handle.dispose()


# --- cancel：同租户 / 幂等 / 终态不复活 ---------------------------------------------


async def test_cancel_targets_tenant_and_is_idempotent(db_url: str) -> None:
    """cancel：只取消同租户目标；重复 cancel 幂等；已完成/失败终态不被复活；未知 run no-op。"""
    def _h_init(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("wait", None, {})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    def _h_complete(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(
        db_url,
        {
            "h_init": _handler(_h_init),
            "h_wait": _handler(_h_wait),
            "h_done": _handler(_h_done),
            "h_complete": _handler(_h_complete),
        },
    )
    try:
        # 取消一个正在等待的 run
        engine.register(_advance_flow())
        tenant = "tCancel"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "cancel-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1  # → wait
        await engine.cancel(TenantId(tenant), rid, "customer withdrew")
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "cancelled"
        steps = await _query_steps(handle, tenant, rid)
        by_name = {str(s["step_name"]): s for s in steps}
        assert by_name["wait"]["status"] == "cancelled", "运行中步骤应被取消"
        assert by_name["init"]["status"] == "completed", "已完成步骤不得被取消复活"

        await engine.cancel(TenantId(tenant), rid, "again")  # 重复 cancel 幂等
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "cancelled"

        # 已完成终态不被复活
        simple = WorkflowDefinition(
            workflow_type="wf-complete",
            version=1,
            steps=(StepDefinition(step_name="first", handler_ref="h_complete"),),
            transitions={},
        )
        engine.register(simple)
        rid_done = await engine.start(TenantId(tenant), "wf-complete", "subj", {}, "done-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        await engine.cancel(TenantId(tenant), rid_done, "too late")
        done_row = await _query_run(handle, tenant, rid_done)
        assert done_row is not None and done_row["status"] == "completed"

        # 未知 run / 错误租户 no-op
        await engine.cancel(TenantId(tenant), "run-unknown", "nope")
        await engine.cancel(TenantId("tOther"), rid, "nope")
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "cancelled"
    finally:
        await handle.dispose()


# --- Round 1 fixes：commit 级失败隔离 / 事件脱敏 / 保留键 / 首步等待 / reason 脱敏 / 退避校验 ---


def _is_hex64(value: object) -> bool:
    """事件指纹为 64 位 hex（SHA-256）固定长度，不携带 payload 原文。"""
    return isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value
    )


async def test_poll_due_commit_failure_isolated_and_no_tight_loop(db_url: str) -> None:
    """Critical 1：commit/flush 级失败（不可 JSON 序列化 patch）不得中断本批。

    坏 run 以固定脱敏错误进入 FAILED，同批另一 run 仍完成，后续 poll 不重复领取
    坏 step（无 tight loop）。断言只关心外部行为，不依赖 asyncpg/SQLAlchemy 包装
    异常类型。
    """
    def _h_bad(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {"unserializable": {1, 2, 3}})

    def _h_good(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {"ok": True})

    engine, handle = _make_engine(
        db_url, {"h_bad": _handler(_h_bad), "h_good": _handler(_h_good)}
    )
    try:
        engine.register(
            WorkflowDefinition(
                "wf-bad", 1, (StepDefinition("first", "h_bad"),), {}
            )
        )
        engine.register(
            WorkflowDefinition(
                "wf-good", 1, (StepDefinition("first", "h_good"),), {}
            )
        )
        tenant = "tCommitFail"
        rid_bad = await engine.start(TenantId(tenant), "wf-bad", "a", {}, "cf-a")
        rid_good = await engine.start(TenantId(tenant), "wf-good", "b", {}, "cf-b")

        processed = await engine.poll_due(TenantId(tenant), 10)
        assert processed == 2, f"commit 失败不得中断本批其他步骤：processed={processed}"

        bad = await _query_run(handle, tenant, rid_bad)
        good = await _query_run(handle, tenant, rid_good)
        assert bad is not None and bad["status"] == "failed"
        assert bad["context"] == {}, "commit 失败后不得残留部分写入的 patch"
        assert "unserializable" not in json.dumps(bad["context"])
        assert "step commit failure" in (bad["last_error"] or ""), "坏 run 应带固定脱敏错误"
        assert good is not None and good["status"] == "completed"

        bad_step = (await _query_steps(handle, tenant, rid_bad))[0]
        assert bad_step["status"] == "failed"
        assert bad_step["error"] == "step commit failure", "坏 step 错误应为固定脱敏文本"

        assert await engine.poll_due(TenantId(tenant), 10) == 0, "坏 step 不得 tight loop"
    finally:
        await handle.dispose()


async def test_event_fingerprint_is_digest_and_raw_event_not_persisted(db_url: str) -> None:
    """Important 2：事件指纹为固定长度密码学 digest；raw event 只在 handler 执行期可见。

    投递后 run.context 不得出现 payload 原文与 ``event`` 保留键；重复投递同一事件
    仍 durable no-op。
    """
    wait_calls: list[str] = []

    def _h_start(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        wait_calls.append(run.run_id)
        payload = run.context["event"]["payload"]
        return ("advance", "done", {"approved": payload["by"]})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    clock = _Clock()
    engine, handle = _make_engine(
        db_url,
        {"h_init": _handler(_h_start), "h_wait": _handler(_h_wait), "h_done": _handler(_h_done)},
        clock=clock,
    )
    try:
        engine.register(_advance_flow())
        tenant = "tDigest"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "digest-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1  # start → wait

        await engine.deliver_event(
            TenantId(tenant), rid, "approval",
            {"by": "bob", "token": "sk_live_secret_123"},
        )
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None
        assert run_row["current_step"] == "done"
        assert run_row["context"]["approved"] == "bob"
        assert "event" not in run_row["context"], "raw event 不得持久化在 context"
        assert "sk_live_secret_123" not in json.dumps(run_row["context"])
        assert '"by"' not in json.dumps(run_row["context"]), "payload 原文不得残留"

        delivered = run_row["context"].get("__wf_delivered_events")
        assert isinstance(delivered, list) and len(delivered) == 1
        assert _is_hex64(delivered[0]), "事件指纹应为 64 位 hex digest，非 payload 原文"

        # 重复投递同一事件仍 durable no-op
        await engine.deliver_event(
            TenantId(tenant), rid, "approval", {"by": "bob", "token": "sk_live_secret_123"}
        )
        assert len(wait_calls) == 1, "重复投递同一事件不得重复推进"
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["current_step"] == "done"
    finally:
        await handle.dispose()


async def test_start_rejects_reserved_context_keys(db_url: str) -> None:
    """Important 3a：initial_context 含保留键（事件指纹簿记 / 事件）fail closed，写 DB 前拒绝。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        engine.register(_simple_def())
        tenant = "tReserved"
        with pytest.raises(ValidationError):
            await engine.start(
                TenantId(tenant), "wf", "subj", {"__wf_delivered_events": ["x"]}, "rk-1"
            )
        with pytest.raises(ValidationError):
            await engine.start(
                TenantId(tenant), "wf", "subj", {"event": {"payload": {}}}, "rk-2"
            )
        # 拒绝后不得留下任何 run 行（写 DB 前校验）
        async with handle.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM workflow_runs WHERE tenant_id = :t"),
                    {"t": tenant},
                )
            ).scalar_one()
        assert count == 0, "保留键拒绝后不得创建 run"
    finally:
        await handle.dispose()


async def test_handler_patch_reserved_key_fails_closed(db_url: str) -> None:
    """Important 3b：handler patch 含保留键必须 fail closed，不得覆盖引擎簿记。"""
    def _h_forge(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {"event": "forged"})

    engine, handle = _make_engine(db_url, {"h_forge": _handler(_h_forge)})
    try:
        engine.register(
            WorkflowDefinition(
                "wf-forge", 1, (StepDefinition("first", "h_forge"),), {}
            )
        )
        tenant = "tForge"
        rid = await engine.start(TenantId(tenant), "wf-forge", "subj", {}, "forge-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1

        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "failed"
        assert run_row["current_step"] == "first", "保留键污染不得改写 current_step"
        assert "forged" not in json.dumps(run_row["context"]), "伪造保留键不得落库"
        assert "event" not in run_row["context"]
    finally:
        await handle.dispose()


async def test_corrupt_delivered_events_context_fails_closed(db_url: str) -> None:
    """Important 3c：DB 中遗留/损坏的保留键值（非字符串列表）fail closed，不崩溃不卡批次。"""
    def _h_start(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "done", {})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(
        db_url,
        {"h_init": _handler(_h_start), "h_wait": _handler(_h_wait), "h_done": _handler(_h_done)},
    )
    try:
        engine.register(_advance_flow())
        tenant = "tCorrupt"
        run_id = "run-corrupt-1"
        async with handle.begin() as conn:
            await conn.execute(
                _INSERT_RUN,
                _run_params(
                    run_id,
                    tenant,
                    key="corrupt-key",
                    current_step="wait",
                    context={"__wf_delivered_events": {"nested": "thing"}},
                ),
            )
            await conn.execute(
                _INSERT_STEP,
                _step_params(
                    "stp-corrupt-1", run_id, tenant,
                    step_name="wait", key="corrupt-sk", status="waiting_event",
                ),
            )

        # 损坏的保留键值：deliver_event 必须 fail closed，不得 AttributeError 逃逸。
        await engine.deliver_event(TenantId(tenant), run_id, "approval", {"by": "bob"})
        run_row = await _query_run(handle, tenant, run_id)
        assert run_row is not None and run_row["status"] == "failed"
        assert "corrupted" in (run_row["last_error"] or ""), "损坏 context 应带固定脱敏错误"
        assert "bob" not in json.dumps(run_row["context"]), "事件 payload 不得写入损坏 context"
    finally:
        await handle.dispose()


async def test_has_delivered_event_rejects_mixed_fingerprint_list(
    db_url: str,
) -> None:
    """durable evidence 中混入格式错误字符串即整体损坏，不得凭 hash 放行。"""
    engine, handle = _make_engine(
        db_url,
        {
            "h_init": _handler(lambda run: ("advance", "wait", {})),
            "h_wait": _handler(lambda run: ("advance", "done", {})),
            "h_done": _handler(lambda run: ("complete", None, {})),
        },
    )
    try:
        engine.register(_advance_flow())
        tenant = TenantId("tMixedEvidence")
        payload = {"by": "bob"}
        run_id = await engine.start(tenant, "wf", "subject", {}, "mixed-evidence")
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.deliver_event(tenant, run_id, "approval", payload)
        run = await _query_run(handle, str(tenant), run_id)
        assert run is not None
        fingerprint = run["context"]["__wf_delivered_events"][0]
        assert await engine.poll_due(tenant, 1) == 1
        async with handle.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE workflow_runs SET context = CAST(:context AS jsonb) "
                    "WHERE tenant_id = :tenant AND run_id = :run_id"
                ),
                {
                    "context": json.dumps(
                        {"__wf_delivered_events": [fingerprint.upper()]}
                    ),
                    "tenant": str(tenant),
                    "run_id": str(run_id),
                },
            )
        assert await engine.has_delivered_event(
            tenant, "wf", "subject", "approval", payload
        )
        corrupted = {"__wf_delivered_events": [fingerprint, "corrupt"]}
        async with handle.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE workflow_runs SET context = CAST(:context AS jsonb) "
                    "WHERE tenant_id = :tenant AND run_id = :run_id"
                ),
                {
                    "context": json.dumps(corrupted),
                    "tenant": str(tenant),
                    "run_id": str(run_id),
                },
            )
        assert not await engine.has_delivered_event(
            tenant, "wf", "subject", "approval", payload
        )
        async with handle.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE workflow_runs SET context = CAST(:context AS jsonb) "
                    "WHERE tenant_id = :tenant AND run_id = :run_id"
                ),
                {
                    "context": json.dumps(["broken"]),
                    "tenant": str(tenant),
                    "run_id": str(run_id),
                },
            )
        assert not await engine.has_delivered_event(
            tenant, "wf", "subject", "approval", payload
        )
    finally:
        await handle.dispose()


async def test_deliver_event_rejects_malformed_string_fingerprint(
    db_url: str,
) -> None:
    """active wait 的 ledger 含目标 hash + 非 SHA 字符串时必须 FAILED，不执行 handler。"""
    wait_calls: list[str] = []

    def _wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        wait_calls.append(str(run.run_id))
        return ("advance", "done", {})

    engine, handle = _make_engine(
        db_url,
        {
            "h_init": _handler(lambda run: ("advance", "wait", {})),
            "h_wait": _handler(_wait),
            "h_done": _handler(lambda run: ("complete", None, {})),
        },
    )
    try:
        engine.register(_advance_flow())
        tenant = TenantId("tMalformedDeliveryEvidence")
        payload = {"by": "bob"}
        completed = await engine.start(
            tenant, "wf", "completed", {}, "malformed-source"
        )
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.deliver_event(tenant, completed, "approval", payload)
        completed_row = await _query_run(handle, str(tenant), completed)
        assert completed_row is not None
        fingerprint = completed_row["context"]["__wf_delivered_events"][0]
        assert await engine.poll_due(tenant, 1) == 1

        waiting = await engine.start(
            tenant, "wf", "waiting", {}, "malformed-target"
        )
        assert await engine.poll_due(tenant, 1) == 1
        async with handle.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE workflow_runs SET context = CAST(:context AS jsonb) "
                    "WHERE tenant_id = :tenant AND run_id = :run_id"
                ),
                {
                    "context": json.dumps(
                        {"__wf_delivered_events": [fingerprint, "corrupt"]}
                    ),
                    "tenant": str(tenant),
                    "run_id": str(waiting),
                },
            )
        assert not await engine.deliver_event(
            tenant, waiting, "approval", payload
        )
        waiting_row = await _query_run(handle, str(tenant), waiting)
        assert waiting_row is not None and waiting_row["status"] == "failed"
        assert waiting_row["last_error"] == (
            "step wait failed: corrupted workflow context"
        )
        assert wait_calls == [str(completed)]
    finally:
        await handle.dispose()


async def test_first_step_with_wait_event_is_not_polled(db_url: str) -> None:
    """Important 4：首步本身等待事件时直接创建为 waiting_event；poll_due 不领取，仅
    deliver_event 匹配后才执行 handler。"""
    first_calls: list[str] = []

    def _h_first(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        first_calls.append(run.run_id)
        return ("advance", "done", {"seen": run.context["event"]["payload"]["v"]})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    definition = WorkflowDefinition(
        workflow_type="wf",
        version=1,
        steps=(
            StepDefinition(step_name="first", handler_ref="h_first", wait_event_type="approval"),
            StepDefinition(step_name="done", handler_ref="h_done"),
        ),
        transitions={"first": ("done",)},
    )
    engine, handle = _make_engine(
        db_url, {"h_first": _handler(_h_first), "h_done": _handler(_h_done)}
    )
    try:
        engine.register(definition)
        tenant = "tFirstWait"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "firstwait-key")

        steps = await _query_steps(handle, tenant, rid)
        assert steps[0]["status"] == "waiting_event", "首步等待事件应直接以 waiting_event 创建"

        assert await engine.poll_due(TenantId(tenant), 10) == 0, "waiting 首步不得被 poll 领取"
        assert first_calls == []

        await engine.deliver_event(TenantId(tenant), rid, "approval", {"v": 7})
        assert len(first_calls) == 1, "匹配事件后首步 handler 才执行"
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["current_step"] == "done"
        assert run_row["context"]["seen"] == 7

        assert await engine.poll_due(TenantId(tenant), 10) == 1  # done → complete
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "completed"
    finally:
        await handle.dispose()


async def test_handler_declared_fail_reason_sanitized(db_url: str) -> None:
    """Controller 5：handler 声明的 fail reason 原样写入 error 字段会泄露凭证；必须用
    固定安全状态文本，敏感原文不得出现在 last_error / step error / context / data。"""
    def _h_fail(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("fail", "credential-xyz-secret", {})

    engine, handle = _make_engine(db_url, {"h_fail": _handler(_h_fail)})
    try:
        engine.register(
            WorkflowDefinition(
                "wf-fail", 1, (StepDefinition("first", "h_fail"),), {}
            )
        )
        tenant = "tFailReason"
        rid = await engine.start(TenantId(tenant), "wf-fail", "subj", {}, "failreason-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1

        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "failed"
        assert "credential-xyz-secret" not in (run_row["last_error"] or "")
        assert "credential-xyz-secret" not in json.dumps(run_row["context"])
        step = (await _query_steps(handle, tenant, rid))[0]
        assert "credential-xyz-secret" not in (step["error"] or "")
        assert "credential-xyz-secret" not in json.dumps(step["data"])
    finally:
        await handle.dispose()


async def test_cancel_reason_sanitized(db_url: str) -> None:
    """Controller 5：cancel(reason) 不得把原始 reason 写入 last_error；敏感原文不落库。"""
    def _h_start(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _h_wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("wait", None, {})

    def _h_done(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(
        db_url,
        {"h_init": _handler(_h_start), "h_wait": _handler(_h_wait), "h_done": _handler(_h_done)},
    )
    try:
        engine.register(_advance_flow())
        tenant = "tCancelReason"
        rid = await engine.start(TenantId(tenant), "wf", "subj", {}, "cancelreason-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1  # → wait

        await engine.cancel(TenantId(tenant), rid, "gmail-oauth2-secret-abc")
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "cancelled"
        assert "gmail-oauth2-secret-abc" not in (run_row["last_error"] or "")
    finally:
        await handle.dispose()


async def test_register_rejects_nonpositive_backoff_and_negative_retries(db_url: str) -> None:
    """Controller 6：非正退避/负重试上限会在零退避下 tight loop；注册时 fail closed。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        zero_backoff = WorkflowDefinition(
            "wf-zero", 1,
            (StepDefinition("first", "h", retry_backoff=timedelta(seconds=0)),),
            {},
        )
        with pytest.raises(ValueError):
            engine.register(zero_backoff)

        negative_backoff = WorkflowDefinition(
            "wf-neg", 1,
            (StepDefinition("first", "h", retry_backoff=timedelta(seconds=-5)),),
            {},
        )
        with pytest.raises(ValueError):
            engine.register(negative_backoff)

        negative_retries = WorkflowDefinition(
            "wf-negretry", 1,
            (StepDefinition("first", "h", max_retries=-1),),
            {},
        )
        with pytest.raises(ValueError):
            engine.register(negative_retries)

        # 合法边界：max_retries=0（立即耗尽）与正退避应接受
        engine.register(
            WorkflowDefinition(
                "wf-ok", 1,
                (StepDefinition("first", "h", max_retries=0, retry_backoff=timedelta(seconds=1)),),
                {},
            )
        )
        engine.register(_simple_def("wf-ok2"))
    finally:
        await handle.dispose()


async def test_wait_and_complete_with_next_step_fail_closed(db_url: str) -> None:
    """Minor：wait/complete action 携带非 None next_step 应 fail closed（非法转换）。"""
    def _h_wait_next(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("wait", "somewhere", {})

    def _h_complete_next(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", "next", {})

    engine, handle = _make_engine(
        db_url,
        {"h_wait_next": _handler(_h_wait_next), "h_complete_next": _handler(_h_complete_next)},
    )
    try:
        engine.register(
            WorkflowDefinition(
                "wf-waitnext", 1, (StepDefinition("first", "h_wait_next"),), {}
            )
        )
        tenant = "tWaitNext"
        rid = await engine.start(TenantId(tenant), "wf-waitnext", "subj", {}, "waitnext-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None and run_row["status"] == "failed", "wait+next_step 应永久失败"

        engine.register(
            WorkflowDefinition(
                "wf-completenext", 1, (StepDefinition("first", "h_complete_next"),), {}
            )
        )
        rid2 = await engine.start(TenantId(tenant), "wf-completenext", "subj", {}, "cnext-key")
        assert await engine.poll_due(TenantId(tenant), 10) == 1
        run2 = await _query_run(handle, tenant, rid2)
        assert run2 is not None and run2["status"] == "failed", "complete+next_step 应永久失败"
    finally:
        await handle.dispose()


# --- S3-10 preflight：active-run lookup + WAITING_EVENT 绝对时限 -----------------


async def test_find_active_run_is_tenant_type_subject_scoped(db_url: str) -> None:
    """同租户/type/subject 命中；跨租户、终态均不可见。"""
    def _h(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    engine, handle = _make_engine(db_url, {"h": _handler(_h)})
    try:
        engine.register(_simple_def("lookup-wf"))
        tenant = TenantId("tLookup")
        active = await engine.start(tenant, "lookup-wf", "hand-1", {}, "lookup-active")
        completed = RunId("run-lookup-completed")
        async with handle.begin() as conn:
            await conn.execute(
                _INSERT_RUN,
                {
                    **_run_params(
                        str(completed),
                        str(tenant),
                        key="lookup-completed",
                        status="completed",
                    ),
                    "workflow_type": "lookup-wf",
                    "subject_ref": "hand-2",
                },
            )
        assert await engine.find_active_run(tenant, "lookup-wf", "hand-1") == active
        assert (
            await engine.find_active_run(TenantId("tOther"), "lookup-wf", "hand-1")
            is None
        )
        assert await engine.find_active_run(tenant, "other-wf", "hand-1") is None
        assert await engine.find_active_run(tenant, "lookup-wf", "hand-2") is None
        completed_row = await _query_run(handle, str(tenant), completed)
        assert completed_row is not None and completed_row["status"] == "completed"
    finally:
        await handle.dispose()


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_find_active_run_excludes_every_terminal_status(
    db_url: str, terminal: str
) -> None:
    """completed/failed/cancelled 都不是 active。"""
    engine, handle = _make_engine(db_url, {"h": _handler(lambda run: ("complete", None, {}))})
    try:
        tenant = "tLookupTerminal"
        async with handle.begin() as conn:
            await conn.execute(
                _INSERT_RUN,
                {
                    **_run_params(
                        f"run-{terminal}", tenant, key=f"key-{terminal}", status=terminal
                    ),
                    "workflow_type": "lookup-wf",
                    "subject_ref": f"hand-{terminal}",
                },
            )
        assert (
            await engine.find_active_run(
                TenantId(tenant), "lookup-wf", f"hand-{terminal}"
            )
            is None
        )
    finally:
        await handle.dispose()


async def test_find_active_run_duplicate_active_fails_closed(db_url: str) -> None:
    """异常重复 active 数据不得任取一条。"""
    engine, handle = _make_engine(db_url, {"h": _handler(lambda run: ("complete", None, {}))})
    try:
        tenant = "tLookupDuplicate"
        async with handle.begin() as conn:
            for suffix in ("a", "b"):
                await conn.execute(
                    _INSERT_RUN,
                    {
                        **_run_params(
                            f"run-dup-{suffix}", tenant, key=f"key-dup-{suffix}"
                        ),
                        "workflow_type": "lookup-wf",
                        "subject_ref": "hand-dup",
                    },
                )
        with pytest.raises(ValidationError):
            await engine.find_active_run(
                TenantId(tenant), "lookup-wf", "hand-dup"
            )
    finally:
        await handle.dispose()


def _timeout_flow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="timeout-wf",
        version=1,
        steps=(
            StepDefinition("notify", "h_notify"),
            StepDefinition(
                "wait",
                "h_wait",
                timeout=timedelta(seconds=10),
                on_timeout="escalate",
                wait_event_type="accepted",
            ),
            StepDefinition("escalate", "h_escalate"),
        ),
        transitions={"notify": ("wait",), "wait": ("escalate",)},
    )


async def test_early_event_schedules_ordinary_successor_at_transition_now(
    db_url: str,
) -> None:
    """默认策略下提前事件推进到普通 step，应按事件处理 now 立即可领取。"""
    clock = _Clock()
    definition = WorkflowDefinition(
        workflow_type="early-event-ordinary",
        version=1,
        steps=(
            StepDefinition("init", "h_init"),
            StepDefinition(
                "wait",
                "h_wait",
                timeout=timedelta(seconds=30),
                on_timeout="timed_out",
                wait_event_type="approval",
            ),
            StepDefinition("ordinary", "h_ordinary"),
            StepDefinition("timed_out", "h_timed_out"),
        ),
        transitions={
            "init": ("wait",),
            "wait": ("ordinary", "timed_out"),
        },
    )
    engine, handle = _make_engine(
        db_url,
        {
            "h_init": _handler(lambda run: ("advance", "wait", {})),
            "h_wait": _handler(lambda run: ("advance", "ordinary", {})),
            "h_ordinary": _handler(lambda run: ("complete", None, {})),
            "h_timed_out": _handler(lambda run: ("complete", None, {})),
        },
        clock=clock,
    )
    try:
        engine.register(definition)
        tenant = TenantId("tEarlyOrdinary")
        run_id = await engine.start(
            tenant, "early-event-ordinary", "subject", {}, "early-ordinary"
        )
        assert await engine.poll_due(tenant, 1) == 1
        clock.advance(seconds=5)
        assert await engine.deliver_event(
            tenant, run_id, "approval", {"approved": True}
        )
        steps = {
            row["step_name"]: row
            for row in await _query_steps(handle, str(tenant), run_id)
        }
        assert steps["ordinary"]["due_at"] == _NOW + timedelta(seconds=5)
        assert steps["ordinary"]["data"]["planned_at"] == (
            _NOW + timedelta(seconds=5)
        ).isoformat()
        assert await engine.poll_due(tenant, 1) == 1
        run = await _query_run(handle, str(tenant), run_id)
        assert run is not None and run["status"] == "completed"
    finally:
        await handle.dispose()


async def test_early_event_starts_default_timed_successor_timeout_from_now(
    db_url: str,
) -> None:
    """默认 timed successor 从实际事件 transition now 起算，不继承前一 deadline。"""
    clock = _Clock()
    definition = WorkflowDefinition(
        workflow_type="early-event-timed",
        version=1,
        steps=(
            StepDefinition("init", "h_init"),
            StepDefinition(
                "wait_one",
                "h_wait_one",
                timeout=timedelta(seconds=30),
                on_timeout="timeout_one",
                wait_event_type="approval_one",
            ),
            StepDefinition(
                "wait_two",
                "h_wait_two",
                timeout=timedelta(seconds=40),
                on_timeout="timeout_two",
                wait_event_type="approval_two",
            ),
            StepDefinition("timeout_one", "h_timeout"),
            StepDefinition("timeout_two", "h_timeout"),
        ),
        transitions={
            "init": ("wait_one",),
            "wait_one": ("wait_two", "timeout_one"),
            "wait_two": ("timeout_two",),
        },
    )
    engine, handle = _make_engine(
        db_url,
        {
            "h_init": _handler(lambda run: ("advance", "wait_one", {})),
            "h_wait_one": _handler(lambda run: ("advance", "wait_two", {})),
            "h_wait_two": _handler(lambda run: ("complete", None, {})),
            "h_timeout": _handler(lambda run: ("complete", None, {})),
        },
        clock=clock,
    )
    try:
        engine.register(definition)
        tenant = TenantId("tEarlyTimed")
        run_id = await engine.start(
            tenant, "early-event-timed", "subject", {}, "early-timed"
        )
        assert await engine.poll_due(tenant, 1) == 1
        clock.advance(seconds=5)
        assert await engine.deliver_event(
            tenant, run_id, "approval_one", {"approved": True}
        )
        steps = {
            row["step_name"]: row
            for row in await _query_steps(handle, str(tenant), run_id)
        }
        expected = _NOW + timedelta(seconds=45)
        assert steps["wait_two"]["due_at"] == expected
        assert steps["wait_two"]["data"]["planned_at"] == expected.isoformat()
    finally:
        await handle.dispose()


async def test_waiting_event_timeout_uses_transition_now_and_is_idempotent(
    db_url: str,
) -> None:
    """未到期不领取；到期转 on_timeout；重复 poll 不重复超时推进。"""
    calls: list[str] = []

    def _notify(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("advance", "wait", {})

    def _wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        return ("complete", None, {})

    def _escalate(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        calls.append(run.run_id)
        return ("complete", None, {})

    clock = _Clock(_NOW + timedelta(hours=1))  # 模拟事件延迟一小时才消费
    engine, handle = _make_engine(
        db_url,
        {
            "h_notify": _handler(_notify),
            "h_wait": _handler(_wait),
            "h_escalate": _handler(_escalate),
        },
        clock=clock,
    )
    try:
        engine.register(_timeout_flow())
        tenant = TenantId("tTimeout")
        rid = await engine.start(
            tenant,
            "timeout-wf",
            "hand-1",
            {},
            "timeout-key",
            scheduled_at=_NOW,
        )
        assert await engine.poll_due(tenant, 1) == 1  # notify → wait
        wait = {s["step_name"]: s for s in await _query_steps(handle, str(tenant), rid)}[
            "wait"
        ]
        assert wait["due_at"] == clock.now() + timedelta(seconds=10)

        assert await engine.poll_due(tenant, 1) == 0
        clock.advance(seconds=10)
        assert await engine.poll_due(tenant, 1) == 1
        row = await _query_run(handle, str(tenant), rid)
        assert row is not None and row["current_step"] == "escalate"
        steps = {s["step_name"]: s for s in await _query_steps(handle, str(tenant), rid)}
        assert steps["wait"]["status"] == "timed_out"
        assert len([s for s in steps.values() if s["step_name"] == "escalate"]) == 1

        assert await engine.poll_due(tenant, 1) == 1  # escalation handler completes
        assert await engine.poll_due(tenant, 10) == 0
        assert len(calls) == 1
    finally:
        await handle.dispose()


async def test_waiting_event_not_due_and_other_tenant_are_not_claimed(db_url: str) -> None:
    """未来 timeout 与其他租户 run 均不得被本租户提前领取。"""
    clock = _Clock()
    handlers = {
        "h_notify": _handler(lambda run: ("advance", "wait", {})),
        "h_wait": _handler(lambda run: ("complete", None, {})),
        "h_escalate": _handler(lambda run: ("complete", None, {})),
    }
    engine, handle = _make_engine(db_url, handlers, clock=clock)
    try:
        engine.register(_timeout_flow())
        tenant = TenantId("tTimeoutIsolation")
        rid = await engine.start(
            tenant, "timeout-wf", "hand-iso", {}, "timeout-iso", scheduled_at=_NOW
        )
        assert await engine.poll_due(tenant, 1) == 1
        assert await engine.poll_due(tenant, 10) == 0
        clock.advance(seconds=10)
        assert await engine.poll_due(TenantId("tOther"), 10) == 0
        row = await _query_run(handle, str(tenant), rid)
        assert row is not None and row["current_step"] == "wait"
        assert await engine.poll_due(tenant, 1) == 1
    finally:
        await handle.dispose()


async def test_waiting_event_without_timeout_never_enters_poll_loop(db_url: str) -> None:
    """普通 WAITING_EVENT 无 timeout 时，即使 due_at 很旧也不被 scheduler 领取。"""
    calls: list[str] = []
    definition = WorkflowDefinition(
        "plain-wait",
        1,
        (StepDefinition("wait", "h_wait", wait_event_type="accepted"),),
        {},
    )

    def _wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        calls.append(run.run_id)
        return ("complete", None, {})

    clock = _Clock()
    engine, handle = _make_engine(db_url, {"h_wait": _handler(_wait)}, clock=clock)
    try:
        engine.register(definition)
        tenant = TenantId("tPlainWait")
        await engine.start(tenant, "plain-wait", "hand", {}, "plain-wait-key")
        clock.advance(days=30)
        assert await engine.poll_due(tenant, 100) == 0
        assert await engine.poll_due(tenant, 100) == 0
        assert calls == []
    finally:
        await handle.dispose()


async def test_event_timeout_race_advances_exactly_once(db_url: str) -> None:
    """event 与 timeout 竞争只能一方推进，不能同时 complete 又建 escalation。"""
    wait_calls: list[str] = []

    def _wait(run: WorkflowRun) -> tuple[str, str | None, dict[str, object]]:
        wait_calls.append(run.run_id)
        return ("complete", None, {})

    handlers = {
        "h_notify": _handler(lambda run: ("advance", "wait", {})),
        "h_wait": _handler(_wait),
        "h_escalate": _handler(lambda run: ("complete", None, {})),
    }
    clock = _Clock()
    engine_a, handle_a = _make_engine(db_url, handlers, clock=clock)
    engine_b, handle_b = _make_engine(db_url, handlers, clock=clock)
    try:
        definition = _timeout_flow()
        engine_a.register(definition)
        engine_b.register(definition)
        tenant = TenantId("tTimeoutRace")
        rid = await engine_a.start(
            tenant, "timeout-wf", "hand-race", {}, "timeout-race", scheduled_at=_NOW
        )
        assert await engine_a.poll_due(tenant, 1) == 1
        clock.advance(seconds=10)
        await asyncio.gather(
            engine_a.poll_due(tenant, 1),
            engine_b.deliver_event(tenant, rid, "accepted", {"accepted_by": "sales"}),
        )
        row = await _query_run(handle_a, str(tenant), rid)
        assert row is not None
        steps = await _query_steps(handle_a, str(tenant), rid)
        escalation_steps = [s for s in steps if s["step_name"] == "escalate"]
        if row["status"] == "completed":
            assert len(wait_calls) == 1
            assert escalation_steps == []
        else:
            assert row["current_step"] == "escalate"
            assert wait_calls == []
            assert len(escalation_steps) == 1
    finally:
        await handle_a.dispose()
        await handle_b.dispose()


async def test_timeout_commit_failure_fails_observably_without_next_cycle_loop(
    db_url: str,
) -> None:
    """timeout transition 真实唯一约束失败后应 FAILED，且不阻塞同批正常 run。"""
    handlers = {
        "h_notify": _handler(lambda run: ("advance", "wait", {})),
        "h_wait": _handler(lambda run: ("complete", None, {})),
        "h_escalate": _handler(lambda run: ("complete", None, {})),
        "h": _handler(lambda run: ("complete", None, {})),
    }
    clock = _Clock()
    engine, handle = _make_engine(db_url, handlers, clock=clock)
    try:
        engine.register(_timeout_flow())
        engine.register(_simple_def("timeout-batch-good"))
        tenant = TenantId("tTimeoutCommitFailure")
        bad = await engine.start(
            tenant,
            "timeout-wf",
            "bad",
            {},
            "timeout-commit-bad",
            scheduled_at=_NOW,
        )
        assert await engine.poll_due(tenant, 1) == 1
        clock.advance(seconds=10)
        good = await engine.start(
            tenant,
            "timeout-batch-good",
            "good",
            {},
            "timeout-commit-good",
            scheduled_at=clock.now(),
        )
        async with handle.begin() as conn:
            await conn.execute(
                _INSERT_STEP,
                _step_params(
                    "stp-timeout-conflict",
                    str(bad),
                    str(tenant),
                    step_name="conflict",
                    # 步骤幂等键按实例唯一（含 planned_at）：注入同实例冲突行
                    key=(
                        f"{tenant}:wfstep:{bad}:escalate:"
                        f"{clock.now().isoformat()}"
                    ),
                    status="cancelled",
                ),
            )

        assert await engine.poll_due(tenant, 2) == 2
        bad_row = await _query_run(handle, str(tenant), bad)
        good_row = await _query_run(handle, str(tenant), good)
        assert bad_row is not None and bad_row["status"] == "failed"
        assert bad_row["last_error"] == "step wait failed: step commit failure"
        assert good_row is not None and good_row["status"] == "completed"
        assert await engine.poll_due(tenant, 10) == 0
    finally:
        await handle.dispose()
