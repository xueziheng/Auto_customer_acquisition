"""合成同 QQ 全自动链；仅替换模型、对象传输和邮箱协议外部边界。"""
import asyncio
import json
from datetime import timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest
from sqlalchemy import func, select

from apps.scheduler_worker.dialogue_resources import DialogueArtifactPorts
from apps.scheduler_worker.standalone_reply import bind_reply
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.qq_mail.business_inbound import content_hash, parse_business_mime
from connectors.qq_mail.client import QQMailConnector
from domains.conversations.aliyun_connection import AliyunActor
from domains.conversations.business_mail import BusinessMailActor
from infra.aliyun_mail_store import AliyunBinding, AliyunMailStore
from infra.business_mail_binding import CurrentBusinessMailboxBinding
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.campaign_mail_owner import CampaignMailboxOwner
from infra.db.dialogue_execution_tables import DialogueBindingRow, DialogueExecutionRow
from infra.db.products_uow import SqlAlchemyProductsUnitOfWork
from infra.db.tables import (
    EmployeeRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    ValidatedNeedRow,
)
from infra.outbound_mail import AliyunOutboundConnectors, EmployeeMailConnectors
from shared.business_mail import ArchivedBusinessMail, BusinessArchivedPage
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import ModelResponse, ModelUsage
from tests.unit.test_aliyun_mail_connector import PASSWORD, MailServer
from tests.unit.test_product_service import _formal_product


class AutomaticMailScenario:
    completed = False
    suppression_phase = None
    suppression_target = "contact_point_id"
    suppression_id = None
    async def setup(self, *, sessions, tenant, boss, sales, api, objects, artifacts, now,
                    settings, fingerprints, env, tmp_path, monkeypatch, enabled=True):
        self.sessions, self.tenant, self.boss, self.sales = sessions, tenant, boss, sales
        self.now, self.artifacts, self.fingerprints, self.enabled = now, artifacts, fingerprints, enabled
        self.root = tmp_path/'automatic-qq'; self.root.mkdir(mode=0o700)
        self.server = MailServer()
        monkeypatch.setattr('connectors.qq_mail.client.QQMailTransport',lambda:self.server)
        async def close(): pass
        objects.aclose = close
        monkeypatch.setattr('apps.scheduler_worker.dialogue_resources.DeferredS3ObjectBlobTransport',lambda *a:objects)
        monkeypatch.setattr('apps.scheduler_worker.dialogue_resources.S3BoundedObjectBlobTransport',lambda *a,**k:objects)
        user = new_id('usr')
        async with sessions.begin() as db:
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id==tenant,EmployeeRow.employee_id==sales)); employee.user_id=user
        actor = AliyunActor(tenant_id=tenant,employee_id=sales,user_id=user)
        operation = new_id('aco')
        store = AliyunMailStore(self.root)
        store.publish(AliyunBinding(actor=actor,email='sales@qq.com',password=PASSWORD,connected_at=now,operation_id=operation))
        self.mailboxes = SqlBusinessMailRepository(sessions,CurrentBusinessMailboxBinding(store))
        self.actor = BusinessMailActor(**actor.model_dump())
        self.mailbox = await self.mailboxes.register(self.actor,address='sales@qq.com',operation=operation)
        from tests.integration.test_customer_preparation_runtime import seed_sender
        self.sender = await seed_sender(api.sending_identities,tenant,boss,now,address='sales@qq.com')
        self.product = _formal_product(new_id('prd')); self.product.tenant_id=tenant; self.product.supplier_id=None
        self.product.spec_summary='Hinges for cabinet doors.'
        self.product.internal_cost=None; self.product.internal_cost_basis=None;self.product.internal_cost_unit=None;self.product.internal_cost_source_ref=None;self.product.match_specs={}
        async with SqlAlchemyProductsUnitOfWork(sessions,tenant) as uow:
            await uow.products.add(tenant,self.product)
        self.settings = settings.model_copy(update={'reply_enabled':True,'automatic_send_enabled':enabled,'reply_classifier_employee_id':boss})
        self.ports = DialogueArtifactPorts(S3ObjectStoreSettings.from_environ(env),self)
        self.calls=[]
        scenario=self
        class Provider:
            def __init__(self,budget=None): self.budget=budget
            async def generate(self,request):
                async def dispatch(body): scenario.calls.append(request); return {}
                if self.budget:
                    await self.budget.run({'model':request.model,'max_output_tokens':request.max_output_tokens,'messages':[request.system_prompt,request.payload]},dispatch)
                else: await dispatch({})
                if 'allowed_question_topics' in request.payload:
                    result={'kind':'ordinary','facts':[{'product_id':str(scenario.product.product_id),'section':'spec_display'}],'question_topics':[]}
                elif '5000' in request.payload.get('body',''):
                    result={'category':'provides_specification','candidate_fields':[
                        {'field':'product_category','value':'hinges','quote':'hinges'},
                        {'field':'application','value':'cabinet doors','quote':'cabinet doors'},
                        {'field':'quantity','value':'5000','quote':'5000 units'}]}
                else: result={'category':'requests_materials','candidate_fields':[]}
                return ModelResponse(model=request.model,text=json.dumps(result),usage=ModelUsage(input_tokens=10,output_tokens=20,cached_input_tokens=0))
            async def aclose(self): pass
        self.provider=Provider
        monkeypatch.setattr('apps.scheduler_worker.dialogue_models.configured_codex_provider',lambda settings,identity,round_budget:Provider(round_budget))
        self.reply_composition=None

    def resolve(self,ref): return 'synthetic-key-only'

    def outbound(self,sessions):
        store=AliyunMailStore(self.root)
        return EmployeeMailConnectors(None,None,AliyunOutboundConnectors(store,sessions,connector_factory=QQMailConnector)),CampaignMailboxOwner(sessions,store)

    def reply(self,core,outreach,resources):
        result=bind_reply(core,outreach,resources,tenant_id=self.tenant,employee_id=self.boss,
            settings=self.settings,resolver=self,fingerprints=self.fingerprints,owner='automatic-qq-integration',
            mailbox_directory=self.root,provider_factory=self.provider)
        admission = result.dialogue_driver.admission
        original_defer = admission.defer_completed_materials
        self.deferred = 0
        self.business_checks = {"defer": 0, "admit": 0}
        original_admit = admission.admit
        async def checked_admit(message_id, enrollment_id):
            from infra.db.tables import WorkflowRunRow
            async with self.sessions() as db:
                completed = await db.scalar(select(WorkflowRunRow.run_id).where(
                    WorkflowRunRow.tenant_id == self.tenant, WorkflowRunRow.subject_ref == message_id,
                    WorkflowRunRow.workflow_type == 'reply_qualification', WorkflowRunRow.status == 'completed'))
                bound = await db.scalar(select(DialogueBindingRow.binding_id).where(DialogueBindingRow.tenant_id == self.tenant))
            if completed and not bound:
                if self.suppression_phase == 'admit':
                    return await self.reject_suppressed(original_admit, message_id, enrollment_id)
                if self.suppression_phase is None:
                    await self.reject_current_business_facts(original_admit, message_id, enrollment_id, 'admit')
            return await original_admit(message_id, enrollment_id)
        admission.admit = checked_admit
        async def checked_defer(message_id, enrollment_id):
            if self.suppression_phase == 'defer':
                return await self.reject_suppressed(original_defer, message_id, enrollment_id)
            if self.suppression_phase is None:
                await self.reject_current_business_facts(original_defer, message_id, enrollment_id, 'defer')
            accepted = await original_defer(message_id, enrollment_id)
            async with self.sessions() as db:
                assert await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==self.tenant)) is None
                assert await db.scalar(select(DialogueExecutionRow).where(DialogueExecutionRow.tenant_id==self.tenant)) is None
            assert len(self.server.sent) == 1 and len(self.calls) == 1
            self.deferred += 1
            return accepted
        admission.defer_completed_materials = checked_defer
        self.reply_composition=result
        return result

    def selection(self):
        return {'owner_employee_id': self.sales,'assessment_ref': 'synthetic-approved-ordinary',
            'reply_sender_identity_ids': [self.sender],'product_ids': [self.product.product_id],
            'expires_at': (self.now+timedelta(days=2)).isoformat(),'max_rounds': 6}

    async def pump(self,worker,cycles=12):
        from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
        stop=asyncio.Event(); count=0
        async def wait(interval,event):
            nonlocal count
            count+=1
            if count>=cycles:event.set()
        result=await run_scheduler_worker(worker,stop_event=stop,wait=wait,install_signal_handlers=False)
        assert result.status is WorkerStartStatus.STARTED

    async def receive(self,uid,body,header,buyer):
        from artifact_store.store import RawArtifactKind
        message=EmailMessage(); message['From']=buyer;message['To']='sales@qq.com'
        message['Message-ID']=f'<automatic-{uid}@buyer.test>';message['In-Reply-To']=header
        message['Subject']='Re: product';message.set_content(body)
        raw=message.as_bytes(policy=policy.SMTP)
        parsed=parse_business_mime(raw,uid=uid,uidvalidity=18,received_at=self.now)
        meta=await self.artifacts.put(self.tenant,RawArtifactKind.EMAIL_RAW,raw,'message/rfc822')
        item=ArchivedBusinessMail(uid=uid,uidvalidity=18,sender=parsed.sender,subject=parsed.subject,body_text=parsed.body_text,
            message_header=parsed.message_header,in_reply_to=parsed.in_reply_to,reply_recipient=parsed.reply_recipient,
            received_at=self.now,artifact_id=meta.artifact_id,mime_sha256=meta.content_hash,source_sha256=meta.content_hash,content_sha256=content_hash(parsed))
        await self.mailboxes.apply_page(self.actor,await self.mailboxes.checkpoint(self.actor,self.mailbox),
            BusinessArchivedPage(uidvalidity=18,after_uid=uid-1,next_uid=uid,messages=(item,)))

    async def reject_current_business_facts(self, invoke, message_id, enrollment_id, phase):
        """真实running/完成流程在创建binding前仍重读当前商业事实。"""
        from infra.db.tables import (
            ContactPointRow,
            ConversationReplyWorkRow,
            ProductRow,
        )
        from shared.errors import PermissionDenied
        async with self.sessions() as db:
            enrollment = await db.get(OutreachEnrollmentRow, (self.tenant,enrollment_id))
        async def counters():
            async with self.sessions() as db:
                counts = tuple([await db.scalar(select(func.count()).select_from(row).where(row.tenant_id == self.tenant))
                    for row in (DialogueBindingRow, DialogueExecutionRow, ConversationReplyWorkRow)])
            return counts, len(self.calls), len(self.server.sent)
        before = await counters()
        assert before[0][0] == 0 and before[0][1] == 0
        changes = [
            (ContactPointRow, (self.tenant,enrollment.contact_point_id), {'verification_status':'risky', 'verified_at':None}),
            (ProductRow, (self.tenant,self.product.product_id), {'name_en':'Changed current public facts'}),
            (ProductRow, (self.tenant,self.product.product_id), {'pool':'candidate','candidate_status':'source_only'}),
        ]
        for model,key,values in changes:
            async with self.sessions.begin() as db:
                row = await db.get(model,key); originals = {field:getattr(row,field) for field in values}
                for field,value in values.items(): setattr(row,field,value)
            try:
                with pytest.raises(PermissionDenied): await invoke(message_id,enrollment_id)
                assert await counters() == before
                self.business_checks[phase] += 1
            finally:
                async with self.sessions.begin() as db:
                    row = await db.get(model,key)
                    for field,value in originals.items(): setattr(row,field,value)
    async def reject_suppressed(self, invoke, message_id, enrollment_id):
        """抑制记录只追加且不清除，后续整条业务链必须保持拒绝。"""
        from infra.db.tables import ConversationReplyWorkRow, OutreachSuppressionRow
        from shared.errors import PermissionDenied
        async with self.sessions.begin() as db:
            enrollment = await db.get(OutreachEnrollmentRow, (self.tenant,enrollment_id))
            if self.suppression_id is None:
                self.suppression_id = new_id('sup')
                db.add(OutreachSuppressionRow(tenant_id=self.tenant,suppression_id=self.suppression_id,
                    contact_point_id=enrollment.contact_point_id if self.suppression_target == 'contact_point_id' else None,
                    account_id=enrollment.account_id if self.suppression_target == 'account_id' else None,
                    reason='manual_block',occurred_at=self.now,created_at=self.now,
                    source_ref='synthetic-current-facts',idempotency_key=self.suppression_id))
        async def counters():
            async with self.sessions() as db:
                counts = tuple([await db.scalar(select(func.count()).select_from(row).where(row.tenant_id == self.tenant))
                    for row in (DialogueBindingRow, DialogueExecutionRow, ConversationReplyWorkRow)])
            return counts, len(self.calls), len(self.server.sent)
        before = await counters()
        assert before[0][0] == 0 and before[0][1] == 0
        with pytest.raises(PermissionDenied): await invoke(message_id,enrollment_id)
        assert await counters() == before
        raise PermissionDenied('合成当前抑制拒绝')

    async def reject_missing_evidence(self, binding):
        """直接调用admit亦重读真实持久证据；不依赖scan替它判权。"""
        from infra.db.dialogue_execution_tables import DialogueBusinessReceiptRow
        from infra.db.tables import ConversationClassificationRow, WorkflowRunRow
        from shared.errors import PermissionDenied
        admission = self.reply_composition.dialogue_driver.admission
        async with self.sessions() as db:
            flow = await db.scalar(select(WorkflowRunRow).where(WorkflowRunRow.tenant_id==self.tenant, WorkflowRunRow.subject_ref==binding.admitted_message_id, WorkflowRunRow.workflow_type=='reply_qualification'))
            receipt = await db.scalar(select(DialogueBusinessReceiptRow).where(DialogueBusinessReceiptRow.tenant_id==self.tenant, DialogueBusinessReceiptRow.canonical_message_id==binding.admitted_message_id))
        cases = [
            (ConversationClassificationRow, (self.tenant,binding.admitted_message_id), 'category', 'auto_reply'),
            (WorkflowRunRow, flow.run_id, 'status', 'running'),
            (DialogueBusinessReceiptRow, (self.tenant,receipt.business_message_id), 'canonical_message_id', None),
            (OutreachEnrollmentRow, (self.tenant,binding.enrollment_id), 'state', 'stopped_manual'),
        ]
        before = (len(self.calls),len(self.server.sent))
        for model,key,field,value in cases:
            async with self.sessions.begin() as db:
                row = await db.get(model,key); original = getattr(row,field); setattr(row,field,value)
                original_reason = row.stop_reason if model is OutreachEnrollmentRow else None
                if model is OutreachEnrollmentRow: row.stop_reason = 'manual'
            try:
                with pytest.raises(PermissionDenied):
                    await admission.admit(binding.admitted_message_id,binding.enrollment_id)
            finally:
                async with self.sessions.begin() as db:
                    row = await db.get(model,key); setattr(row,field,original)
                    if model is OutreachEnrollmentRow: row.stop_reason = original_reason
        assert (len(self.calls),len(self.server.sent)) == before

    async def verify(self,worker):
        await self.pump(worker)
        async with self.sessions() as db:
            enrollment=await db.scalar(select(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id==self.tenant))
            if enrollment is None:
                from infra.db.prospect_inventory_tables import (
                    CustomerPreparationItemRow,
                )
                items = (await db.scalars(select(CustomerPreparationItemRow).where(CustomerPreparationItemRow.tenant_id == self.tenant))).all()
                pytest.fail(str([(item.state, item.stage, item.blocked_reason) for item in items]))
            attempts=(await db.scalars(select(OutreachMessageAttemptRow).where(OutreachMessageAttemptRow.tenant_id==self.tenant))).all()
        if not self.enabled:
            assert self.server.sent == [] and attempts == [] and not self.calls
            return
        assert len(self.server.sent)==1,[(a.state,a.last_error if hasattr(a,'last_error') else None) for a in attempts]
        assert len(attempts)==1 and attempts[0].state=='sent'
        cold=BytesParser(policy=policy.default).parsebytes(self.server.sent[0])
        assert cold['From']=='sales@qq.com' and cold['In-Reply-To'] is None
        assert cold['Message-ID']==attempts[0].deterministic_message_id
        assert self.reply_composition is not None
        await self.receive(1,'Please introduce this product.',str(cold['Message-ID']),str(cold['To']))
        await self.pump(worker)
        async with self.sessions() as db:
            binding=await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==self.tenant))
            execution=await db.scalar(select(DialogueExecutionRow).where(DialogueExecutionRow.tenant_id==self.tenant))
            current=await db.get(OutreachEnrollmentRow,(self.tenant,enrollment.enrollment_id))
            assert current.state==('completed' if self.completed else 'replied'), (current.state, binding.state if binding else None, execution.state if execution else None, len(self.calls), len(self.server.sent))
            if self.suppression_phase:
                from infra.db.tables import ConversationReplyWorkRow
                assert self.suppression_id is not None and binding is None and execution is None
                assert len(self.server.sent) == 1 and len(self.calls) == 1
                if self.suppression_phase == 'defer':
                    assert await db.scalar(select(func.count()).select_from(ConversationReplyWorkRow).where(
                        ConversationReplyWorkRow.tenant_id == self.tenant,
                        ConversationReplyWorkRow.action == 'create_follow_up')) == 0
                return
            assert binding is not None and binding.owner_employee_id==self.sales
            assert self.business_checks == {"defer": 3, "admit": 3}
            assert execution is not None and execution.state=='sent'
            hypothesis=binding.hypothesis_id
        assert len(self.server.sent)==2 and self.deferred == 1
        await self.reject_missing_evidence(binding)
        reply=BytesParser(policy=policy.default).parsebytes(self.server.sent[1])
        assert reply['From']=='sales@qq.com' and reply['In-Reply-To']=='<automatic-1@buyer.test>'
        assert 'Hinges for cabinet doors.' in reply.get_content()
        await self.receive(2,'We need hinges for cabinet doors, 5000 units.',str(reply['Message-ID']),str(cold['To']))
        await self.pump(worker)
        async with self.sessions() as db:
            current=await db.get(DialogueBindingRow,(self.tenant,binding.binding_id))
            needs=(await db.scalars(select(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==self.tenant))).all()
            assert current.hypothesis_id==hypothesis and len(needs)==1 and needs[0].quantity['value']==5000
        assert len(self.server.sent)==2
