# ruff: noqa: F811 -- pytest 显式复用持久化夹具
"""首次普通询盘复用精确批准范围与真实业务邮箱原件，绝不制造冷Enrollment。"""
import hashlib
from dataclasses import asdict
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from shared.errors import PermissionDenied, PolicyViolation, ValidationError
from shared.schemas.identifiers import new_id
from shared.schemas.quote_facts import canonical_fact_hash
from tests.integration.test_dialogue_owner_access import (
    dialogue_owner_runtime,  # noqa: F401
)
from tests.integration.test_dialogue_source import (  # noqa: F401
    dialogue_source_runtime,
    owned_infrastructure,
    page_runtime,
)
from tests.integration.test_email_inbound_page import NOW


async def prepare_inquiry(dialogue_owner_runtime):
    from domains.approvals.service import ApprovalType, BlastRadius
    from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
    from domains.outreach.schemas import (
        CampaignCreateRequest,
        OrdinaryDialoguePolicyInput,
        SequenceStepRequest,
        StepIntent,
    )
    from domains.products.permissions import ProductActor, ProductRole
    from domains.sending_identity.schemas import DomainRole
    from domains.sending_identity.service import Actor as SenderActor
    from domains.sending_identity.service import (
        AuthenticationResult,
        IdentityRegisterRequest,
        SendingIdentityScope,
    )
    from domains.sending_identity.service import ScopeLevel as SenderScope
    from infra.db.inquiry_admission import SqlInquiryAdmissionTransaction
    from infra.db.tables import EmployeeRow, OutreachEnrollmentRow, RawArtifactRow
    from shared.business_mail import ArchivedBusinessMail, BusinessArchivedPage
    from tests.unit.test_product_service import _formal_product, _Products
    from tests.unit.test_product_service import _service as product_service
    from workflows.reply_qualification.inquiry_admission import (
        InquiryAdmission,
        InquiryAdmissionRequest,
    )

    runtime, ancestor, mail_actor, _, access = dialogue_owner_runtime
    deps, tenant, sessions = runtime['deps'], ancestor.tenant_id, runtime['factory']
    # 使用当前实际boss批准；被委托原邮箱owner保持sales。
    boss_staff = next(i for i in runtime['staff'] if i.role == 'manager')
    async with sessions.begin() as db:
        boss_row = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == boss_staff.employee_id))
        boss_row.role = 'boss'
    boss = Actor(boss_staff.employee_id, OutreachScope(level=ScopeLevel.TENANT), 'boss')
    sender_boss = SenderActor(boss.actor_id, SendingIdentityScope(level=SenderScope.TENANT), 'boss')
    identity = await deps.sending_identities.register(tenant, IdentityRegisterRequest('ordinary@primary.test', 'primary.test', DomainRole.PRIMARY_BUSINESS), actor=sender_boss)
    await deps.sending_identities.begin_authentication(tenant, identity, actor=sender_boss)
    await deps.sending_identities.record_authentication_result(tenant, identity, AuthenticationResult(NOW, True, True, True, (), 'controlled-auth'),
        actor=SenderActor('system:test', SendingIdentityScope(level=SenderScope.SYSTEM, allowed_identity_ids=frozenset({identity})), 'system'))
    await deps.sending_identities.start_warmup(tenant, identity, 5, actor=sender_boss)
    product = _formal_product(new_id('prd')); product.tenant_id = tenant
    products, _, _ = product_service(products=_Products([product]))
    visible = await products.get_ordinary_dialogue_view(tenant, product.product_id, actor=ProductActor('system:test', ProductRole.SYSTEM, tenant))
    policy = OrdinaryDialoguePolicyInput(owner_employee_id=mail_actor.employee_id, assessment_ref='human-confirmed-inquiry',
        reply_sender_identity_ids=(identity,), product_refs=({'product_id':product.product_id, 'customer_view_sha256':canonical_fact_hash(asdict(visible))},),
        expires_at=NOW+timedelta(days=5), max_rounds=3, max_model_calls=6, max_no_progress_rounds=2, max_input_bytes=8000, max_output_tokens=1000)
    campaign = await deps.outreach.create_campaign(tenant, CampaignCreateRequest('Ordinary replies', ('DE',), ('manufacturer',), ('hinges',),
        (ancestor.sending_identity_id,), (SequenceStepRequest(1, StepIntent.DISCOVERY, 0),), 5, 5, (), ordinary_dialogue_policy=policy), actor=boss)
    await deps.outreach.submit_campaign(tenant, campaign.campaign_id, actor=boss)
    approval = await deps.approvals.submit(tenant, ApprovalType.CAMPAIGN_BOUNDARY_CHANGE, '普通对话范围', {'version':1}, '核对普通回复',
        BlastRadius(['Campaign'], '启用普通回复', '停止', True), proposed_by_employee=mail_actor.employee_id,
        change_set_ref=f'campaign:{campaign.campaign_id}:v1')
    await deps.approvals.decide(tenant, approval, True, boss_staff.employee_id)
    await deps.outreach.activate_campaign(tenant, campaign.campaign_id, actor=boss)
    box = await access.mailbox.register(mail_actor, address='ordinary@primary.test', operation=ancestor.mailbox_operation_id)
    point = await deps.prospecting.get_contact_point(tenant, ancestor.contact_point_id)
    artifact = new_id('art'); raw = b'controlled original inquiry'; digest=hashlib.sha256(raw).hexdigest()
    async with sessions.begin() as db:
        db.add(RawArtifactRow(tenant_id=tenant, artifact_id=artifact, kind='email_raw', content_hash=digest, mime_type='message/rfc822', size_bytes=len(raw), object_key=f'raw/{tenant}/{artifact}', uploaded_by=None, uploaded_at=NOW))
    archived = ArchivedBusinessMail(uidvalidity=8, uid=1, sender=point.value, subject='Product question', body_text='We need hinges for cabinets.',
        message_header=f'<{new_id("test")}@buyer.test>', received_at=NOW, artifact_id=artifact, mime_sha256=digest, source_sha256=digest, content_sha256=hashlib.sha256(b'visible inquiry').hexdigest())
    await access.mailbox.apply_page(mail_actor, await access.mailbox.checkpoint(mail_actor, box), BusinessArchivedPage(uidvalidity=8, after_uid=0, next_uid=1, messages=(archived,)))
    mail = (await access.mailbox.list(mail_actor, bucket='all', offset=0, limit=20)).items[0]
    admission = InquiryAdmission(lambda t: SqlInquiryAdmissionTransaction(sessions, t, access.mailbox, now=lambda:NOW), deps.outreach, products, deps.prospecting)
    request = InquiryAdmissionRequest(account_id=ancestor.account_id, contact_point_id=ancestor.contact_point_id, campaign_id=campaign.campaign_id, campaign_version=1,
        product_id=product.product_id, customer_quote='We need hinges for cabinets.')
    async with sessions() as db:
        before = await db.scalar(select(func.count()).select_from(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id == tenant))
    source = await admission.admit(mail_actor, mail.message_id, request)
    assert source.source_kind == 'inquiry' and source.enrollment_id is None and source.sending_identity_id == identity
    assert await admission.admit(mail_actor, mail.message_id, request) == source
    async with sessions() as db:
        after = await db.scalar(select(func.count()).select_from(OutreachEnrollmentRow).where(OutreachEnrollmentRow.tenant_id == tenant))
    assert after == before
    return {'runtime':runtime, 'source':source, 'access':access, 'products':products,
        'actor':mail_actor, 'admission':admission, 'request':request, 'mail':mail,
        'campaign':campaign, 'boss':boss, 'boss_staff':boss_staff, 'identity':identity}


async def test_primary_business_inquiry_admission_keeps_cold_role_and_enrollment_boundary(dialogue_owner_runtime):
    from domains.conversations.business_mail import BusinessMailActor
    from domains.outreach.schemas import (
        CampaignCreateRequest,
        SequenceStepRequest,
        StepIntent,
    )
    data = await prepare_inquiry(dialogue_owner_runtime)
    deps, tenant = data['runtime']['deps'], data['source'].tenant_id
    identity, boss, boss_staff = data['identity'], data['boss'], data['boss_staff']
    admission, mail, request, campaign, mail_actor = (data[key] for key in ('admission','mail','request','campaign','actor'))
    # 同一PRIMARY_BUSINESS身份仍不可放进冷开发Campaign范围。
    with pytest.raises((ValidationError, PolicyViolation, PermissionDenied)):
        await deps.outreach.create_campaign(tenant, CampaignCreateRequest('Illegal cold', ('DE',), ('manufacturer',), ('hinges',),
            (identity,), (SequenceStepRequest(1, StepIntent.DISCOVERY, 0),), 5, 5, ()), actor=boss)
    other = BusinessMailActor(tenant_id=tenant, employee_id=boss_staff.employee_id, user_id=boss_staff.user_id)
    with pytest.raises(PermissionDenied):
        await admission.admit(other, mail.message_id, request)
    await deps.outreach.pause_campaign(tenant, campaign.campaign_id, '撤销普通对话', actor=boss)
    with pytest.raises((PermissionDenied, ValidationError, PolicyViolation)):
        await admission.admit(mail_actor, mail.message_id, request)


async def test_actual_inquiry_http_choices_and_replay_preserve_owner_gate(dialogue_owner_runtime):
    from httpx import ASGITransport, AsyncClient

    from apps.api.composition.dialogue_inquiry import OrdinaryInquiryChoices
    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from domains.conversations.business_mail import BusinessMailService
    data = await prepare_inquiry(dialogue_owner_runtime)
    runtime, actor = data['runtime'], data['actor']
    app = create_app(settings=ApiSettings(tenant_id=actor.tenant_id,dev_mode=True,retry_after_seconds=2),dependencies=runtime['deps'])
    service = BusinessMailService(data['access'].mailbox)
    app.state.business_mail = service
    app.state.ordinary_inquiry_choices = OrdinaryInquiryChoices(runtime['factory'],service,runtime['deps'].outreach,data['products'])
    app.state.ordinary_inquiry_admission = data['admission']
    base=f"/inbox/business-mail/{data['mail'].message_id}"
    headers={'X-Tenant-Id':actor.tenant_id,'X-Employee-Id':actor.employee_id}
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://controlled.test') as client:
        choices=await client.get(base+'/ordinary-dialogue-options',headers=headers)
        assert choices.status_code==200, choices.text
        assert len(choices.json()['contacts'])==1 and len(choices.json()['scopes'])==1
        assert 'customer_view_sha256' not in choices.text
        assert 'no-store' in choices.headers['cache-control']
        body=data['request'].model_dump(mode='json')
        result=await client.post(base+'/ordinary-dialogue',headers=headers,json=body)
        assert result.status_code==200 and result.json()['hypothesis_id']==data['source'].hypothesis_id
        other={**headers,'X-Employee-Id':data['boss_staff'].employee_id}
        assert (await client.get(base+'/ordinary-dialogue-options',headers=other)).status_code==403
        assert (await client.post(base+'/ordinary-dialogue',headers=other,json=body)).status_code==403
        assert (await client.post(base+'/ordinary-dialogue',headers=headers,json={**body,'approved':True})).status_code==400
