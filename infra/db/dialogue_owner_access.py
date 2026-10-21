"""普通对话当前owner投影；原邮箱授权仍调用原仓储同事务实现。"""
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.dialogue_access import DialogueOwnerSnapshot
from domains.conversations.inbox_access import InboxActor, InboxScope
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_tables import BusinessMailboxRow, BusinessMailRow
from infra.db.dialogue_execution_tables import (
    DialogueBindingRow,
    DialogueBusinessReceiptRow,
    DialogueExecutionRow,
)
from infra.db.dialogue_source import read_dialogue_source
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    RawArtifactRow,
    SendingIdentityRow,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, UserId


class SqlDialogueOwnerAccess:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: TenantId,
                 mailbox: SqlBusinessMailRepository) -> None:
        self.sessions, self.tenant_id, self.mailbox = sessions, tenant_id, mailbox

    async def require(self, tenant_id: TenantId, message_id: MessageId,
                      employee_id: EmployeeId) -> DialogueOwnerSnapshot:
        """每次新事务读当前事实；邮箱连接由原binding端口重核，不缓存许可。"""
        if tenant_id != self.tenant_id:
            raise PermissionDenied("普通对话原件权限拒绝")
        async with self.sessions.begin() as db:
            source = await read_dialogue_source(db, tenant_id, message_id)
            if source is None or source.owner_employee_id != employee_id:
                raise PermissionDenied("普通对话原件权限拒绝")
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant_id, EmployeeRow.employee_id == employee_id))
            if employee is None or not employee.user_id:
                raise PermissionDenied("普通对话原件权限拒绝")
            actor = BusinessMailActor(tenant_id=tenant_id, employee_id=employee_id,
                user_id=UserId(employee.user_id))
            identity = await db.get(SendingIdentityRow, (str(tenant_id), source.sending_identity_id))
            if identity is None:
                raise PermissionDenied("普通对话原件权限拒绝")
            box_id = await db.scalar(select(BusinessMailboxRow.mailbox_id).where(
                BusinessMailboxRow.tenant_id == tenant_id,
                BusinessMailboxRow.address == identity.address,
                BusinessMailboxRow.owner_id == employee_id))
            if box_id is None:
                raise PermissionDenied("普通对话原件权限拒绝")
            box = await self.mailbox._mailbox(db, actor, box_id)
            owner = await db.scalar(select(OwnershipLockRow).where(
                OwnershipLockRow.tenant_id == tenant_id,
                OwnershipLockRow.account_id == source.account_id).with_for_update(read=True))
            if (owner is None or owner.owner != employee_id
                or owner.locked_at != source.ownership_locked_at
                or box.binding_operation_id != source.mailbox_operation_id):
                raise PermissionDenied("普通对话原件权限拒绝")
            if source.business_message_id is not None:
                original = await db.get(BusinessMailRow, (str(tenant_id), source.business_message_id))
                if (original is None or original.mailbox_id != box.mailbox_id
                    or original.source_kind != "full_mime"
                    or original.source_sha256 != source.admitted_source_sha256):
                    raise PermissionDenied("普通对话原件权限拒绝")
            binding = await db.get(DialogueBindingRow, (str(tenant_id), source.binding_id))
            if binding is None:
                raise PermissionDenied("普通对话原件权限拒绝")
            contact = await db.get(ContactPointRow, (str(tenant_id), source.contact_point_id))
            if contact is None:
                raise PermissionDenied("普通对话联系人关联拒绝")
            # 独立收件提交后立即可见；不等bridge批次/模型分类，不读取客户正文。
            pending = await db.scalar(select(BusinessMailRow.message_id)
                .join(RawArtifactRow, (RawArtifactRow.tenant_id == BusinessMailRow.tenant_id)
                    & (RawArtifactRow.artifact_id == BusinessMailRow.artifact_id))
                .where(BusinessMailRow.tenant_id == tenant_id, RawArtifactRow.tenant_id == tenant_id,
                    BusinessMailRow.mailbox_id == box.mailbox_id, BusinessMailRow.source_kind == 'full_mime',
                    BusinessMailRow.sender == contact.value, BusinessMailRow.reply_recipient == box.address,
                    BusinessMailRow.message_header.is_not(None), BusinessMailRow.dialogue_feedback.is_(None),
                    RawArtifactRow.kind == 'email_raw', RawArtifactRow.content_hash == BusinessMailRow.source_sha256,
                    BusinessMailRow.mime_sha256 == BusinessMailRow.source_sha256,
                    or_(exists(select(DialogueExecutionRow.source_message_id).where(
                        DialogueExecutionRow.tenant_id == tenant_id, DialogueExecutionRow.binding_id == source.binding_id,
                        DialogueExecutionRow.state == 'sent', DialogueExecutionRow.deterministic_message_id == BusinessMailRow.in_reply_to)),
                        exists(select(OutreachMessageAttemptRow.attempt_id).where(
                            OutreachMessageAttemptRow.tenant_id == tenant_id,
                            OutreachMessageAttemptRow.enrollment_id == source.enrollment_id,
                            OutreachMessageAttemptRow.state == 'sent',
                            OutreachMessageAttemptRow.deterministic_message_id == BusinessMailRow.in_reply_to))),
                    ~exists(select(DialogueBusinessReceiptRow.business_message_id).where(
                        DialogueBusinessReceiptRow.tenant_id == tenant_id,
                        DialogueBusinessReceiptRow.business_message_id == BusinessMailRow.message_id)))
                .limit(1))
            await self.mailbox.binding.check(actor, box.address, box.binding_operation_id)
            return DialogueOwnerSnapshot(source=source,
                actor=InboxActor(tenant_id, employee_id, employee.role,
                    {"boss": InboxScope.TENANT, "manager": InboxScope.MANAGER, "sales": InboxScope.SELF}[employee.role],
                    frozenset({employee_id})), user_id=UserId(employee.user_id),
                latest_message_id=MessageId(binding.latest_message_id), binding_state=binding.state,
                rounds_sent=binding.rounds_sent, model_calls=binding.model_calls,
                no_progress_rounds=binding.no_progress_rounds, pending_inbound=pending is not None)


class DialogueOriginalAccess:
    """API只装原件授权；无自动分类或发送接口。"""
    def __init__(self, owners: SqlDialogueOwnerAccess) -> None:
        self.owners = owners

    async def require_original(self, tenant_id: TenantId, message_id: MessageId,
                               employee_id: EmployeeId) -> InboxActor:
        return (await self.owners.require(tenant_id, message_id, employee_id)).actor
