"""独立合成 P1：原 standalone/单副本循环/PG/MinIO；只替换邮箱与模型边界。"""
import asyncio
import hashlib
import json
import socket
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from apps.composition_support.employee_readers import employee_service_scope
from apps.notification_worker.config import NotificationWorkerConfig
from apps.notification_worker.runtime import (
    NotificationRuntimeMode,
    notification_worker_runtime,
    run_notification_worker,
)
from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
from apps.scheduler_worker.standalone import create_standalone_factory
from domains.assistant.schemas import AssistantActor
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import (
    Actor,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialRow
from infra.db.repositories.in_app_notifications import PostgresInAppNotificationStore
from infra.db.session import create_engine_from
from infra.db.tables import (
    ConversationReplyWorkRow,
    EmailInboundCursorRow,
    EmployeeRow,
    MessageRow,
    ModelInvocationRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    RawArtifactRow,
    ToolCallEventRow,
    ToolCallRow,
    WorkflowRunRow,
)
from infra.qq_inbound_binding import CurrentQQBinding
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.qq_trial import QQTrialLimits, QQTrialStopped
from tests.integration.test_manual_email_send_api import _seed_employee
from tests.integration.test_model_usage import limits
from tests.integration.test_qq_inbound_scope import seed
from tests.integration.test_qq_trial_model import FakeSDK, FixedBrokerProvider
from tests.integration.test_reply_completion import advance
from tests.integration.test_standalone_reply_chain import (
    NoModelCredentials,
    ReplyProvider,
)
from tests.integration.test_standalone_reply_chain import (
    reply_storage as reply_storage,  # noqa: PLC0414
)
from tests.runtime_database_fixtures import runtime_database_scope
from tests.unit.qq_p1_imap import p1_mailbox
from tests.unit.test_qq_inbound import mail
from tool_gateway.fingerprint import HmacFingerprintProvider


class CategorySDK(FakeSDK):
    def __init__(self,category):
        super().__init__("unknown" if category=="unknown" else "ok")
        self.category=category

    async def create(self,**body):
        result=(await super().create(**body)).model_dump()
        if len(self.requests)>1:
            result["output"][0]["content"][0]["text"]=json.dumps({"category":self.category,"candidate_fields":[]})
        return SimpleNamespace(model_dump=lambda:result)


@pytest_asyncio.fixture
async def p1(reply_storage,tmp_path,monkeypatch):
    tenant=TenantId(new_id("tn"))
    async with runtime_database_scope(reply_storage.migration_database_url.get_secret_value()) as provision:
        url=make_url(await provision(tenant))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1",0))
            health=listener.getsockname()[1]
        profile=reply_storage.model_copy(update={"tenant_id":tenant,"database_username":url.username,
            "database_runtime_password":SecretStr(url.password),"gmail":None,"scheduler_port":health})
        engine=create_engine_from(profile.database_url.get_secret_value())
        sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            route,owner,user,canonical,attempt,account,_point=await seed(sessions,tenant)
            boss=new_id("emp")
            await _seed_employee(sessions,tenant,boss)
            async with sessions.begin() as db:
                boss_user=await db.scalar(select(EmployeeRow.user_id).where(EmployeeRow.tenant_id==tenant,EmployeeRow.employee_id==boss))
                await db.execute(update(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id==tenant,OutreachMessageAttemptRow.attempt_id==attempt
                ).values(message_id=new_id("msg")))
            route=route.model_copy(update={"mailbox_alias":"qq-p1-"+new_id("x").split("_")[1].lower(),"config_version":"synthetic-p1-v1"})
            stamp=datetime.now(UTC)
            model=StandaloneModelSettings.model_validate({"provider":"deepseek","model":"synthetic-p1-model",
                "secret_ref":"SYNTHETIC_MODEL_ONLY","configuration_version":"synthetic-p1-v1",
                "limits":limits(tenant_calls=10,employee_calls=10,max_input_bytes=32768,max_output_tokens=512,timeout_seconds=60),
                "model_data_export_enabled":True,"reply_enabled":True,"automatic_send_enabled":False,
                "qq_reply":{"employee_id":owner,"user_id":user,"classifier_employee_id":boss,
                    "mailbox_directory":tmp_path/"SYNTHETIC-MAILBOX-NO-CREDENTIALS","attempt_id":attempt,
                    "trial":{"enabled":True,"trial_id":new_id("qqt"),"route":route,"alias_evidence_id":new_id("qae"),
                        "classifier_user_id":boss_user,"authorization_ref":"SYNTHETIC-P1-LOCAL-ONLY",
                        "authorization_source":"explicit_user_request","canonical_header":canonical,
                        "starts_at":stamp-timedelta(seconds=1),"expires_at":stamp+timedelta(seconds=599),
                        "limits":QQTrialLimits(model_timeout_seconds=30),
                        "sent":{"folder":"Sent","uidvalidity":7,"first_uid":1,"last_uid":1,"timeout_seconds":30}}}})
            async def matches(self,t,e,u,address):
                return (t,e,u,address)==(tenant,owner,user,model.qq_reply.address)
            def resolve(self,ref):
                assert ref=="QQ_INBOUND_AUTH"
                return "synthetic-p1-password"
            monkeypatch.setattr(CurrentQQBinding,"matches",matches)
            monkeypatch.setattr(CurrentQQBinding,"resolve",resolve)
            alias=f"<{new_id('synthetic')}@qq.com>"
            sent=(f"Message-ID: {alias}\r\nX-TradeOS-Idempotency-V1: {canonical[1:].split('@')[0]}\r\n"
                "From: 1249583590@qq.com\r\nTo: 986647123@qq.com\r\n\r\n").encode()
            with p1_mailbox(monkeypatch,sent) as mailbox:
                yield {"tenant":tenant,"sessions":sessions,"profile":profile,"model":model,"boss":boss,
                    "boss_user":boss_user,"owner":owner,"account":account,"route":route,"mailbox":mailbox,
                    "alias":alias,"sent":sent,"attempt":attempt}
        finally:
            await engine.dispose()


def factory(p1,sdk,probe):
    return create_standalone_factory(p1["profile"],p1["model"],NoModelCredentials(),
        provider_factory=lambda:probe,trial_provider_factory=lambda budget,identity:FixedBrokerProvider(budget,sdk))


async def rows(p1,kind):
    async with p1["sessions"]() as db:
        return list((await db.scalars(select(kind).where(kind.tenant_id==p1["tenant"])) ).all())


async def probe_model(p1,worker):
    tenant=p1["tenant"]
    settings=p1["model"]
    now=lambda:datetime.now(UTC)
    fingerprints=HmacFingerprintProvider("v1",p1["profile"].resolve("PILOT_FINGERPRINT").encode())
    repo=SqlModelConfigurationRepository(p1["sessions"],fingerprints,now=now)
    await repo.register_process(tenant,"api",settings.configuration_version,"synthetic-p1-api")
    employees=partial(employee_service_scope,p1["sessions"],now=now,
        authorizer=Phase1EmployeeAuthorizer(tenant),audit=StandardAuditLogger())
    config=ModelConfigurationServiceImpl(repo,CurrentEmployeeIdentity(employees,
        Actor("system:synthetic-p1",EmployeeScope.SYSTEM,"system")),now=now)
    await config.request_probe(AssistantActor(tenant_id=tenant,employee_id=p1["boss"],user_id=p1["boss_user"]),"synthetic-p1-probe")
    await worker.assistant_driver.scan_once()
    await advance(worker,tenant,8)


async def deliver_notices(p1):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1",0))
        port=listener.getsockname()[1]
    config=NotificationWorkerConfig(p1["profile"].database_url,p1["tenant"],1,30,port,"synthetic-p1-notices")
    async with notification_worker_runtime(config,mode=NotificationRuntimeMode.CONTROLLED_IN_APP) as delivery:
        stop=asyncio.Event()
        async def once(seconds,event):
            event.set()
        await run_notification_worker(delivery,stop_event=stop,wait=once)
    store=PostgresInAppNotificationStore(p1["sessions"])
    return await store.list_for_recipient(p1["tenant"],p1["boss"],limit=10,before=None)


async def run_once(worker):
    stop=asyncio.Event()
    async def once(interval,event):
        stop.set()
    return await run_scheduler_worker(worker,stop_event=stop,wait=once,install_signal_handlers=False)


@pytest.mark.parametrize("category",["clear_interest","willing_to_continue","needs_review","unknown"])
async def test_original_startup_collection_to_owner_notice_and_restart(p1,category,record_property):
    sdk,probe=CategorySDK(category),ReplyProvider()
    assert await rows(p1,QQSentAliasRow)==[] and await rows(p1,ToolCallRow)==[]
    async with factory(p1,sdk,probe)() as worker:
        assert p1["mailbox"]["commands"]==[]
        stop=asyncio.Event()
        cycles=0
        async def wait(interval,event):
            nonlocal cycles
            cycles+=1
            if cycles==1:
                assert len(await rows(p1,QQSentAliasRow))==1
                await probe_model(p1,worker)
                body={"clear_interest":"I want to purchase solar fans.","willing_to_continue":"Please send more information.",
                    "needs_review":"Please check this unclear reply.","unknown":"I want to purchase solar fans."}[category]
                p1["mailbox"]["reply"]=mail(reply=p1["alias"],body=body)
                await asyncio.sleep(31)  # 原 DB 时钟与 30 秒门禁，不手工改游标或预算。
            if cycles>=9:
                stop.set()
        outcome=await run_scheduler_worker(worker,stop_event=stop,wait=wait,install_signal_handlers=False)
        assert outcome.status is WorkerStartStatus.STARTED
    calls=await rows(p1,ToolCallRow)
    collect=[c for c in calls if c.tool_id=="email.qq.sent_alias.read"]
    assert len(collect)==1 and collect[0].status=="succeeded" and collect[0].attempt_count==1
    alias=(await rows(p1,QQSentAliasRow))[0]
    assert alias.tool_call_id==collect[0].tool_call_id and alias.artifact_id==collect[0].provider_ref
    assert alias.artifact_hash==hashlib.sha256(p1["sent"]).hexdigest()
    events=await rows(p1,ToolCallEventRow)
    assert any(e.tool_call_id==collect[0].tool_call_id and e.outcome=="executing" for e in events)
    trial=(await rows(p1,QQTrialRow))[0]
    incoming=[m for m in await rows(p1,MessageRow) if m.direction=="inbound"]
    assert len(incoming)==1 and trial.message_id==incoming[0].message_id
    assert trial.sessions==3 and trial.searches==2 and trial.headers==2 and trial.bodies==1
    assert trial.classifications==1 and len(await rows(p1,RawArtifactRow))==2
    work=await rows(p1,ConversationReplyWorkRow)
    notices=await deliver_notices(p1)
    count=int(category in {"clear_interest","willing_to_continue"})
    work_count=int(category!="unknown")
    assert len(work)==work_count and len(notices)==count
    if work_count:
        assert work[0].account_id==p1["account"] and work[0].message_id==incoming[0].message_id
        assert work[0].action==("review_reply" if category=="needs_review" else "start_qualification")
    if count:
        assert notices[0].context.reason_code==category
    before=list(p1["mailbox"]["commands"])
    requests=len(sdk.requests)
    if category=="unknown":
        assert trial.state=="stopped" and trial.provider_pending and trial.provider_requests==1
        with pytest.raises(QQTrialStopped):
            async with factory(p1,sdk,probe)() as worker:
                await run_once(worker)
    else:
        assert trial.state=="received" and trial.provider_requests==2 and trial.output_tokens==10
        runs=[r for r in await rows(p1,WorkflowRunRow) if r.workflow_type=="reply_qualification"]
        assert len(runs)==1 and runs[0].status=="completed",[(r.status,r.last_error) for r in runs]
        async with factory(p1,sdk,probe)() as worker:
            await run_once(worker)
    assert p1["mailbox"]["commands"]==before and len(sdk.requests)==requests
    assert len(await rows(p1,ConversationReplyWorkRow))==work_count
    assert len(await deliver_notices(p1))==count
    invocations=await rows(p1,ModelInvocationRow)
    assert sum(i.capability=="reply_qualification" for i in invocations)==1
    record_property("p1_original_entry","create_standalone_factory_and_run_scheduler_worker")
    record_property("p1_alias_provenance","real_gateway_executing_raw_sha256_registration")
    record_property("p1_restart","no_mailbox_read_or_model_repeat")
    record_property("p1_category",category)
    record_property("p1_notifications","original_notification_worker_controlled_in_app")


async def test_ambiguous_collection_permanent_and_restart_does_not_retry(p1):
    p1["mailbox"]["sent_search"]="1 2"
    sdk,probe=CategorySDK("clear_interest"),ReplyProvider()
    for _ in range(2):
        with pytest.raises(QQTrialStopped):
            async with factory(p1,sdk,probe)() as worker:
                await run_once(worker)
    assert await rows(p1,QQSentAliasRow)==[] and await rows(p1,QQTrialRow)==[]
    assert not sdk.requests
    assert sum(command.startswith("UID SEARCH") for _,command in p1["mailbox"]["commands"])==1


async def test_no_singleton_lock_no_collection_or_trial(p1):
    sdk,probe=CategorySDK("clear_interest"),ReplyProvider()
    async with factory(p1,sdk,probe)() as worker, worker.lock_engine.connect() as holder:
        assert await holder.scalar(text("SELECT pg_try_advisory_lock(:key)"),{"key":worker.config.lock_key})
        await holder.commit()
        try:
            result=await run_once(worker)
            assert result.status is WorkerStartStatus.NOT_STARTED and result.cycles_completed==0
        finally:
            assert await holder.scalar(text("SELECT pg_advisory_unlock(:key)"),{"key":worker.config.lock_key})
            await holder.commit()
    assert p1["mailbox"]["commands"]==[] and not sdk.requests
    assert await rows(p1,ToolCallRow)==[] and await rows(p1,QQTrialRow)==[]


@pytest.mark.parametrize("change",["tenant","owner","boss_user","expired","cursor"])
async def test_new_entry_rejects_stale_scope_before_collection(p1,change):
    qq=p1["model"].qq_reply
    launch=qq.trial
    if change=="tenant":
        launch=launch.model_copy(update={"route":launch.route.model_copy(update={"tenant_id":new_id("tn")})})
    elif change=="boss_user":
        launch=launch.model_copy(update={"classifier_user_id":new_id("usr")})
    elif change=="expired":
        launch=launch.model_copy(update={"starts_at":launch.starts_at-timedelta(hours=1),"expires_at":launch.expires_at-timedelta(hours=1)})
    elif change=="owner":
        async with p1["sessions"].begin() as db:
            await db.execute(update(OwnershipLockRow).where(OwnershipLockRow.tenant_id==p1["tenant"],
                OwnershipLockRow.account_id==p1["account"]).values(owner=p1["boss"]))
    elif change=="cursor":
        route=p1["route"]
        stamp=datetime.now(UTC)
        async with p1["sessions"].begin() as db:
            db.add(EmailInboundCursorRow(**route.model_dump(),provider_cursor="synthetic-stale-cursor",version=1,
                bootstrap_started_at=stamp,after_epoch=int(stamp.timestamp()),confirmed_by=p1["boss"],confirmed_at=stamp))
    p1["model"]=p1["model"].model_copy(update={"qq_reply":qq.model_copy(update={"trial":launch})})
    sdk,probe=CategorySDK("clear_interest"),ReplyProvider()
    with pytest.raises((QQTrialStopped,ValueError)):
        async with factory(p1,sdk,probe)() as worker:
            await run_once(worker)
    assert p1["mailbox"]["commands"]==[] and not sdk.requests
    assert await rows(p1,ToolCallRow)==[] and await rows(p1,QQTrialRow)==[]
