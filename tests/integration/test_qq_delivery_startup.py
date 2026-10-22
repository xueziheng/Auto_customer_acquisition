"""真实 Gateway/PG/原件存储的收件方证明；仅邮箱与模型边界使用合成数据。"""
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import update

from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
from infra.db.qq_alias import record_verified_alias
from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialRow
from infra.db.tables import (
    ConversationReplyWorkRow,
    MessageRow,
    ToolCallRow,
    WorkflowRunRow,
)
from infra.qq_inbound_binding import CurrentQQBinding
from infra.standalone.qq_trial_settings import QQTrialLaunchSettings
from shared.schemas.qq_trial import QQTrialScope, QQTrialStopped
from tests.integration.test_qq_p1_standalone import (
    CategorySDK,
    ReplyProvider,
    deliver_notices,
    factory,
    probe_model,
    rows,
    run_once,
)
from tests.integration.test_qq_p1_standalone import p1 as p1  # noqa: PLC0414
from tests.integration.test_standalone_reply_chain import (
    reply_storage as reply_storage,  # noqa: PLC0414
)
from tests.unit.test_qq_inbound import mail


def configure(p1,monkeypatch,*,active=True):
    model=p1['model']; qq=model.qq_reply
    launch=QQTrialLaunchSettings.model_validate({**qq.trial.model_dump(),
        'delivery':{'employee_id':p1['boss'],'user_id':p1['boss_user'],
            'authorization_source':'explicit_user_request','authorization_ref':'SYNTHETIC-OWNED-RECIPIENT',
            'folder':'INBOX','uidvalidity':7,'uid':1,'timeout_seconds':30,'header_bytes':16384}})
    p1['model']=model.model_copy(update={'qq_reply':qq.model_copy(update={'trial':launch})})
    original=CurrentQQBinding.matches
    async def matches(self,t,e,u,address):
        if (t,e,u,address)==(p1['tenant'],p1['boss'],p1['boss_user'],'986647123@qq.com'):
            return active
        return await original(self,t,e,u,address)
    monkeypatch.setattr(CurrentQQBinding,'matches',matches)
    p1['mailbox']['delivery_login']='986647123@qq.com'
    p1['mailbox']['sent_search']=''


async def test_current_recipient_proof_activates_original_trial_without_sent_search(p1,monkeypatch):
    configure(p1,monkeypatch)
    sdk=CategorySDK('clear_interest')
    async with factory(p1,sdk,ReplyProvider())() as worker:
        await run_once(worker)
    trials=await rows(p1,QQTrialRow)
    assert len(trials)==1 and trials[0].state=='active'
    scope=QQTrialScope.model_validate(trials[0].scope)
    assert scope.alias_source=='controlled_recipient_imap'
    assert scope.delivery_employee_id==p1['boss'] and scope.delivery_user_id==p1['boss_user']
    calls=await rows(p1,ToolCallRow)
    proof=[c for c in calls if c.tool_id=='email.qq.delivery_alias.read']
    assert len(proof)==1 and proof[0].status=='succeeded' and proof[0].user_id==p1['boss_user']
    assert not any(c.tool_id=='email.qq.sent_alias.read' for c in calls)
    assert len(await rows(p1,QQSentAliasRow))==1 and not sdk.requests
    before=[x for x in p1['mailbox']['commands'] if x[0]=='receipt']
    assert sum(c.startswith('UID FETCH') for _,c in before)==1
    assert not any('SEARCH' in c or 'BODY.PEEK[]' in c for _,c in before)
    async with factory(p1,sdk,ReplyProvider())() as worker:
        await run_once(worker)
    assert [x for x in p1['mailbox']['commands'] if x[0]=='receipt']==before


async def test_revoked_recipient_binding_stops_before_any_mailbox_io(p1,monkeypatch):
    configure(p1,monkeypatch,active=False)
    with pytest.raises(QQTrialStopped):
        async with factory(p1,CategorySDK('clear_interest'),ReplyProvider())() as worker:
            await run_once(worker)
    assert p1['mailbox']['commands']==[]
    assert await rows(p1,QQTrialRow)==[] and await rows(p1,QQSentAliasRow)==[]


@pytest.mark.parametrize('change',['wrong_tool','wrong_actor','wrong_artifact','failed_call','manual_export'])
async def test_delivery_alias_rejects_unbound_or_manual_provenance(p1,monkeypatch,change):
    from connectors.qq_mail.sent_alias import verify_sent_alias
    configure(p1,monkeypatch)
    async with factory(p1,CategorySDK('clear_interest'),ReplyProvider())() as worker:
        await run_once(worker)
    scope=QQTrialScope.model_validate((await rows(p1,QQTrialRow))[0].scope)
    alias=(await rows(p1,QQSentAliasRow))[0]
    proof=verify_sent_alias((p1['sent'],),source='controlled_recipient_imap',
        canonical_header=scope.canonical_header,idempotency_header=scope.canonical_header[1:].split('@')[0],
        sender=scope.sender,recipient=scope.recipient)
    if change=='manual_export':
        proof=replace(proof,source='recipient_export')
    else:
        values={'wrong_tool':{'tool_id':'email.qq.sent_alias.read'},
            'wrong_actor':{'user_id':scope.user_id},'wrong_artifact':{'provider_ref':'art_other'},
            'failed_call':{'status':'failed_permanent','error_category':'provider_permanent','provider_ref':None}}[change]
        async with p1['sessions'].begin() as db:
            await db.execute(update(ToolCallRow).where(ToolCallRow.tenant_id==p1['tenant'],
                ToolCallRow.tool_call_id==alias.tool_call_id).values(**values))
    async def authorized():return True  # 仅合成验收，生产使用当前双方资格。
    with pytest.raises(QQTrialStopped) as error:
        await record_verified_alias(p1['sessions'],scope,proof,artifact_id=alias.artifact_id,
            tool_call_id=alias.tool_call_id,authorize=authorized)
    assert error.value.reason==('alias_source_not_collectable' if change=='manual_export' else 'alias_provenance_missing')
    assert len(await rows(p1,QQSentAliasRow))==1


async def test_failed_sent_attempt_cannot_switch_to_recipient_source(p1,monkeypatch):
    p1['mailbox']['sent_search']=''
    with pytest.raises(QQTrialStopped):
        async with factory(p1,CategorySDK('clear_interest'),ReplyProvider())() as worker:
            await run_once(worker)
    calls=await rows(p1,ToolCallRow)
    assert len(calls)==1 and calls[0].status=='failed_permanent'
    before=list(p1['mailbox']['commands'])
    configure(p1,monkeypatch)
    with pytest.raises(QQTrialStopped):
        async with factory(p1,CategorySDK('clear_interest'),ReplyProvider())() as worker:
            await run_once(worker)
    assert p1['mailbox']['commands']==before
    assert len(await rows(p1,ToolCallRow))==1
    assert await rows(p1,QQTrialRow)==[] and await rows(p1,QQSentAliasRow)==[]


@pytest.mark.parametrize('category,body',[
    ('clear_interest','I would like to buy. Please send specifications and MOQ.'),
    ('willing_to_continue','I would like to add you on WhatsApp to discuss further.'),
])
async def test_recipient_proof_to_reply_followup_and_notice(p1,monkeypatch,category,body):
    configure(p1,monkeypatch)
    sdk=CategorySDK(category)
    async with factory(p1,sdk,ReplyProvider())() as worker:
        stop=asyncio.Event()
        cycles=0
        async def wait(interval,event):
            nonlocal cycles
            cycles+=1
            if cycles==1:
                assert len(await rows(p1,QQSentAliasRow))==1
                await probe_model(p1,worker)
                p1['mailbox']['reply']=mail(reply=p1['alias'],body=body)
                await asyncio.sleep(31)
            if cycles>=9:
                stop.set()
        result=await run_scheduler_worker(worker,stop_event=stop,wait=wait,install_signal_handlers=False)
        assert result.status is WorkerStartStatus.STARTED
    incoming=[m for m in await rows(p1,MessageRow) if m.direction=='inbound']
    trial=(await rows(p1,QQTrialRow))[0]
    assert len(incoming)==1 and trial.message_id==incoming[0].message_id
    assert trial.state=='received' and trial.classifications==1 and trial.bodies==1
    work=await rows(p1,ConversationReplyWorkRow)
    assert len(work)==1 and work[0].action=='start_qualification'
    assert work[0].message_id==incoming[0].message_id and work[0].account_id==p1['account']
    notices=await deliver_notices(p1)
    assert len(notices)==1 and notices[0].context.reason_code==category
    runs=[r for r in await rows(p1,WorkflowRunRow) if r.workflow_type=='reply_qualification']
    assert len(runs)==1 and runs[0].status=='completed'
    before=list(p1['mailbox']['commands']); requests=len(sdk.requests)
    async with factory(p1,sdk,ReplyProvider())() as worker:
        await run_once(worker)
    assert p1['mailbox']['commands']==before and len(sdk.requests)==requests
    assert len(await rows(p1,ConversationReplyWorkRow))==1 and len(await deliver_notices(p1))==1
