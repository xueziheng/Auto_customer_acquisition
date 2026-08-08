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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import TextClause, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import TenantId
from workflows.engine.runner import StepDefinition, WorkflowDefinition, WorkflowRun

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
        ["alembic", *command],
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

        await engine.deliver_event(TenantId(tenant), rid, "approval", {"by": "bob"})
        run_row = await _query_run(handle, tenant, rid)
        assert run_row is not None
        assert run_row["current_step"] == "done"
        assert run_row["context"]["approved"] == "bob"
        assert len(wait_calls) == 1

        await engine.deliver_event(TenantId(tenant), rid, "approval", {"by": "bob"})
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

        await engine.deliver_event(TenantId("tOther"), rid, "approval", {"by": "bob"})
        await engine.deliver_event(TenantId(tenant), "run-unknown", "approval", {"by": "bob"})
        await engine.deliver_event(TenantId(tenant), rid, "wrong_type", {"by": "bob"})
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
