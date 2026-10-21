"""冻结 v11 的尾部故障诊断；失败原样保留，watchdog 先取证再解阻。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

import asyncpg
import asyncpg.connect_utils
import pytest
from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_connection
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.scheduler_worker import main as scheduler_main
from apps.scheduler_worker.main import run_scheduler_worker
from apps.scheduler_worker.qq_trial_startup import QQTrialStartup
from connectors._deadline_process import CLEANUP_GRACE_SECONDS
from infra.db.qq_preparation import QQPreparationDatabase
from infra.db.qq_trial import SqlQQTrialGate
from tests.integration.test_qq_p1_standalone import CategorySDK, ReplyProvider, factory
from tests.integration.test_qq_p1_standalone import p1 as p1  # noqa: PLC0414
from tests.integration.test_qq_preparation_deadline import evidence_snapshot
from tests.integration.test_standalone_reply_chain import (
    reply_storage as reply_storage,  # noqa: PLC0414
)


class RollbackDiagnosticSignal(Exception):
    """仅使最后一次 gate 的真实事务退出选择 rollback；不替换退出实现。"""


def task_state(task):
    if task is None:
        return None
    return {
        "done": task.done(), "cancelled": task.cancelled(), "cancelling": task.cancelling(),
        "coroutine": getattr(task.get_coro(), "__qualname__", type(task.get_coro()).__name__),
        "stack": [{"file": Path(f.f_code.co_filename).name, "function": f.f_code.co_name,
                   "line": f.f_lineno} for f in task.get_stack(limit=6)],
    }


def snapshot_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class TailDiagnostic:
    def __init__(self, p1, mode, monkeypatch):
        self.p1, self.mode = p1, mode
        self.budget = p1["model"].qq_reply.trial.sent.timeout_seconds
        assert self.budget == 30 and CLEANUP_GRACE_SECONDS == 2
        self.events = []
        self.started = monotonic()
        self.startup = self.worker = self.deadline = self.driver = self.transport = None
        self.backend_pid = None
        self.preparation = []
        self.cleanup_tasks = []
        self.cancel_tasks = []
        self.runtime_connections = []
        self.runtime_pids = {}
        self.preparation_close = []
        self.disposals = []
        self.bound = self.in_final_gate = self.row_completed = False
        self.inside_startup = False
        self.injected = False
        self.entry_start = self.entry_exit = self.context_exit = None
        self.exception = None
        self.cycle_times = []
        self.stop = asyncio.Event()
        self.deadline_ready = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.blocker_key = int(uuid4().hex[:15], 16)
        self.before = None
        self.boundary = None
        self.rescue_actions = []
        self.path = Path(os.environ["QQ_TEST_EVIDENCE_DIR"])/f"tail-{mode}.json"
        self.install(monkeypatch)

    def mark(self, name, **data):
        self.events.append({"stage": name, "since_probe_start": monotonic()-self.started,
                            "monotonic": monotonic(), **data})

    def install(self, patch):
        prepare = QQTrialStartup._prepare
        async def prepared(startup, sessions, deadline):
            self.startup, self.deadline = startup, deadline
            self.deadline_ready.set()
            self.mark("prepare_enter", deadline_at=deadline.at, budget=self.budget)
            result = await prepare(startup, sessions, deadline)
            self.mark("original_prepare_returned")
            self.before = await evidence_snapshot(self.p1)
            self.mark("committed_preparation_observed", counts={k:len(v) for k,v in self.before.items()})
            return result
        patch.setattr(QQTrialStartup, "_prepare", prepared)

        bind = SqlQQTrialGate.bind_scope
        def bound(gate, scope):
            bind(gate, scope)
            if self.startup is not None and gate is self.startup.gate:
                self.bound = True
                self.mark("bind_scope_returned")
        patch.setattr(SqlQQTrialGate, "bind_scope", bound)

        check = SqlQQTrialGate.check
        async def checked(gate):
            own = self.startup is not None and gate is self.startup.gate and self.bound and self.inside_startup
            if own:
                self.in_final_gate = True
                self.mark("original_pool_final_gate_entered",
                    original_pool=gate.factory.kw["bind"] is self.worker.lock_engine)
                assert gate.factory.kw["bind"] is self.worker.lock_engine
            try:
                return await check(gate)
            finally:
                if own:
                    self.mark("original_pool_final_gate_exited")
                    self.in_final_gate = False
        patch.setattr(SqlQQTrialGate, "check", checked)

        row = SqlQQTrialGate._row
        async def row_checked(gate, session, **kwargs):
            result = await row(gate, session, **kwargs)
            if self.in_final_gate and gate is self.startup.gate:
                connection = await session.connection()
                raw = await connection.get_raw_connection()
                self.driver = raw.driver_connection
                self.transport = self.driver._transport
                self.backend_pid = self.driver.get_server_pid()
                assert session.bind is self.worker.lock_engine
                assert all(self.driver not in d.connections for d in self.preparation)
                self.row_completed = True
                self.mark("original_gate_row_checks_complete", backend_pid=self.backend_pid,
                          preparation_pool=False, remaining=self.deadline.at-monotonic())
                if self.mode == "query_cancel_ack":
                    self.injected = True
                    self.mark("real_original_pool_query_started")
                    await session.execute(text("SELECT pg_sleep(60)"))
                elif self.mode == "rollback_wait":
                    self.mark("rollback_selected_by_test_exception")
                    raise RollbackDiagnosticSignal()
            return result
        patch.setattr(SqlQQTrialGate, "_row", row_checked)

        cancel = asyncpg.connect_utils._cancel
        async def held_cancel(*, loop, addr, params, backend_pid, backend_secret):
            if self.mode == "query_cancel_ack" and backend_pid == self.backend_pid:
                self.cancel_tasks.append(asyncio.current_task())
                self.mark("target_cancel_handshake_held", backend_pid=backend_pid)
                await self.cancel_release.wait()
                self.mark("target_cancel_handshake_released")
            return await cancel(loop=loop,addr=addr,params=params,backend_pid=backend_pid,
                                backend_secret=backend_secret)
        patch.setattr(asyncpg.connect_utils, "_cancel", held_cancel)

        execute = asyncpg.Connection.execute
        async def execute_observed(driver, query, *args, **kwargs):
            if self.mode == "rollback_wait" and driver is self.driver and query.strip().upper() == "ROLLBACK;":
                self.injected = True
                self.cleanup_tasks.append(asyncio.current_task())
                self.mark("real_rollback_path_advisory_wait_started", backend_pid=self.backend_pid)
                # 真实 asyncpg/PG 等待置于真实 rollback 调用内；不替换 SQLAlchemy 的 shield/退出。
                await execute(driver, "SELECT pg_advisory_xact_lock($1)", self.blocker_key)
                self.mark("real_rollback_path_advisory_wait_released")
            return await execute(driver, query, *args, **kwargs)
        patch.setattr(asyncpg.Connection, "execute", execute_observed)

        graceful = AsyncAdapt_asyncpg_connection._terminate_graceful_close
        async def graceful_observed(adapter):
            own = adapter._connection is self.driver
            if own:
                self.cleanup_tasks.append(asyncio.current_task())
                self.mark("sqlalchemy_original_graceful_close_entered")
            try:
                return await graceful(adapter)
            finally:
                if own:
                    self.mark("sqlalchemy_original_graceful_close_exited")
        patch.setattr(AsyncAdapt_asyncpg_connection, "_terminate_graceful_close", graceful_observed)

        init = QQPreparationDatabase.__init__
        def initialized(database, *args, **kwargs):
            init(database, *args, **kwargs)
            self.preparation.append(database)
        patch.setattr(QQPreparationDatabase, "__init__", initialized)
        close = QQPreparationDatabase.close
        async def close_observed(database):
            started = monotonic()
            self.mark("preparation_database_close_entered")
            try:
                return await close(database)
            finally:
                item={"seconds":monotonic()-started,"deadline_delta":started-database.deadline.at,
                      "tracked_connections_closed":all(c.is_closed() for c in database.connections)}
                self.preparation_close.append(item)
                self.mark("preparation_database_close_exited", **item)
        patch.setattr(QQPreparationDatabase, "close", close_observed)

        dispose = AsyncEngine.dispose
        async def dispose_observed(engine, *args, **kwargs):
            kind = "preparation" if any(engine is d.engine for d in self.preparation) else (
                "original_runtime" if self.worker is not None and engine is self.worker.lock_engine else None)
            started = monotonic()
            if kind:
                self.mark("engine_dispose_entered", kind=kind, checked_out=engine.pool.checkedout())
            try:
                return await dispose(engine,*args,**kwargs)
            finally:
                if kind:
                    item={"kind":kind,"seconds":monotonic()-started}
                    self.disposals.append(item)
                    self.mark("engine_dispose_exited",**item)
        patch.setattr(AsyncEngine, "dispose", dispose_observed)

        activate = QQTrialStartup.activate
        async def activated(startup):
            self.inside_startup=True
            try:
                await activate(startup)
            finally:
                self.inside_startup=False
            self.mark("activate_returned_successfully")
            if self.mode == "control":
                self.stop.set()  # 对照组在激活成功后结束，不启动业务 cycle。
        patch.setattr(QQTrialStartup, "activate", activated)
        cycle = scheduler_main._run_cycle
        async def cycle_observed(*args,**kwargs):
            self.cycle_times.append(monotonic())
            return await cycle(*args,**kwargs)
        patch.setattr(scheduler_main, "_run_cycle", cycle_observed)

    async def run(self, sdk, provider):
        try:
            async with factory(self.p1,sdk,provider)() as worker:
                self.worker = worker
                @event.listens_for(worker.lock_engine.sync_engine,"checkout")
                def checkout(connection, record, proxy):
                    driver=connection.driver_connection
                    if driver not in self.runtime_connections:
                        self.runtime_connections.append(driver)
                        self.runtime_pids[id(driver)]=driver.get_server_pid()
                self.entry_start=monotonic()
                self.mark("original_scheduler_entry")
                try:
                    return await run_scheduler_worker(worker,stop_event=self.stop,install_signal_handlers=False)
                finally:
                    self.entry_exit=monotonic()
                    self.mark("original_scheduler_exit")
        except BaseException as exc:
            self.exception=type(exc).__name__
            raise
        finally:
            self.context_exit=monotonic()
            self.mark("original_factory_context_exit")

    async def observe(self, task, sdk):
        stamp=monotonic()
        result={"observed_at":stamp,"deadline_at":self.deadline.at,
                "cleanup_cutoff":self.deadline.at+CLEANUP_GRACE_SECONDS,
                "scheduler_task":task_state(task),"entry_exit_at":self.entry_exit,
                "context_exit_at":self.context_exit,
                "cleanup_tasks":[task_state(t) for t in set(self.cleanup_tasks)],
                "cancel_tasks":[task_state(t) for t in set(self.cancel_tasks)],
                "driver_is_closed":self.driver.is_closed() if self.driver else None,
                "driver_transport_closing":self.transport.is_closing() if self.transport else None,
                "runtime_driver_states":[{"pid":self.runtime_pids[id(c)],"is_closed":c.is_closed()}
                                         for c in self.runtime_connections],
                "cycle_count":len(self.cycle_times),"model_calls":len(sdk.requests),
                "io_children_reaped":all(p.poll() is not None for p in self.p1["mailbox"]["processes"])}
        async with self.p1["sessions"]() as db:
            backend=await db.execute(text("SELECT pid,state,wait_event_type,wait_event FROM pg_stat_activity WHERE pid=:pid"),
                                     {"pid":self.backend_pid})
            result["target_backend"]=[dict(row) for row in backend.mappings()]
            acquired=await db.scalar(text("SELECT pg_try_advisory_lock(:key)"),{"key":self.worker.config.lock_key})
            if acquired:
                assert await db.scalar(text("SELECT pg_advisory_unlock(:key)"),{"key":self.worker.config.lock_key})
            await db.commit()
            result["scheduler_lock_reacquired"]=bool(acquired)
        return result

    def save(self, **extra):
        result={"mode":self.mode,"budget":self.budget,"cleanup_grace":CLEANUP_GRACE_SECONDS,
                "production_source_unchanged":True,"watchdog_task_cancel_calls":0,
                "bound":self.bound,"row_completed":self.row_completed,"injection_reached":self.injected,
                "entry_start":self.entry_start,"entry_exit":self.entry_exit,"context_exit":self.context_exit,
                "entry_seconds":self.entry_exit-self.entry_start if self.entry_exit and self.entry_start else None,
                "entry_and_context_seconds":self.context_exit-self.entry_start if self.context_exit and self.entry_start else None,
                "exception_type":self.exception,"boundary":self.boundary,"rescue_actions":self.rescue_actions,
                "events":self.events,"preparation_database_close":self.preparation_close,
                "engine_disposals":self.disposals,**extra}
        self.path.write_text(json.dumps(result,ensure_ascii=False,indent=2))
        return result

    def boundary_violations(self):
        boundary=self.boundary
        cutoff=self.deadline.at+CLEANUP_GRACE_SECONDS
        violations=[]
        if boundary["entry_exit_at"] is None or boundary["entry_exit_at"]>cutoff:
            violations.append("scheduler_entry_exceeded_absolute_deadline_plus_grace")
        if boundary["context_exit_at"] is None or boundary["context_exit_at"]>cutoff:
            violations.append("factory_context_exceeded_absolute_deadline_plus_grace")
        if any(not t["done"] for t in boundary["cleanup_tasks"]+boundary["cancel_tasks"]):
            violations.append("owned_cleanup_or_cancel_task_pending_at_boundary")
        if boundary["target_backend"] or boundary["driver_transport_closing"] is False:
            violations.append("target_original_pool_connection_present_at_boundary")
        if not boundary["scheduler_lock_reacquired"]:
            violations.append("scheduler_lock_held_at_boundary")
        return violations


@pytest.mark.parametrize("mode",["control","query_cancel_ack","rollback_wait"])
async def test_original_final_gate_cleanup_bound(p1,monkeypatch,mode,record_property):
    probe=TailDiagnostic(p1,mode,monkeypatch)
    sdk,provider=CategorySDK("clear_interest"),ReplyProvider()
    task=None
    async with p1["sessions"].kw["bind"].connect() as blocker:
        await blocker.execute(text("SELECT pg_advisory_lock(:key)"),{"key":probe.blocker_key})
        await blocker.commit()
        task=asyncio.create_task(probe.run(sdk,provider),name="diagnostic-original-entry")

        async def watchdog():
            setup_limit=monotonic()+45
            while not probe.deadline_ready.is_set():
                if task.done() or monotonic()>setup_limit:
                    raise AssertionError("original activation was not reached")
                await asyncio.sleep(.02)
            await asyncio.sleep(max(0,probe.deadline.at+2-monotonic()))
            probe.boundary=await probe.observe(task,sdk)
            probe.mark("watchdog_boundary_recorded_before_unblock")
            violations=probe.boundary_violations()
            boundary_record=probe.save(status="boundary_failed_before_any_rescue" if violations else "boundary_met",
                                      violations=violations)
            probe.path.with_suffix(".boundary.json").write_text(json.dumps(boundary_record,indent=2))
            if mode!="control":
                probe.rescue_actions.append("release_injected_waits_after_boundary_record")
                probe.cancel_release.set()
            assert await blocker.scalar(text("SELECT pg_advisory_unlock(:key)"),{"key":probe.blocker_key})
            await blocker.commit()
            probe.mark("watchdog_injection_released")

        guard=asyncio.create_task(watchdog(),name="diagnostic-independent-watchdog")
        try:
            await guard
            await asyncio.wait({task},timeout=10)
            pending={t for t in probe.cleanup_tasks+probe.cancel_tasks if not t.done()}
            if pending:
                await asyncio.wait(pending,timeout=5)
            if not task.done() or any(not t.done() for t in probe.cleanup_tasks+probe.cancel_tasks):
                probe.rescue_actions.append("abort_exact_target_transport_after_failed_boundary")
                probe.save(status="watchdog_transport_abort_required")
                if probe.transport is not None:
                    probe.transport.abort()
                await asyncio.wait({task,*probe.cleanup_tasks,*probe.cancel_tasks},timeout=5)
            assert task.done(), "watchdog could not finish original entry"
            if not task.cancelled():
                task.exception()  # 收集原异常；不将被测任务的失败当成测试支撑异常。
            await asyncio.sleep(.1)
            after_release=await probe.observe(task,sdk)
            if after_release["target_backend"] or (probe.driver and not probe.driver.is_closed()):
                probe.rescue_actions.append("abort_exact_target_transport_after_unblock_left_connection")
                probe.save(status="boundary_failed_residual_connection_requires_rescue",after_injection_release=after_release)
                probe.transport.abort()
                await asyncio.sleep(.2)
            final=await probe.observe(task,sdk)
            after=await evidence_snapshot(p1)
            unchanged=probe.before==after
            changed_sections=[k for k in probe.before if probe.before[k]!=after[k]]
            protected_before={k:v for k,v in probe.before.items() if k!="email_inbound_cursors"}
            protected_after={k:v for k,v in after.items() if k!="email_inbound_cursors"}
            protected_unchanged=protected_before==protected_after
            violations=probe.boundary_violations()
            result=probe.save(status="diagnostic_complete",violations=violations,final=final,
                after_injection_release=after_release,
                evidence_unchanged=unchanged,prepared_evidence_sha256=snapshot_digest(probe.before),
                final_evidence_sha256=snapshot_digest(after),
                protected_evidence_unchanged=protected_unchanged,changed_evidence_sections=changed_sections,
                protected_prepared_sha256=snapshot_digest(protected_before),
                protected_final_sha256=snapshot_digest(protected_after),
                prepared_counts={k:len(v) for k,v in probe.before.items()},
                final_counts={k:len(v) for k,v in after.items()},
                watchdog_classification="no_intervention" if mode=="control" else (
                    "boundary_failed_then_injection_released" if violations else "boundary_met_before_injection_release"))
            record_property("tail_diagnostic",json.dumps({k:result[k] for k in (
                "mode","budget","cleanup_grace","entry_seconds","entry_and_context_seconds",
                "violations","watchdog_classification","evidence_unchanged","protected_evidence_unchanged")}))
            assert probe.bound and probe.row_completed
            assert probe.before["qq_reply_trials"] and probe.before["qq_sent_aliases"] and probe.before["raw_artifacts"]
            assert mode=="control" or probe.injected
            if mode=="control":
                # 原 _InboundRuntimeActivation 在 QQStartup 成功后绑定游标；不是故障时改写证据。
                assert changed_sections==["email_inbound_cursors"]
                assert len(probe.before["email_inbound_cursors"])==0 and len(after["email_inbound_cursors"])==1
            else:
                assert unchanged
                assert not any(e["stage"]=="activate_returned_successfully" for e in probe.events)
            assert protected_unchanged and not probe.cycle_times and not sdk.requests
            assert final["scheduler_lock_reacquired"] and final["io_children_reaped"]
            assert not final["target_backend"] and all(t["done"] for t in final["cleanup_tasks"]+final["cancel_tasks"])
            assert all(c.is_closed() for c in probe.runtime_connections)
            assert all(c.is_closed() for d in probe.preparation for c in d.connections)
            assert not violations, "v11 tail cleanup bound failed: "+", ".join(violations)
        finally:
            probe.cancel_release.set()
            if not guard.done():
                # 支撑异常也仅解阻，不追加被测 task.cancel；独立 watchdog 仍保留原计时。
                await guard
            if task.done() and not task.cancelled():
                task.exception()
