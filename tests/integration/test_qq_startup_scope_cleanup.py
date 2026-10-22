"""v13 额外边界：真实原池、后置初始化、提前取消与已提交证据恢复。"""
import asyncio
import json
from time import monotonic

import asyncpg
import asyncpg.prepared_stmt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.scheduler_worker.main import run_scheduler_worker
from apps.scheduler_worker.qq_trial_startup import QQTrialStartup
from connectors._deadline_process import PreparationDeadline
from infra.db.qq_startup_scope import (
    QQStartupScope,
    QQStartupSession,
    configure_qq_engine,
)
from infra.db.qq_trial import SqlQQTrialGate
from infra.db.repositories.email_inbound import InboundStore
from infra.db.session import create_engine_from
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.test_qq_p1_standalone import CategorySDK, ReplyProvider, factory
from tests.integration.test_qq_p1_standalone import p1 as p1  # noqa: PLC0414
from tests.integration.test_qq_preparation_deadline import evidence_snapshot
from tests.integration.test_qq_tail_cleanup_diagnostic import TailDiagnostic, task_state
from tests.integration.test_standalone_reply_chain import (
    reply_storage as reply_storage,  # noqa: PLC0414
)


@pytest.mark.parametrize("stage", [
    "early_cancel_ack", "repeated_cancel_rollback", "post_gate", "bind_query", "committed_cursor",
])
async def test_post_initialization_and_cancellation(p1, request, stage, record_property):
    monkeypatch = pytest.MonkeyPatch()
    request.addfinalizer(monkeypatch.undo)
    mode = "query_cancel_ack" if stage == "early_cancel_ack" else (
        "rollback_wait" if stage == "repeated_cancel_rollback" else "control")
    probe = TailDiagnostic(p1, mode, monkeypatch)
    probe.path = probe.path.with_name("startup-" + stage + ".json")
    sdk, provider = CategorySDK("clear_interest"), ReplyProvider()
    entered = asyncio.Event()
    scopes = []
    init = QQStartupScope.__init__

    def initialized(scope, *args, **kwargs):
        init(scope, *args, **kwargs)
        scopes.append(scope)
    monkeypatch.setattr(QQStartupScope, "__init__", initialized)

    # 原 query 确实进入 asyncpg cancel handshake，早于 30 秒截止。
    if stage == "early_cancel_ack":
        fetch = asyncpg.prepared_stmt.PreparedStatement.fetch
        async def early_timeout(statement, *args, **kwargs):
            if statement._connection is probe.driver and "pg_sleep(60)" in statement.get_query():
                kwargs["timeout"] = .15
            return await fetch(statement, *args, **kwargs)
        monkeypatch.setattr(asyncpg.prepared_stmt.PreparedStatement, "fetch", early_timeout)

    async def capture(session):
        raw = await (await session.connection()).get_raw_connection()
        probe.driver = raw.driver_connection
        probe.transport = probe.driver._transport
        probe.backend_pid = probe.driver.get_server_pid()
        assert session.bind is probe.worker.lock_engine
        probe.injected = True
        probe.mark("post_initialization_wait", stage_under_test=stage, backend_pid=probe.backend_pid)
        entered.set()
        await session.execute(text("SELECT pg_sleep(60)"))

    row = SqlQQTrialGate._row
    async def post_row(gate, session, **kwargs):
        result = await row(gate, session, **kwargs)
        if stage == "post_gate" and probe.bound and not probe.inside_startup:
            await capture(session)
        return result
    monkeypatch.setattr(SqlQQTrialGate, "_row", post_row)

    store_row = InboundStore.row
    async def bind_row(store, session, **kwargs):
        result = await store_row(store, session, **kwargs)
        if stage == "bind_query" and kwargs.get("lock"):
            await capture(session)
        return result
    monkeypatch.setattr(InboundStore, "row", bind_row)

    bind = InboundStore.bind
    async def committed(store, *args, **kwargs):
        result = await bind(store, *args, **kwargs)
        if stage == "committed_cursor":
            # 真实提交已完成，在后续启动动作阻塞；不得撤销/重置游标。
            probe.before = await evidence_snapshot(p1)
            assert len(probe.before["email_inbound_cursors"]) == 1
            probe.injected = True
            probe.mark("cursor_committed_before_wait")
            entered.set()
            await asyncio.sleep(60)
        return result
    monkeypatch.setattr(InboundStore, "bind", committed)

    cancelled_at = None
    async with p1["sessions"].kw["bind"].connect() as blocker:
        await blocker.execute(text("SELECT pg_advisory_lock(:key)"), {"key": probe.blocker_key})
        await blocker.commit()
        task = asyncio.create_task(probe.run(sdk, provider), name="v13-original-entry")
        try:
            async with asyncio.timeout(45):
                while not probe.injected:
                    assert not task.done(), "startup did not reach injected stage"
                    await asyncio.sleep(.01)
            if stage == "repeated_cancel_rollback":
                cancelled_at = monotonic()
                task.cancel()
                asyncio.get_running_loop().call_later(.005, task.cancel)
                asyncio.get_running_loop().call_later(.01, task.cancel)
            cutoff = min(probe.deadline.at, cancelled_at or probe.deadline.at) + 2
            await asyncio.sleep(max(0, cutoff - monotonic()))
            probe.boundary = await probe.observe(task, sdk)
            violations = probe.boundary_violations()
            if probe.context_exit is None or probe.context_exit > cutoff:
                violations.append("early_cancel_cleanup_exceeded_two_seconds")
            pending = [task_state(t) for scope in scopes for t in scope.tasks if not t.done()]
            if pending:
                violations.append("scope_owned_tasks_pending")
            result = probe.save(production_source_unchanged=False, stage_under_test=stage,
                status="boundary_met" if not violations else "boundary_failed_before_rescue",
                violations=violations, cancelled_at=cancelled_at, cutoff=cutoff,
                owned_pending=pending, scope_cleanup_confirmed=[s.cleanup_confirmed for s in scopes])
            probe.path.with_suffix(".boundary.json").write_text(json.dumps(result, indent=2))
        finally:
            # 取证之后才解锁；不为被测代码额外 cancel。
            probe.cancel_release.set()
            assert await blocker.scalar(text("SELECT pg_advisory_unlock(:key)"), {"key": probe.blocker_key})
            await blocker.commit()
            await asyncio.wait({task}, timeout=5)
            if task.done() and not task.cancelled():
                task.exception()
        assert not violations
        assert task.done() and all(s.cleanup_confirmed for s in scopes)
        assert probe.before == await evidence_snapshot(p1)
        assert not probe.cycle_times and not sdk.requests
        assert all(c.is_closed() for c in probe.runtime_connections)
        assert all(c.is_closed() for d in probe.preparation for c in d.connections)
        if stage == "early_cancel_ack":
            held = [e for e in probe.events if e["stage"] == "target_cancel_handshake_held"]
            assert held and held[0]["monotonic"] < probe.deadline.at - 5
            assert all(t.done() for t in probe.cancel_tasks)
        if cancelled_at is not None:
            assert task.cancelled() and probe.context_exit - cancelled_at <= 2
        record_property("v13_boundary", json.dumps({"stage": stage, "violations": violations,
            "context_seconds": probe.context_exit-probe.entry_start, "cutoff": cutoff,
            "cancelled_at": cancelled_at, "owned_tasks_done": True, "evidence_retained": True}))

    if stage == "committed_cursor":
        # 移除注入后从同一耐久证据恢复，仍经过原 scheduler singleton 入口。
        before = await evidence_snapshot(p1)
        commands = list(p1["mailbox"]["commands"])
        monkeypatch.undo()
        stop = asyncio.Event()
        activate = QQTrialStartup.activate
        async def stop_after_activation(startup):
            await activate(startup)
            stop.set()
        monkeypatch.setattr(QQTrialStartup, "activate", stop_after_activation)
        async with factory(p1, sdk, provider)() as worker:
            result = await run_scheduler_worker(worker, stop_event=stop, install_signal_handlers=False)
            assert result.cycles_completed == 0
        assert before == await evidence_snapshot(p1)
        assert commands == p1["mailbox"]["commands"]
        record_property("restart", "same_key_same_cursor_no_sent_recollection_no_cycle")


async def test_scoped_abort_preserves_other_borrowers_and_pool_reuse(p1, record_property):
    engine = create_engine_from(p1["sessions"].kw["bind"].url.render_as_string(hide_password=False))
    configure_qq_engine(engine)
    sessions = async_sessionmaker(engine, class_=QQStartupSession, expire_on_commit=False)
    scope = QQStartupScope(engine, PreparationDeadline(.5))
    started = monotonic()
    target = []
    try:
        async with engine.connect() as scheduler, engine.connect() as unrelated:
            scheduler_pid = await scheduler.scalar(text("SELECT pg_backend_pid()"))
            unrelated_pid = await unrelated.scalar(text("SELECT pg_backend_pid()"))
            await scheduler.commit()
            await unrelated.commit()
            async def work():
                async with sessions.begin() as session:
                    target.append(await session.scalar(text("SELECT pg_backend_pid()")))
                    await session.execute(text("SELECT pg_sleep(60)"))
            with pytest.raises(QQTrialStopped):
                await scope.run(work)
            assert scope.cleanup_confirmed and monotonic()-started <= 2.5
            assert await scheduler.scalar(text("SELECT pg_backend_pid()")) == scheduler_pid
            assert await unrelated.scalar(text("SELECT pg_backend_pid()")) == unrelated_pid
            async with sessions() as fresh:
                assert await fresh.scalar(text("SELECT 1")) == 1
                assert not await fresh.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"),
                                              {"pid": target[0]})
            assert all(t.done() for t in scope.tasks)
        record_property("connection_ownership", json.dumps({"other_borrowers_survived": 2,
            "pool_reusable": True, "target_backend_gone": True, "elapsed": monotonic()-started}))
    finally:
        await engine.dispose()
