# ruff: noqa: F811 -- pytest 显式复用持久化夹具
"""真实PG、双Gateway与协议替身：规划落原件后只发一次精确线程回复。"""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from tests.integration.test_dialogue_owner_access import (
    dialogue_owner_runtime,  # noqa: F401
)
from tests.integration.test_dialogue_source import (  # noqa: F401
    dialogue_source_runtime,
    owned_infrastructure,
    page_runtime,
)
from tests.integration.test_inquiry_admission import prepare_inquiry


@pytest.mark.parametrize("scenario", ["normal", "send_unknown", "withdraw_after_plan", "takeover_after_plan"])
async def test_inquiry_driver_actual_gateways_are_durable_and_empty_queue_has_no_model_io(dialogue_owner_runtime, scenario):
    from datetime import timedelta

    from apps.composition_support.outreach_fact_readers import (
        ProspectingDemandAccountNames,
    )
    from apps.scheduler_worker.dialogue_delivery import CurrentDialogueDelivery
    from apps.scheduler_worker.dialogue_driver import OrdinaryDialogueDriver
    from apps.scheduler_worker.dialogue_models import OrdinaryDialogueModels
    from apps.scheduler_worker.dialogue_plan_authority import DialoguePlanAuthority
    from apps.scheduler_worker.dialogue_plan_store import GatewayDialoguePlanStore
    from artifact_store.service_impl import GeneratedArtifactStoreImpl
    from connectors.gmail.client import GmailSendResult
    from domains.conversations.service_impl import ConversationServiceImpl
    from domains.demand.service_impl import DemandServiceImpl
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
    from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
    from infra.db.dialogue_execution import SqlDialogueExecutionStore
    from infra.db.dialogue_execution_tables import DialogueBindingRow
    from infra.db.tables import OutreachEnrollmentRow
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
    from infra.standalone.settings import StandaloneModelSettings
    from shared.schemas.identifiers import new_id
    from shared.schemas.model_invocation import ModelResponse, ModelUsage
    from tool_gateway.errors import DeliveryCertainty
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.handlers.dialogue_send import (
        MANIFEST,
        DialogueSendHandler,
        dialogue_checks,
    )
    from tool_gateway.manifest import ToolRegistry
    from tool_gateway.pipeline import ToolGateway
    from workflows.reply_qualification.dialogue_authority import (
        CurrentOrdinaryDialogueAccess,
    )
    from workflows.reply_qualification.ports import ReplyMessageContent

    data = await prepare_inquiry(dialogue_owner_runtime)
    runtime, source = data['runtime'], data['source']
    sessions, deps, tenant = runtime['factory'], runtime['deps'], source.tenant_id
    access = CurrentOrdinaryDialogueAccess(data['access'], deps.outreach, data['products'], deps.prospecting)
    conversations = ConversationServiceImpl(lambda t:SqlAlchemyConversationsUnitOfWork(sessions,t,now=lambda:datetime.now(UTC)), now=lambda:datetime.now(UTC), dialogue_access=access)
    class Objects:
        def __init__(self): self.items = {}
        async def put(self,key,value): self.items[key]=value
        async def delete(self,key): self.items.pop(key,None)
        async def get_bounded(self,key,*,maximum_bytes): return self.items[key]
    objects = Objects()
    artifacts = GeneratedArtifactStoreImpl(lambda t:SqlAlchemyArtifactUnitOfWork(sessions,t), objects, 32768, lambda:datetime.now(UTC), new_id, bounded_transport=objects)
    class Config:
        async def authorize(self,actor,version,*,probe): assert actor.employee_id==source.owner_employee_id and version=='test-v1'
    settings = StandaloneModelSettings(provider='compatible', model='synthetic', secret_ref='MODEL_API_KEY', configuration_version='test-v1', model_data_export_enabled=True, automatic_send_enabled=True,
        limits={"window_seconds": 60,"tenant_calls": 100,"employee_calls": 100,"tenant_concurrency": 1,"employee_concurrency": 1,"max_input_bytes": 32768,"max_output_tokens": 1000,"timeout_seconds": 30})
    fingerprints = HmacFingerprintProvider('v1', b'synthetic-fingerprint-key-for-test')
    calls=[]
    class Provider:
        def __init__(self,budget): self.budget=budget
        async def generate(self,request):
            async def dispatch(body): calls.append(body); return {}
            await self.budget.run({'model':'synthetic','max_output_tokens':request.max_output_tokens,'messages':[request.system_prompt,request.payload]}, dispatch)
            return ModelResponse(text='{"kind":"ordinary","question_topics":["quantity"]}', model='synthetic', usage=ModelUsage(input_tokens=10,cached_input_tokens=0,output_tokens=10))
        async def aclose(self): pass
    models = OrdinaryDialogueModels(sessions,settings,access,DialoguePlanAuthority(sessions,tenant,access,Config()),fingerprints,'test-dialogue',lambda budget,identity:Provider(budget))
    class Content:
        async def load(self,t,m): return ReplyMessageContent('Product question','We need hinges for cabinets.')
    class Binding:
        def resolver(self,actor,address,operation):
            assert actor.employee_id==source.owner_employee_id and operation==source.mailbox_operation_id
            return self
        def resolve(self,ref): assert ref=='BUSINESS_QQ_MAIL'; return 'synthetic-mail-password'
    sent=[]
    class Connector:
        async def send_once(self,request):
            sent.append(request)
            return GmailSendResult(None,DeliveryCertainty.UNKNOWN,False) if scenario=='send_unknown' else GmailSendResult('synthetic-sent',DeliveryCertainty.SENT,False)
        async def reconcile_once(self,request):
            assert scenario=='send_unknown' and sent==[request]
            return GmailSendResult('synthetic-sent',DeliveryCertainty.SENT,False)
    class Progress:
        async def progress(self,message): return await driver.progress(message)
    store=SqlDialogueExecutionStore(sessions,tenant)
    delivery=CurrentDialogueDelivery(sessions,tenant,access,store,artifacts,deps.sending_identities,Binding(),Progress(),lambda address,password:Connector(), settings=settings)
    registry=ToolRegistry();registry.register(MANIFEST,DialogueSendHandler(delivery,fingerprints))
    gateway_time=[datetime.now(UTC)]
    gateway=ToolGateway(registry,dialogue_checks(delivery),lambda t:SqlAlchemyToolGatewayUnitOfWork(sessions,t,now=lambda:gateway_time[0]),lease_duration=timedelta(seconds=60),lease_owner='test-dialogue',id_factory=new_id,now=lambda:gateway_time[0])
    results=[]
    original_invoke=gateway.invoke
    async def observe_invoke(context):
        result=await original_invoke(context)
        results.append((result.status,result.error_category,result.rejected))
        return result
    gateway.invoke=observe_invoke
    demand=DemandServiceImpl(lambda t:SqlAlchemyDemandUnitOfWork(sessions,t), now=lambda:datetime.now(UTC), account_names=ProspectingDemandAccountNames(deps.prospecting))
    driver=OrdinaryDialogueDriver(sessions,tenant,store,access,demand,conversations,Content(),models,GatewayDialoguePlanStore(sessions,artifacts,fingerprints),gateway,configuration_version='test-v1', settings=settings)
    assert await driver.scan_once()==0 and calls==[] and sent==[]
    # 准入真实binding已由业务桥创建；此测试定向执行driver，不冒充完整outbox/factory验收。
    await store.enqueue(source.admitted_message_id, source.binding_id)
    claim = await store.claim(lease_duration=timedelta(minutes=10))
    if scenario in {'withdraw_after_plan', 'takeover_after_plan'}:
        original_put=driver.plans.put
        async def withdrawn_plan(*args,**kwargs):
            result=await original_put(*args,**kwargs)
            if scenario == 'withdraw_after_plan':
                await deps.outreach.pause_campaign(tenant,data['campaign'].campaign_id,'撤销批准范围',actor=data['boss'])
            else:
                from domains.conversations.schemas import (
                    ReplyWorkAction,
                    ReplyWorkActionRequest,
                    ReplyWorkStatus,
                )
                from shared.schemas.identifiers import IdempotencyKey
                work = await conversations.enqueue_reply_work_action(tenant, ReplyWorkActionRequest(
                    message_id=source.admitted_message_id, outbound_message_id=None,
                    enrollment_id=None, account_id=source.account_id, contact_point_id=source.contact_point_id,
                    action=ReplyWorkAction.CREATE_FOLLOW_UP,
                    idempotency_key=IdempotencyKey(f'reply:create_follow_up:{source.admitted_message_id}'),
                    dialogue_binding_id=source.binding_id,
                ))
                await conversations.handle_reply_work(tenant, work.action_id,
                    actor=(await data['access'].require(tenant, source.admitted_message_id, source.owner_employee_id)).actor,
                    expected_status=ReplyWorkStatus.PENDING, status=ReplyWorkStatus.IN_PROGRESS)
            return result
        driver.plans.put=withdrawn_plan
        from shared.errors import PermissionDenied
        with pytest.raises(PermissionDenied):
            await driver._process(claim)
        assert len(calls)==1 and sent==[]
        return
    await driver._process(claim)
    execution=await store.read(source.admitted_message_id)
    if scenario=='send_unknown':
        assert execution.state.value=='unknown' and len(sent)==1
        # 新仓储实例仍只能走原attempt核对；未决占用不退款，不调用send_once。
        driver.store=SqlDialogueExecutionStore(sessions,tenant)
        gateway_time[0] += timedelta(seconds=61)
        await driver.scan_once()
        execution=await driver.store.read(source.admitted_message_id)
    assert execution.state.value=='sent', results
    assert len(calls)==1 and len(sent)==1
    assert sent[0].in_reply_to==data['mail'].message_header if hasattr(data['mail'],'message_header') else sent[0].in_reply_to.startswith('<')
    assert 'approximate quantity' in sent[0].body and sent[0].body.isascii()
    assert sent[0].references[-1]==sent[0].in_reply_to
    assert await driver.scan_once()==0 and len(calls)==1 and len(sent)==1
    async with sessions() as db:
        binding=await db.get(DialogueBindingRow,(tenant,source.binding_id))
        assert binding.rounds_sent==1 and binding.model_calls==1
        enrollment=await db.scalar(select(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id==tenant,OutreachEnrollmentRow.enrollment_id==dialogue_owner_runtime[1].enrollment_id))
        assert enrollment is not None # 原冷身份未借给新询盘。

    # 真实本人业务收取持久端口 → 精确已发Message-ID → 原binding/Need，不经过私人QQ grant。
    from email import policy as email_policy
    from email.message import EmailMessage
    from hashlib import sha256

    from apps.scheduler_worker.dialogue_business_inbound import (
        DialogueBusinessInboundBridge,
    )
    from connectors.qq_mail.business_inbound import content_hash, parse_business_mime
    from infra.db.business_mail_tables import BusinessMailRow
    from infra.db.dialogue_source import SqlDialogueSources
    from infra.db.tables import MessageRow, RawArtifactRow
    from shared.business_mail import ArchivedBusinessMail, BusinessArchivedPage
    incoming = EmailMessage()
    incoming['From'] = sent[0].recipient_address
    incoming['To'] = sent[0].from_address
    incoming['Message-ID'] = '<second-customer-message@buyer.test>'
    incoming['In-Reply-To'] = f'<{sent[0].deterministic_message_id}>'
    incoming['Subject'] = 'More details'
    incoming.set_content('We need 5000 pieces for cabinet doors.')
    raw = incoming.as_bytes(policy=email_policy.SMTP)
    parsed = parse_business_mime(raw, uid=2, uidvalidity=8, received_at=datetime.now(UTC))
    artifact = new_id('art'); digest = sha256(raw).hexdigest()
    async with sessions.begin() as db:
        db.add(RawArtifactRow(tenant_id=tenant, artifact_id=artifact, kind='email_raw', content_hash=digest,
            mime_type='message/rfc822', size_bytes=len(raw), object_key=f'raw/{tenant}/{artifact}', uploaded_by=None, uploaded_at=datetime.now(UTC)))
        original = await db.get(BusinessMailRow, (tenant, source.business_message_id))
        box_id = original.mailbox_id
    archived = ArchivedBusinessMail(uid=2, uidvalidity=8, sender=parsed.sender, subject=parsed.subject,
        body_text=parsed.body_text, message_header=parsed.message_header, received_at=parsed.received_at,
        in_reply_to=parsed.in_reply_to, reply_recipient=parsed.reply_recipient, artifact_id=artifact,
        mime_sha256=digest, source_sha256=digest, content_sha256=content_hash(parsed))
    checkpoint = await data['access'].mailbox.checkpoint(data['actor'], box_id)
    await data['access'].mailbox.apply_page(data['actor'], checkpoint,
        BusinessArchivedPage(uidvalidity=8, after_uid=1, next_uid=2, messages=(archived,)))
    bridge = DialogueBusinessInboundBridge(sessions, tenant, data['access'].mailbox, data['access'], deps.outreach, deps.sending_identities)
    assert await bridge.scan_once() == 1
    async with sessions() as db:
        binding = await db.get(DialogueBindingRow, (tenant,source.binding_id))
        assert binding.latest_message_id != source.admitted_message_id
        second = binding.latest_message_id
        message = await db.get(MessageRow, (tenant,second))
        assert message.outbound_message_id == f'<{sent[0].deterministic_message_id}>'
    assert (await SqlDialogueSources(sessions,tenant).for_message(tenant,second)).hypothesis_id == source.hypothesis_id
    assert await bridge.scan_once() == 0
    assert len(calls) == 1 and len(sent) == 1

    # 确定性报告从同一完整MIME业务收取入口关联新SENT；重复不重复抑制/计数。
    from infra.db.dialogue_execution_tables import DialogueBusinessReceiptRow
    from tests.unit.test_gmail_feedback_parser import _dsn
    report_raw = _dsn(message_id=(f'<{sent[0].deterministic_message_id}>',), idempotency=(sent[0].idempotency_header,))
    report_raw = report_raw.replace(b'Thu, 13 Aug 2026 10:00:00 +0000', b'Sat, 05 Sep 2026 00:00:00 +0000')
    report_raw = report_raw.replace(b'private-sender@example.com', sent[0].from_address.encode()).replace(b'private-recipient@example.com', sent[0].recipient_address.encode()).replace(b'private-0@example.com', sent[0].recipient_address.encode())
    report = parse_business_mime(report_raw, uid=3, uidvalidity=8, received_at=datetime.now(UTC))
    assert report.dialogue_feedback is not None
    report_artifact = new_id('art'); report_hash = sha256(report_raw).hexdigest()
    async with sessions.begin() as db:
        db.add(RawArtifactRow(tenant_id=tenant, artifact_id=report_artifact, kind='email_raw', content_hash=report_hash,
            mime_type='message/rfc822', size_bytes=len(report_raw), object_key=f'raw/{tenant}/{report_artifact}', uploaded_by=None, uploaded_at=datetime.now(UTC)))
    archived_report = ArchivedBusinessMail(uid=3, uidvalidity=8, sender=report.sender, subject=report.subject,
        body_text=report.body_text, message_header=report.message_header, received_at=report.received_at,
        dialogue_feedback=report.dialogue_feedback, artifact_id=report_artifact, mime_sha256=report_hash,
        source_sha256=report_hash, content_sha256=content_hash(report), parse_failure=report.parse_failure)
    checkpoint = await data['access'].mailbox.checkpoint(data['actor'], box_id)
    await data['access'].mailbox.apply_page(data['actor'], checkpoint,
        BusinessArchivedPage(uidvalidity=8, after_uid=2, next_uid=3, messages=(archived_report,)))
    from infra.db.tables import EmployeeRow
    async with sessions.begin() as db:
        owner_row=await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id==tenant,EmployeeRow.employee_id==source.owner_employee_id))
        owner_row.is_active=False
    assert await bridge.scan_once() == 1
    assert await bridge.scan_once() == 0
    async with sessions() as db:
        stopped = await db.get(DialogueBindingRow, (tenant,source.binding_id))
        assert stopped.state == 'stopped'
        receipts = (await db.scalars(select(DialogueBusinessReceiptRow.result).where(DialogueBusinessReceiptRow.tenant_id==tenant))).all()
        assert receipts.count('hard_bounce') == 1
    assert await driver.scan_once()==0 and len(calls)==1 and len(sent)==1
