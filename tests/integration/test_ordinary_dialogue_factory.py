# ruff: noqa: F811 -- pytest 显式复用持久化夹具
"""同一QQ身份：正式API首封→本人原件→规范工厂分类/REPLIED→普通线程回复。"""
import asyncio
import json
import socket
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from email import policy as email_policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from sqlalchemy import select

from shared.schemas.identifiers import new_id
from shared.schemas.money import CurrencyCode, Money
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import (  # noqa: F401
    NOW,
    page_runtime,
    prepare_sent,
)


@pytest.mark.parametrize("mode", ["normal", "invalid_plan", "unknown_model", "off", "budget", "stop", "repeat", "pending_during_model", "pending_after_plan", "inquiry"])
async def test_actual_factory_qq_cold_materials_to_sales_dialogue_same_need(page_runtime, tmp_path, monkeypatch, mode):
    """cold从原API首封开始；inquiry从未准入本人邮件POST开始，两者均经真实factory消费。"""
    from apps.api.composition.aliyun_web import AliyunOutboundConnectors
    from apps.api.composition.runtime import build_phase1_dependencies
    from apps.api.controlled import initialize_identities
    from apps.api.runtime_config import Phase1RuntimeSettings
    from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap
    from apps.scheduler_worker.dialogue_resources import DialogueArtifactPorts
    from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
    from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
    from apps.scheduler_worker.standalone_reply import bind_reply
    from artifact_store.service_impl import RawArtifactStoreImpl
    from artifact_store.store import RawArtifactKind
    from connectors.object_store.config import S3ObjectStoreSettings
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from connectors.qq_mail.business_inbound import content_hash, parse_business_mime
    from connectors.qq_mail.client import QQMailConnector
    from domains.conversations.aliyun_connection import AliyunActor
    from domains.conversations.business_mail import BusinessMailActor
    from domains.opportunities.scoring import ScoringPolicy
    from domains.opportunities.service_impl import HandoffPolicy
    from domains.outreach.schemas import OrdinaryDialoguePolicyInput
    from domains.products.permissions import (
        Phase2ProductAuthorizer,
        ProductActor,
        ProductRole,
    )
    from domains.products.service_impl import ProductServiceImpl
    from domains.sending_identity.schemas import DomainRole
    from domains.sending_identity.service import (
        Actor,
        IdentityRegisterRequest,
        ScopeLevel,
        SendingIdentityScope,
    )
    from infra.aliyun_mail_store import AliyunBinding, AliyunMailStore
    from infra.business_mail_binding import CurrentBusinessMailboxBinding
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.business_mail import SqlBusinessMailRepository
    from infra.db.dialogue_execution_tables import (
        DialogueBindingRow,
        DialogueExecutionRow,
    )
    from infra.db.model_configuration import SqlModelConfigurationRepository
    from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
    from infra.db.tables import (
        ConversationReplyWorkRow,
        OutreachEnrollmentRow,
        WorkflowRunRow,
    )
    from shared.business_mail import ArchivedBusinessMail, BusinessArchivedPage
    from shared.schemas.model_invocation import ModelResponse, ModelUsage
    from shared.schemas.quote_facts import canonical_fact_hash
    from tests.unit.test_aliyun_mail_connector import PASSWORD, MailServer
    from tests.unit.test_dialogue_send_switch import settings as model_settings
    from tests.unit.test_product_service import _formal_product
    from tool_gateway.fingerprint import HmacFingerprintProvider

    runtime = page_runtime
    sessions, tenant = runtime['factory'], runtime['route'].tenant_id
    config = runtime['config'].model_copy(update={'identities':tuple(i.model_copy(update={'employee_id':new_id('emp'), 'user_id':new_id('usr')}) for i in runtime['config'].identities)})
    await initialize_identities(config)
    staff = config.identities
    boss, sales = next(i for i in staff if i.role=='boss'), next(i for i in staff if i.role=='sales')
    server = MailServer()
    monkeypatch.setattr('connectors.qq_mail.client.QQMailTransport', lambda:server)
    root = tmp_path/'synthetic-mailbox'; root.mkdir(mode=0o700)
    mailbox_actor = AliyunActor(tenant_id=tenant, employee_id=sales.employee_id, user_id=sales.user_id)
    operation = new_id('aco')
    AliyunMailStore(root).publish(AliyunBinding(actor=mailbox_actor, email='sales@qq.com', password=PASSWORD,
        connected_at=NOW, operation_id=operation))
    env = config.runtime_environment()
    env['TRADEOS_TENANT_ID'] = tenant
    deps = build_phase1_dependencies(Phase1RuntimeSettings.from_environ(env), sessions, now=lambda:NOW,
        secret_resolver=config, outbound_mail_connectors=AliyunOutboundConnectors(AliyunMailStore(root), sessions,
            connector_factory=lambda address,password:QQMailConnector(address,password,transport=server)))
    runtime['deps'] = deps
    sid = await deps.sending_identities.register(tenant, IdentityRegisterRequest('sales@qq.com','qq.com',DomainRole.COLD_OUTREACH),
        actor=Actor(boss.employee_id,SendingIdentityScope(level=ScopeLevel.TENANT),'boss'))
    runtime['route'] = runtime['route'].model_copy(update={'configured_identity_id':sid})
    product = _formal_product(new_id('prd'));product.tenant_id=tenant; product.supplier_id=None
    product.spec_summary='Hinges for cabinet doors.'
    product.internal_cost=None;product.internal_cost_basis=None;product.internal_cost_unit=None;product.internal_cost_source_ref=None;product.match_specs={}
    async with SqlAlchemyProductsUnitOfWork(sessions, tenant) as uow:
        await uow.products.add(tenant, product)
    products = ProductServiceImpl(lambda t:SqlAlchemyProductsUnitOfWork(sessions,t), Phase2ProductAuthorizer(tenant), now=lambda:NOW)
    visible = await products.get_ordinary_dialogue_view(tenant,product.product_id,actor=ProductActor(boss.employee_id,ProductRole.BOSS,tenant))
    scope = OrdinaryDialoguePolicyInput(owner_employee_id=sales.employee_id,assessment_ref='approved-inquiry',
        reply_sender_identity_ids=(sid,),product_refs=({'product_id':product.product_id,'customer_view_sha256':canonical_fact_hash(asdict(visible))},),
        expires_at=NOW+timedelta(days=5),max_rounds=6,max_model_calls=2 if mode=="budget" else 10,max_no_progress_rounds=2,max_input_bytes=16000,max_output_tokens=1000)
    await prepare_sent(runtime, reply_source=True, initialized_staff=staff, ordinary_policy=scope, sending_employee_id=sales.employee_id, prepare_only=mode=='inquiry')
    offset = 0 if mode == 'inquiry' else 1
    assert len(server.sent) == offset
    cold = BytesParser(policy=email_policy.default).parsebytes(server.sent[0]) if offset else None
    if cold:
        assert cold['From']=='sales@qq.com' and cold['In-Reply-To'] is None
        assert cold['Message-ID']==runtime['outbound']
    buyer_address = str(cold['To']) if cold else (await deps.prospecting.get_contact_point(tenant,runtime['source_contact_point_id'])).value
    mailboxes=SqlBusinessMailRepository(sessions,CurrentBusinessMailboxBinding(AliyunMailStore(root)))
    actor=BusinessMailActor(**mailbox_actor.model_dump())
    mailbox=await mailboxes.register(actor,address='sales@qq.com',operation=operation)
    objects=S3ObjectBlobTransport(S3ObjectStoreSettings.from_environ(env),config)
    artifacts=RawArtifactStoreImpl(lambda t:SqlAlchemyArtifactUnitOfWork(sessions,t),objects,4*1024*1024,lambda:NOW,new_id)
    async def receive(uid, body, header):
        message=EmailMessage();message['From']=buyer_address;message['To']='sales@qq.com'
        message['Message-ID']=f'<customer-{uid}@buyer.test>'
        if header is not None:
            message['In-Reply-To']=header
        message['Subject']='Product enquiry';message.set_content(body)
        raw=message.as_bytes(policy=email_policy.SMTP)
        parsed=parse_business_mime(raw,uid=uid,uidvalidity=8,received_at=NOW)
        assert parsed.in_reply_to == header
        meta=await artifacts.put(tenant,RawArtifactKind.EMAIL_RAW,raw,'message/rfc822')
        item=ArchivedBusinessMail(uid=uid,uidvalidity=8,sender=parsed.sender,subject=parsed.subject,body_text=parsed.body_text,
            message_header=parsed.message_header,in_reply_to=parsed.in_reply_to,reply_recipient=parsed.reply_recipient,
            received_at=NOW,artifact_id=meta.artifact_id,mime_sha256=meta.content_hash,source_sha256=meta.content_hash,content_sha256=content_hash(parsed))
        await mailboxes.apply_page(actor,await mailboxes.checkpoint(actor,mailbox),BusinessArchivedPage(uidvalidity=8,after_uid=uid-1,next_uid=uid,messages=(item,)))
    fingerprints=HmacFingerprintProvider('v1',b'controlled-dialogue-fingerprint-key')
    settings=model_settings(mode != "off").model_copy(update={'api_key_file':tmp_path/'unread-synthetic-key', 'api_control_tenant':new_id('tn'),
        'api_business_tenant':tenant,'api_verified_at':NOW,'codex_job_root':tmp_path/'unused-codex'})
    configuration=SqlModelConfigurationRepository(sessions,fingerprints,now=lambda:NOW)
    await configuration.initialize(tenant,settings.configuration_version,settings.model,settings.limits,True)
    for process in ('api','scheduler'):
        await configuration.register_process(tenant,process,settings.configuration_version,f'controlled-{process}')
    calls=[]
    class Provider:
        def __init__(self,budget=None): self.budget=budget
        async def generate(self,request):
            async def dispatch(body): calls.append(request);return {}
            if self.budget:
                await self.budget.run({'model':request.model,'max_output_tokens':request.max_output_tokens,'messages':[request.system_prompt,request.payload]},dispatch)
            else: await dispatch({})
            if 'allowed_question_topics' in request.payload:
                if mode == 'pending_during_model' and request.payload['customer']['text'].strip() == 'We need hinges.':
                    await receive(3, 'We need hinges for cabinet doors, 5000 units.', str(ordinary['Message-ID']))
                if mode == 'unknown_model':
                    from shared.schemas.model_invocation import ModelGenerationError
                    raise ModelGenerationError('unknown')
                if mode == 'invalid_plan':
                    return ModelResponse(model=request.model, text='{"kind":"ordinary","unapproved_text":"price"}', usage=ModelUsage(input_tokens=10,cached_input_tokens=0,output_tokens=20))
                output={'kind':'ordinary','facts':[{'product_id':str(product.product_id),'section':'spec_display'}],'question_topics':[]}
            else:
                body = request.payload.get('body', '')
                if 'Please stop' in body:
                    output={'category':'unsubscribe','candidate_fields':[],'suppress_scope':'contact'}
                elif '5000 units' in body:
                    output={'category':'provides_specification','candidate_fields':[
                        {'field':'product_category','value':'hinges','quote':'hinges'},
                        {'field':'application','value':'cabinet doors','quote':'cabinet doors'},
                        {'field':'quantity','value':'5000','quote':'5000 units'}]}
                elif 'hinges' in body:
                    output={'category':'provides_specification','candidate_fields':[
                        {'field':'product_category','value':'hinges','quote':'hinges'}]}
                else:
                    output={'category':'requests_materials','candidate_fields':[]}
            return ModelResponse(model=request.model,text=json.dumps(output),usage=ModelUsage(input_tokens=10,cached_input_tokens=0,output_tokens=20))
        async def aclose(self): pass
    monkeypatch.setattr('apps.scheduler_worker.dialogue_models.configured_codex_provider',lambda settings,identity,round_budget:Provider(round_budget))
    captured=[]
    def reply(core,outreach,resources):
        result=bind_reply(core,outreach,resources,tenant_id=tenant,employee_id=boss.employee_id,
            settings=settings,resolver=config,fingerprints=fingerprints,owner='formal-dialogue-test',mailbox_directory=root,provider_factory=Provider)
        captured.append(result)
        return result
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));env['TRADEOS_SCHEDULER_HEALTH_PORT']=str(sock.getsockname()[1])
    factory=SchedulerRuntimeFactory(env,bootstrap=CanonicalSchedulerBootstrap(
        ScoringPolicy('controlled',(Money(Decimal(1000),CurrencyCode('USD')),),{i:'high' for i in range(1,8)}),
        HandoffPolicy(3600,10),reply_factory=reply),
        reply_artifact_ports=DialogueArtifactPorts(S3ObjectStoreSettings.from_environ(env),config),now=lambda:NOW)
    try:
        await receive(1,'Please introduce this product.',str(cold['Message-ID']) if cold else None)
        if mode == 'inquiry':
            from httpx import ASGITransport, AsyncClient

            from apps.api.main import create_app
            from apps.api.middleware import ApiSettings
            from domains.conversations.business_mail import BusinessMailService
            from infra.db.business_mail_tables import BusinessMailRow
            from infra.db.inquiry_admission import SqlInquiryAdmissionTransaction
            from workflows.reply_qualification.inquiry_admission import InquiryAdmission
            async with sessions() as db:
                assert await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==tenant)) is None
                assert await db.scalar(select(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id==tenant)) is None
                initial_mail=await db.scalar(select(BusinessMailRow).where(BusinessMailRow.tenant_id==tenant))
            app=create_app(settings=ApiSettings(tenant_id=tenant,dev_mode=True,retry_after_seconds=2),dependencies=deps)
            app.state.business_mail=BusinessMailService(mailboxes)
            app.state.ordinary_inquiry_admission=InquiryAdmission(
                lambda t:SqlInquiryAdmissionTransaction(sessions,t,mailboxes,now=lambda:NOW),deps.outreach,products,deps.prospecting)
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://controlled.test') as client:
                admitted=await client.post(f'/inbox/business-mail/{initial_mail.message_id}/ordinary-dialogue',
                    headers={'X-Tenant-Id':tenant,'X-Employee-Id':sales.employee_id},
                    json={'account_id':runtime['source_account_id'],'contact_point_id':runtime['source_contact_point_id'],
                        'campaign_id':runtime['source_campaign_id'],'campaign_version':1,'product_id':product.product_id,
                        'customer_quote':'Please introduce this product.'})
                assert admitted.status_code==200,admitted.text

        async with factory() as worker:
            driver=captured[0].dialogue_driver
            assert driver is not None
            original_process = driver._process
            failures = []
            async def observe_process(claim):
                try:
                    await original_process(claim)
                except Exception as error:
                    failures.append((type(error).__name__, getattr(error,"code",None), getattr(error,"diagnostic",None)))
                    raise
            driver._process = observe_process
            async def pump(cycles=12):
                stop = asyncio.Event()
                elapsed = 0
                async def wait(interval, event):
                    nonlocal elapsed
                    elapsed += 1
                    if elapsed >= cycles:
                        event.set()
                outcome = await run_scheduler_worker(worker, stop_event=stop, wait=wait, install_signal_handlers=False)
                assert outcome.status is WorkerStartStatus.STARTED and outcome.cycles_completed == cycles
            await pump()
            async with sessions() as db:
                runs=(await db.execute(select(WorkflowRunRow.current_step,WorkflowRunRow.status,WorkflowRunRow.last_error).where(WorkflowRunRow.tenant_id==tenant,WorkflowRunRow.workflow_type=='reply_qualification'))).all()
                assert runs and all(row.status=='completed' for row in runs),runs
                binding=await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==tenant))
                reviews=(await db.execute(select(ConversationReplyWorkRow.action,ConversationReplyWorkRow.review_reason).where(ConversationReplyWorkRow.tenant_id==tenant))).all()
                assert binding is not None, reviews
                execution=await db.scalar(select(DialogueExecutionRow).where(DialogueExecutionRow.tenant_id==tenant))
                if mode == 'off':
                    assert execution is None and len(server.sent) == 1 and binding.model_calls == 0
                    assert len(calls) == 1  # 原冷分类仍可进行；全局停发不阻断收件。
                    return
                if mode in {'invalid_plan','unknown_model'}:
                    expected = 'model_result_unknown' if mode == 'unknown_model' else 'invalid_plan'
                    assert ('review_reply', expected) in reviews, reviews
                    assert execution.state == ('unknown' if mode == 'unknown_model' else 'review')
                    assert execution.attempt_id is None and binding.model_calls == 1
                    assert len(server.sent) == 1
                    return
                assert execution is not None and execution.state=='sent', (reviews, execution.state if execution else None, failures)
                if mode == 'inquiry':
                    assert binding.enrollment_id is None
                    assert await db.scalar(select(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id==tenant)) is None
                else:
                    enrollment=await db.get(OutreachEnrollmentRow,(tenant,runtime['source_enrollment_id']))
                    assert enrollment.state=='replied'
                assert binding.owner_employee_id==sales.employee_id
            assert len(server.sent)==offset+1
            ordinary=BytesParser(policy=email_policy.default).parsebytes(server.sent[offset])
            assert ordinary['From']=='sales@qq.com' and ordinary['In-Reply-To']=='<customer-1@buyer.test>'
            assert 'Hinges for cabinet doors.' in ordinary.get_content()
            first_hypothesis = binding.hypothesis_id
            if mode == 'pending_after_plan':
                original_put = driver.plans.put
                async def receive_after_plan(*args, **kwargs):
                    result = await original_put(*args, **kwargs)
                    await receive(3, 'We need hinges for cabinet doors, 5000 units.', str(ordinary['Message-ID']))
                    return result
                driver.plans.put = receive_after_plan
            await receive(2, 'Please stop contacting me.' if mode=='stop' else 'We need hinges.', str(ordinary['Message-ID']))
            await pump()
            if mode in {'budget','stop'}:
                assert len(server.sent)==2
                async with sessions() as db:
                    current_binding = await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==tenant))
                    assert current_binding.state != 'active'
                    if mode=='budget':
                        assert current_binding.model_calls==2
                        reasons=(await db.scalars(select(ConversationReplyWorkRow.review_reason).where(ConversationReplyWorkRow.tenant_id==tenant))).all()
                        assert 'budget_exhausted' in reasons
                call_count=len(calls)
                await pump(3)
                assert len(calls)==call_count and len(server.sent)==2
                return
            if mode in {'pending_during_model', 'pending_after_plan'}:
                assert len(server.sent) == 2, failures
                from infra.db.tables import MessageRow, ValidatedNeedRow
                async with sessions() as db:
                    current_binding=await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==tenant))
                    needs=(await db.scalars(select(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==tenant))).all()
                    assert current_binding.hypothesis_id == first_hypothesis
                    assert len(needs)==1 and needs[0].quantity['value']==5000
                    second_id=await db.scalar(select(MessageRow.message_id).where(MessageRow.tenant_id==tenant,MessageRow.external_message_id=='<customer-2@buyer.test>'))
                    abandoned=await db.get(DialogueExecutionRow,(tenant,second_id))
                    assert abandoned.state=='cancelled'
                return
            assert len(server.sent) == offset+2, failures
            async with sessions() as db:
                from infra.db.tables import ValidatedNeedRow
                current_binding = await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==tenant))
                assert current_binding.binding_id == binding.binding_id and current_binding.hypothesis_id == first_hypothesis
                needs = (await db.scalars(select(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==tenant))).all()
                assert len(needs)==1
                first_need = needs[0].need_id
            if mode=='repeat':
                for uid in (3,4):
                    last=BytesParser(policy=email_policy.default).parsebytes(server.sent[-1])
                    await receive(uid,'Yes, we need hinges.',str(last['Message-ID']))
                    await pump()
                async with sessions() as db:
                    current_binding=await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==tenant))
                    assert current_binding.no_progress_rounds==2
                    needs=(await db.scalars(select(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==tenant))).all()
                    assert len(needs)==1 and needs[0].need_id==first_need
                last=BytesParser(policy=email_policy.default).parsebytes(server.sent[-1])
                await receive(5,'Yes, we need hinges.',str(last['Message-ID']))
                sent_count=len(server.sent)
                await pump()
                assert len(server.sent)==sent_count
                async with sessions() as db:
                    reasons=(await db.scalars(select(ConversationReplyWorkRow.review_reason).where(ConversationReplyWorkRow.tenant_id==tenant))).all()
                    assert 'no_progress' in reasons, reasons
                return
            third = BytesParser(policy=email_policy.default).parsebytes(server.sent[offset+1])
            await receive(3, 'We need hinges for cabinet doors, 5000 units.', str(third['Message-ID']))
            await pump()
            async with sessions() as db:
                needs = (await db.scalars(select(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==tenant))).all()
                assert len(needs)==1 and needs[0].need_id==first_need and needs[0].quantity['value']==5000
                reviews=(await db.execute(select(ConversationReplyWorkRow.action,ConversationReplyWorkRow.review_reason).where(ConversationReplyWorkRow.tenant_id==tenant))).all()
                assert ('review_reply','need_validated_intake_permission_required') in reviews, reviews
            assert len(server.sent)==offset+2
            call_count=len(calls)
            await pump(3)
            assert len(calls)==call_count and len(server.sent)==offset+2
        # 重建真实factory/resources、重新取得singleton；稳定源/attempt不会再次规划或发送。
        async with factory() as worker:
            await pump(3)
            assert len(calls)==call_count and len(server.sent)==offset+2
    finally:
        await objects.aclose()
