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
