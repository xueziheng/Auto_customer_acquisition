"""同一原入口的 F1 及恢复验证；真实本机受限 PG/MinIO，合成邮箱/模型。"""
import asyncio
import json
import sys
from datetime import timedelta
from time import monotonic

import pytest
from sqlalchemy import func, select, text, update

from apps.scheduler_worker import main as scheduler_main
from apps.scheduler_worker.main import run_scheduler_worker
from connectors._deadline_process import CLEANUP_GRACE_SECONDS, DeadlineProcess
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.qq_preparation import QQPreparationObjects
from infra.db.qq_preparation import QQPreparationDatabase
from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialEventRow, QQTrialRow
from infra.db.repositories.artifacts import RawArtifactRepositoryImpl
from infra.db.tables import (
    EmailInboundCursorRow,
    RawArtifactRow,
    ToolCallEventRow,
    ToolCallRow,
)
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.qq_trial import QQTrialStopped
from tests.integration.test_qq_p1_standalone import (
    CategorySDK,
    ReplyProvider,
    factory,
    rows,
    run_once,
)
from tests.integration.test_qq_p1_standalone import p1 as p1  # noqa: PLC0414
from tests.integration.test_standalone_reply_chain import (
    reply_storage as reply_storage,  # noqa: PLC0414
)


def narrow_timeout(p1,seconds):
    settings=p1["model"]
    qq=settings.qq_reply
    launch=qq.trial
    p1["model"]=settings.model_copy(update={"qq_reply":qq.model_copy(update={
        "trial":launch.model_copy(update={"sent":launch.sent.model_copy(update={"timeout_seconds":seconds})})})})


async def assert_lock_released(worker):
    async with worker.lock_engine.connect() as connection:
        assert await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"),{"key":worker.config.lock_key})
        assert await connection.scalar(text("SELECT pg_advisory_unlock(:key)"),{"key":worker.config.lock_key})
        await connection.commit()


def watch_cycles(monkeypatch):
    cycles=[]
    original=scheduler_main._run_cycle
    async def cycle(*args,**kwargs):
        cycles.append("cycle")
        return await original(*args,**kwargs)
    monkeypatch.setattr(scheduler_main,"_run_cycle",cycle)
    return cycles


@pytest.mark.parametrize("stage",["line","literal","object_put","object_put_ack","object_get","object_close","metadata"])
async def test_full_entry_deadline_no_cycle_no_late_success(p1,monkeypatch,tmp_path,stage,record_property):
    narrow_timeout(p1,2)
    marker=tmp_path/"blocked-stage"
    original=DeadlineProcess._command
    database_pids=[]
    databases=[]
    original_database=QQPreparationDatabase.__init__
    def observe_database(self,*args,**kwargs):
        original_database(self,*args,**kwargs)
        databases.append(self)
    monkeypatch.setattr(QQPreparationDatabase,"__init__",observe_database)
    put_keys=[]
    original_object_call=QQPreparationObjects._call
    async def observed_object_call(self,operation,key,*args):
        if operation=="put":
            put_keys.append(key)
        return await original_object_call(self,operation,key,*args)
    monkeypatch.setattr(QQPreparationObjects,"_call",observed_object_call)
    if stage.startswith("object_"):
        def command(self,fd):
            if self.kind=="objects":
                return [sys.executable,"-m","tests.unit.qq_deadline_child",str(fd),
                    str(self.deadline.at),self.kind,"0",stage,str(marker)]
            return original(self,fd)
        monkeypatch.setattr(DeadlineProcess,"_command",command)
    elif stage=="metadata":
        original_hash=RawArtifactRepositoryImpl.get_by_hash
        async def delayed_hash(self,*args,**kwargs):
            database_pids.append(await self._session.scalar(text("SELECT pg_backend_pid()")))
            await self._session.execute(text("SELECT pg_sleep(60)"))
            return await original_hash(self,*args,**kwargs)
        monkeypatch.setattr(RawArtifactRepositoryImpl,"get_by_hash",delayed_hash)
    else:
        p1["mailbox"]["slow"]=stage
    sdk,probe=CategorySDK("clear_interest"),ReplyProvider()
    cycles=watch_cycles(monkeypatch)
    started=0.0
    async with factory(p1,sdk,probe)() as worker:
        started=monotonic()
        with pytest.raises(QQTrialStopped):
            await run_scheduler_worker(worker,install_signal_handlers=False)
        elapsed=monotonic()-started
        assert elapsed <= 2+CLEANUP_GRACE_SECONDS
        await assert_lock_released(worker)
    elapsed=monotonic()-started
    assert elapsed <= 2+CLEANUP_GRACE_SECONDS
    assert not cycles and not sdk.requests
    assert await rows(p1,QQTrialRow)==[] and await rows(p1,QQSentAliasRow)==[]
    assert all(p.poll() is not None for p in p1["mailbox"]["processes"])
    assert all(connection.is_closed() for database in databases for connection in database.connections)
    if stage.startswith("object_"):
        assert marker.exists(), {"elapsed":elapsed,"commands":len(p1["mailbox"]["commands"]),
            "process_exit_codes":[p.returncode for p in p1["mailbox"]["processes"]]}
        assert marker.read_text()==stage
    if stage=="object_put_ack":
        assert len(put_keys)==1
        settings=S3ObjectStoreSettings.from_pilot_environ(p1["profile"].runtime_environment())
        verifier=S3BoundedObjectBlobTransport(settings,p1["profile"],limits=ObjectReadLimits(
            connect_timeout_ms=1000,read_timeout_ms=1000,total_timeout_ms=2000,maximum_attempts=1,chunk_bytes=16384))
        assert await verifier.get_bounded(put_keys[0],maximum_bytes=16384)==p1["sent"]
        assert await rows(p1,RawArtifactRow)==[]
    calls=[c for c in await rows(p1,ToolCallRow) if c.tool_id=="email.qq.sent_alias.read"]
    assert len(calls)==1 and calls[0].attempt_count==1
    assert calls[0].status in {"executing","failed_permanent","succeeded"}
    # SDK 关闭阻塞可能发生于成功原件复核之后；不得抹去已成功的 Gateway 证据。
    if stage!="object_close":
        assert calls[0].status!="succeeded"
    events=await rows(p1,ToolCallEventRow)
    assert any(e.tool_call_id==calls[0].tool_call_id and e.outcome=="executing" for e in events)
    before=list(p1["mailbox"]["commands"])
    await asyncio.sleep(.1)
    assert not marker.with_suffix(".late").exists()
    assert await rows(p1,QQTrialRow)==[]
    if stage=="metadata":
        assert database_pids
        async with p1["sessions"]() as db:
            for pid in database_pids:
                assert not await db.scalar(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=:pid)"),{"pid":pid})
        for database in databases:
            count=len(database.connections)
            with pytest.raises(TimeoutError):
                async with database.factory() as db:
                    await db.execute(text("SELECT 1"))
            assert len(database.connections)==count
    # 保留同一故障/配置重启；Sent 只允许复用持久成功证据或拒绝，不能重采。
    with pytest.raises(QQTrialStopped):
        async with factory(p1,sdk,probe)() as worker:
            await run_once(worker)
    assert p1["mailbox"]["commands"]==before
    if stage=="line":
        # 不改 lease 或 DB 时钟，覆盖原 EXECUTING 租约真实到期后的再次启动。
        async with p1["sessions"]() as db:
            stamp=await db.scalar(select(func.clock_timestamp()))
        wait=(calls[0].lease_expires_at-stamp).total_seconds()
        if wait>0:
            await asyncio.sleep(wait+.05)
        with pytest.raises(QQTrialStopped):
            async with factory(p1,sdk,probe)() as worker:
                await run_once(worker)
        assert p1["mailbox"]["commands"]==before
        assert await rows(p1,QQTrialRow)==[]
        record_property("expired_lease_restart","original_key_no_sent_recollection")
    assert all(p.poll() is not None for p in p1["mailbox"]["processes"])
    record_property("full_entry_deadline",json.dumps({"stage":stage,"budget":2,
        "elapsed":elapsed,"cleanup_grace":CLEANUP_GRACE_SECONDS,"cycles":0,
        "lock_reacquired":True,"children_reaped":True,"restart_sent_recollections":0}))


async def test_full_entry_repeated_cancellation_releases_original_lock(p1,monkeypatch,tmp_path,record_property):
    narrow_timeout(p1,5)
    marker=tmp_path/"cancel-stage"
    original=DeadlineProcess._command
    def command(self,fd):
        if self.kind=="objects":
            return [sys.executable,"-m","tests.unit.qq_deadline_child",str(fd),
                str(self.deadline.at),self.kind,"0","object_put",str(marker)]
        return original(self,fd)
    monkeypatch.setattr(DeadlineProcess,"_command",command)
    cycles=watch_cycles(monkeypatch)
    sdk,probe=CategorySDK("clear_interest"),ReplyProvider()
    async with factory(p1,sdk,probe)() as worker:
        task=asyncio.create_task(run_scheduler_worker(worker,install_signal_handlers=False))
        async with asyncio.timeout(4):
            while not marker.exists():
                await asyncio.sleep(.01)
        started=monotonic()
        task.cancel()
        asyncio.get_running_loop().call_soon(task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed=monotonic()-started
        assert elapsed <= CLEANUP_GRACE_SECONDS
        await assert_lock_released(worker)
    elapsed=monotonic()-started
    assert elapsed <= CLEANUP_GRACE_SECONDS
    assert not cycles and not sdk.requests
    assert await rows(p1,QQTrialRow)==[] and await rows(p1,QQSentAliasRow)==[]
    assert all(p.poll() is not None for p in p1["mailbox"]["processes"])
    before=list(p1["mailbox"]["commands"])
    with pytest.raises(QQTrialStopped):
        async with factory(p1,sdk,probe)() as worker:
            await run_once(worker)
    assert before==p1["mailbox"]["commands"]
    record_property("cancellation_cleanup",json.dumps({"elapsed":elapsed,
        "cleanup_grace":CLEANUP_GRACE_SECONDS,"lock_reacquired":True,"children_reaped":True}))


async def evidence_snapshot(p1):
    kinds=(QQTrialRow,QQTrialEventRow,QQSentAliasRow,RawArtifactRow,EmailInboundCursorRow)
    result={}
    for kind in kinds:
        result[kind.__tablename__]=[
            {column.name:getattr(row,column.name) for column in kind.__table__.columns}
            for row in await rows(p1,kind)]
    async with p1["sessions"]() as connection:
        result["policies"]=[tuple(row) for row in (await connection.execute(text(
            "SELECT tablename, policyname, permissive, roles, qual, with_check FROM pg_policies "
            "WHERE schemaname='public' AND tablename IN ('qq_reply_trials','qq_reply_trial_events','qq_sent_aliases') "
            "ORDER BY tablename, policyname"))).all()]
        result["grants"]=[tuple(row) for row in (await connection.execute(text(
            "SELECT c.relname, c.relacl::text, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
            "AND c.relname IN ('qq_reply_trials','qq_reply_trial_events','qq_sent_aliases') ORDER BY c.relname"))).all()]
    return result


@pytest.mark.parametrize("reason",["expired","stopped"])
async def test_restore_complete_disabled_reply_config_preserves_evidence(p1,reason,record_property):
    sdk,probe=CategorySDK("clear_interest"),ReplyProvider()
    async with factory(p1,sdk,probe)() as worker:
        assert (await run_once(worker)).cycles_completed==1
    settings=p1["model"]
    if reason=="stopped":
        async with p1["sessions"].begin() as db:
            await db.execute(update(QQTrialRow).where(QQTrialRow.tenant_id==p1["tenant"])
                .values(state="stopped",reason="synthetic_operator_stop"))
    else:
        qq=settings.qq_reply
        launch=qq.trial
        p1["model"]=settings.model_copy(update={"qq_reply":qq.model_copy(update={"trial":launch.model_copy(
            update={"starts_at":launch.starts_at-timedelta(hours=1),"expires_at":launch.expires_at-timedelta(hours=1)})})})
    with pytest.raises(QQTrialStopped):
        async with factory(p1,sdk,probe)() as worker:
            await run_once(worker)
    snapshot=await evidence_snapshot(p1)
    before=list(p1["mailbox"]["commands"])
    # 此合成基线未启用 Gmail/QQ；恢复整个已审核的回复配置，自动发送始终关闭。
    p1["model"]=settings.model_copy(update={"qq_reply":None,"reply_enabled":False,"automatic_send_enabled":False})
    async with factory(p1,sdk,probe)() as worker:
        outcome=await run_once(worker)
        assert outcome.cycles_completed==1
    assert p1["mailbox"]["commands"]==before and not sdk.requests
    assert await evidence_snapshot(p1)==snapshot
    record_property("recovery",json.dumps({"reason":reason,"normal_cycles":1,"qq_commands_added":0,
        "evidence_policies_grants_unchanged":True,"automatic_send_enabled":False}))
