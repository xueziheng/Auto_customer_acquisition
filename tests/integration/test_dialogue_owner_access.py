# ruff: noqa: F811 -- pytest 显式复用持久化夹具
"""普通对话复用当前邮箱原件权限，不借老板租户权限或旧owner快照。"""
import pytest
import pytest_asyncio
from sqlalchemy import select

from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id
from tests.integration.test_dialogue_source import (  # noqa: F401
    dialogue_source_runtime,
    owned_infrastructure,
    page_runtime,
)


@pytest_asyncio.fixture
async def dialogue_owner_runtime(dialogue_source_runtime):
    from domains.conversations.business_mail import BusinessMailActor
    from infra.db.business_mail import SqlBusinessMailRepository
    from infra.db.dialogue_owner_access import SqlDialogueOwnerAccess
    from infra.db.tables import EmployeeRow, OwnershipLockRow, SendingIdentityRow
    from tests.integration.test_business_mail import CurrentBinding
    runtime, source, _ = dialogue_source_runtime
    sessions = runtime["factory"]
    async with sessions.begin() as db:
        owner = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == source.tenant_id, EmployeeRow.employee_id == source.owner_employee_id))
        owner.role = "sales"
        owner.user_id = new_id("usr")
        actor = BusinessMailActor(tenant_id=source.tenant_id, employee_id=owner.employee_id, user_id=owner.user_id)
        identity = await db.get(SendingIdentityRow, (source.tenant_id, source.sending_identity_id))
        address = identity.address
        lock = await db.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == source.tenant_id,
            OwnershipLockRow.account_id == source.account_id))
        assert lock is None
        db.add(OwnershipLockRow(lock_id=new_id("own"), tenant_id=source.tenant_id,
            account_id=source.account_id, owner=source.owner_employee_id,
            locked_at=source.ownership_locked_at, locked_by_rule="controlled-test"))
    current = CurrentBinding()
    current.operation = source.mailbox_operation_id
    mailbox = SqlBusinessMailRepository(sessions, current)
    await mailbox.register(actor, address=address, operation=source.mailbox_operation_id)
    access = SqlDialogueOwnerAccess(sessions, source.tenant_id, mailbox)
    return runtime, source, actor, current, access


async def test_owner_reader_rechecks_sales_owner_original_mailbox_and_account(dialogue_owner_runtime):
    from infra.db.tables import EmployeeRow
    runtime, source, actor, current, access = dialogue_owner_runtime
    sessions = runtime["factory"]
    result = await access.require(source.tenant_id, source.admitted_message_id, actor.employee_id)
    assert result.actor.role == "sales" and result.actor.employee_id == actor.employee_id
    with pytest.raises(PermissionDenied):
        await access.require(source.tenant_id, source.admitted_message_id, new_id("emp"))
    current.operation = "revoked"
    with pytest.raises(PermissionDenied):
        await access.require(source.tenant_id, source.admitted_message_id, actor.employee_id)
    current.operation = source.mailbox_operation_id
    from infra.db.tables import OwnershipLockRow
    async with sessions.begin() as db:
        lock = await db.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id==source.tenant_id,OwnershipLockRow.account_id==source.account_id))
        lock.owner = next(staff.employee_id for staff in runtime['staff'] if staff.employee_id != source.owner_employee_id)
    with pytest.raises(PermissionDenied):
        await access.require(source.tenant_id, source.admitted_message_id, actor.employee_id)
    async with sessions.begin() as db:
        lock = await db.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id==source.tenant_id,OwnershipLockRow.account_id==source.account_id))
        lock.owner = source.owner_employee_id
    async with sessions.begin() as db:
        owner = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == source.tenant_id, EmployeeRow.employee_id == source.owner_employee_id))
        owner.is_active = False
    with pytest.raises(PermissionDenied):
        await access.require(source.tenant_id, source.admitted_message_id, actor.employee_id)


async def test_ordinary_classification_uses_separate_gate_and_preserves_cold_boss_rule(dialogue_owner_runtime):
    from domains.conversations.schemas import ReplyCategory
    from domains.conversations.service_impl import ConversationServiceImpl
    from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
    runtime, source, actor, current, access = dialogue_owner_runtime
    from tests.integration.test_email_inbound_page import NOW
    uow = lambda tenant: SqlAlchemyConversationsUnitOfWork(runtime["factory"], tenant, now=lambda: NOW)

    class Gate:
        async def require_original(self, tenant, message, employee):
            return (await access.require(tenant, message, employee)).actor
        async def require(self, tenant, message, employee):
            return (await access.require(tenant, message, employee)).actor

    service = ConversationServiceImpl(uow, now=lambda: NOW, dialogue_access=Gate())
    inbox_actor = (await access.require(source.tenant_id, source.admitted_message_id, actor.employee_id)).actor
    with pytest.raises(PermissionDenied):
        await service.record_classification(source.tenant_id, source.admitted_message_id,
            ReplyCategory.CLEAR_INTEREST, "controlled", outbound_message_id=runtime["outbound"], actor=inbox_actor)
    actions = await service.record_ordinary_dialogue_classification(source.tenant_id, source.admitted_message_id,
        ReplyCategory.CLEAR_INTEREST, "controlled", outbound_message_id=runtime["outbound"], actor=inbox_actor)
    assert "start_qualification" in actions
    current.operation = "revoked"
    with pytest.raises(PermissionDenied):
        await service.record_ordinary_dialogue_classification(source.tenant_id, source.admitted_message_id,
            ReplyCategory.CLEAR_INTEREST, "controlled", outbound_message_id=runtime["outbound"], actor=inbox_actor)


async def test_paused_dialogue_keeps_owner_original_access_without_auto_scope(dialogue_owner_runtime):
    from domains.conversations.service_impl import ConversationServiceImpl
    from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
    from infra.db.dialogue_owner_access import DialogueOriginalAccess
    from tests.integration.test_email_inbound_page import NOW
    runtime, source, actor, current, access = dialogue_owner_runtime
    uow = lambda tenant: SqlAlchemyConversationsUnitOfWork(runtime['factory'], tenant, now=lambda: NOW)
    service = ConversationServiceImpl(uow, now=lambda: NOW)
    service.bind_ordinary_original_access(DialogueOriginalAccess(access))
    inbox_actor = (await access.require(source.tenant_id, source.admitted_message_id, actor.employee_id)).actor
    async with uow(source.tenant_id) as transaction:
        await transaction.pause_dialogue(source.tenant_id, source.admitted_message_id)
    evidence = await service.get_message_evidence(source.tenant_id, source.admitted_message_id, actor=inbox_actor)
    assert evidence.message_id == source.admitted_message_id
    current.operation = 'revoked'
    with pytest.raises(PermissionDenied):
        await service.get_message_evidence(source.tenant_id, source.admitted_message_id, actor=inbox_actor)


async def test_pending_inbound_fence_is_exact_and_visible_before_bridge(dialogue_owner_runtime, db_url):
    from datetime import UTC, datetime, timedelta
    from email import policy
    from email.message import EmailMessage
    from hashlib import sha256

    from sqlalchemy import func
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from connectors.qq_mail.business_inbound import content_hash, parse_business_mime
    from domains.conversations.business_mail import BusinessMailActor
    from infra.db.business_mail import SqlBusinessMailRepository
    from infra.db.business_mail_tables import BusinessMailboxRow, BusinessMailRow
    from infra.db.session import create_engine_from
    from infra.db.tables import EmployeeRow, RawArtifactRow
    from shared.business_mail import ArchivedBusinessMail, BusinessArchivedPage
    from shared.schemas.identifiers import TenantId
    from tests.runtime_database_fixtures import runtime_database_scope

    runtime, source, actor, current, access = dialogue_owner_runtime
    sessions, tenant = runtime['factory'], source.tenant_id
    contact = await runtime['deps'].prospecting.get_contact_point(tenant, source.contact_point_id)
    async with sessions() as db:
        box = await db.scalar(select(BusinessMailboxRow).where(BusinessMailboxRow.tenant_id==tenant, BusinessMailboxRow.owner_id==actor.employee_id))
    async def ingest(repo, factory, recipient_actor, mailbox_id, uid, header, *, sender=contact.value, destination=box.address, duplicate_uid=None):
        message = EmailMessage()
        message['From']=sender;message['To']=destination
        message['Message-ID']=f'<pending-{duplicate_uid or uid}@buyer.test>';message['In-Reply-To']=header
        message['Subject']='New business message';message.set_content('Please stop contacting me.')
        raw=message.as_bytes(policy=policy.SMTP)
        digest=sha256(raw).hexdigest();artifact=new_id('art')
        # Date不决定新鲜性：新提交的旧日期来信也必须成为fence。
        parsed=parse_business_mime(raw,uid=uid,uidvalidity=8,received_at=datetime.now(UTC)-timedelta(days=60))
        async with factory.begin() as db:
            old_artifact=await db.scalar(select(RawArtifactRow.artifact_id).where(
                RawArtifactRow.tenant_id==recipient_actor.tenant_id,RawArtifactRow.kind=='email_raw',RawArtifactRow.content_hash==digest))
            if old_artifact is not None:
                artifact=old_artifact
            else:
                db.add(RawArtifactRow(tenant_id=recipient_actor.tenant_id,artifact_id=artifact,kind='email_raw',
                    content_hash=digest,mime_type='message/rfc822',size_bytes=len(raw),
                    object_key=f'raw/{recipient_actor.tenant_id}/{artifact}',uploaded_at=datetime.now(UTC)))
        item=ArchivedBusinessMail(uid=uid,uidvalidity=8,sender=parsed.sender,subject=parsed.subject,
            body_text=parsed.body_text,message_header=parsed.message_header,in_reply_to=parsed.in_reply_to,
            reply_recipient=parsed.reply_recipient,received_at=parsed.received_at,artifact_id=artifact,
            mime_sha256=digest,source_sha256=digest,content_sha256=content_hash(parsed))
        checkpoint=await repo.checkpoint(recipient_actor,mailbox_id)
        await repo.apply_page(recipient_actor,checkpoint,BusinessArchivedPage(uidvalidity=8,after_uid=checkpoint.after_uid,next_uid=uid,messages=(item,)))
    async def pending():
        return (await access.require(tenant,source.admitted_message_id,actor.employee_id)).pending_inbound
    assert not await pending()
    await ingest(access.mailbox,sessions,actor,box.mailbox_id,1,'<different-thread@buyer.test>')
    assert not await pending()
    other_box=await access.mailbox.register(actor,address='other@example.test',operation=current.operation)
    await ingest(access.mailbox,sessions,actor,other_box,1,runtime['outbound'])
    assert not await pending()
    await ingest(access.mailbox,sessions,actor,box.mailbox_id,2,runtime['outbound'],sender='unrelated@example.test')
    assert not await pending()
    foreign=BusinessMailActor(tenant_id=TenantId(new_id('tn')),employee_id=new_id('emp'),user_id=new_id('usr'))
    async with runtime_database_scope(db_url) as provision:
        engine=create_engine_from(await provision(foreign.tenant_id))
        foreign_sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with foreign_sessions.begin() as db:
                db.add(EmployeeRow(tenant_id=foreign.tenant_id,employee_id=foreign.employee_id,user_id=foreign.user_id,name='合成员工',role='sales',is_active=True))
            repo=SqlBusinessMailRepository(foreign_sessions,current)
            foreign_box=await repo.register(foreign,address=box.address,operation=current.operation)
            await ingest(repo,foreign_sessions,foreign,foreign_box,1,runtime['outbound'])
            assert not await pending()
            with pytest.raises(PermissionDenied):
                await access.require(foreign.tenant_id,source.admitted_message_id,actor.employee_id)
        finally:
            await engine.dispose()
    await ingest(access.mailbox,sessions,actor,box.mailbox_id,3,runtime['outbound'])
    assert await pending()
    # 原件人工阅读权限不因pending被撤销；仅自动范围读取这个fence。
    before=await access.mailbox.detail(actor,(await access.mailbox.list(actor,bucket='all',offset=0,limit=20)).items[0].message_id)
    assert before is not None
    async with sessions() as db:
        count=await db.scalar(select(func.count()).select_from(BusinessMailRow).where(BusinessMailRow.tenant_id==tenant))
    # 相同原件的新UID在原收件幂等边界去重，不能制造第二消息或清掉fence。
    await ingest(access.mailbox,sessions,actor,box.mailbox_id,4,runtime['outbound'],duplicate_uid=3)
    assert await pending()
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessMailRow).where(BusinessMailRow.tenant_id==tenant))==count
