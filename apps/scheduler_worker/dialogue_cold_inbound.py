"""正式本人业务收件到原冷回复流程的精确桥，不扩大私人测试收件范围。"""
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.service import (
    ConversationsUnitOfWork,
    require_reply_internal_access,
)
from domains.conversations.service_impl import ConversationServiceImpl
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_tables import BusinessMailboxRow, BusinessMailRow
from infra.db.dialogue_execution_tables import DialogueBusinessReceiptRow
from infra.db.email_inbound_uow import _BoundConversations
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    MessageRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    RawArtifactRow,
    SendingIdentityRow,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.business_reply import validate_reply_message_id
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    UserId,
)
from shared.schemas.quote_facts import QuoteEmployeeFact


class ColdBusinessInboundBridge:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                 mailbox: SqlBusinessMailRepository, classifier: EmployeeId) -> None:
        self.sessions, self.tenant, self.mailbox, self.classifier = sessions, tenant, mailbox, classifier

    async def scan_once(self, limit: int) -> int:
        async with self.sessions() as db:
            rows = (await db.scalars(select(BusinessMailRow.message_id).join(OutreachMessageAttemptRow,
                (OutreachMessageAttemptRow.tenant_id == BusinessMailRow.tenant_id)
                & (OutreachMessageAttemptRow.deterministic_message_id == BusinessMailRow.in_reply_to))
                .where(BusinessMailRow.tenant_id == self.tenant, OutreachMessageAttemptRow.tenant_id == self.tenant,
                    OutreachMessageAttemptRow.state == 'sent', BusinessMailRow.source_kind == 'full_mime',
                    BusinessMailRow.dialogue_feedback.is_(None),
                    ~exists(select(DialogueBusinessReceiptRow.business_message_id).where(
                        DialogueBusinessReceiptRow.tenant_id == self.tenant,
                        DialogueBusinessReceiptRow.business_message_id == BusinessMailRow.message_id)))
                .order_by(BusinessMailRow.ingested_at, BusinessMailRow.message_id).limit(limit))).all()
        for message in rows:
            try:
                await self.ingest(message)
            except (PermissionDenied, ValidationError):
                async with self.sessions.begin() as db:
                    mail = await db.get(BusinessMailRow, (str(self.tenant), message), with_for_update=True)
                    if mail is not None:
                        mail.classification_status, mail.category, mail.failure_code = 'failed', 'review', 'cold_reply_admission_denied'
                        attempt = await db.scalar(select(OutreachMessageAttemptRow).where(OutreachMessageAttemptRow.tenant_id == self.tenant,
                            OutreachMessageAttemptRow.deterministic_message_id == mail.in_reply_to, OutreachMessageAttemptRow.state == 'sent'))
                        if attempt and await db.get(DialogueBusinessReceiptRow, (str(self.tenant), message)) is None:
                            db.add(DialogueBusinessReceiptRow(tenant_id=self.tenant, business_message_id=message,
                                cold_attempt_id=attempt.attempt_id, binding_id=None, canonical_message_id=None,
                                result='rejected', created_at=datetime.now(UTC)))
        return len(rows)

    async def ingest(self, business_id: str) -> MessageId:
        async with self.sessions.begin() as db:
            mail = await db.get(BusinessMailRow, (str(self.tenant), business_id), with_for_update=True)
            old = await db.get(DialogueBusinessReceiptRow, (str(self.tenant), business_id))
            if old and old.canonical_message_id:
                return MessageId(old.canonical_message_id)
            if old or mail is None or mail.source_kind != 'full_mime' or mail.dialogue_feedback:
                raise PermissionDenied('冷回复原件不可准入')
            classifier = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == self.tenant, EmployeeRow.employee_id == self.classifier))
            if classifier is None:
                raise PermissionDenied('冷回复分类员工缺失')
            require_reply_internal_access(self.tenant, QuoteEmployeeFact.model_validate({
                'tenant_id': self.tenant, 'employee_id': classifier.employee_id, 'role': classifier.role,
                'is_active': classifier.is_active, 'manager_id': classifier.manager_id, 'team_id': classifier.team_id}), action='qualify')
            box = await db.get(BusinessMailboxRow, (str(self.tenant), mail.mailbox_id))
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == self.tenant, EmployeeRow.employee_id == box.owner_id)) if box else None
            if box is None or employee is None or not employee.user_id:
                raise PermissionDenied('冷回复当前邮箱负责人缺失')
            actor = BusinessMailActor(tenant_id=self.tenant, employee_id=EmployeeId(employee.employee_id), user_id=UserId(employee.user_id))
            box = await self.mailbox._mailbox(db, actor, mail.mailbox_id)
            attempt = await db.scalar(select(OutreachMessageAttemptRow).where(OutreachMessageAttemptRow.tenant_id == self.tenant,
                OutreachMessageAttemptRow.deterministic_message_id == mail.in_reply_to, OutreachMessageAttemptRow.state == 'sent'))
            enrollment = await db.get(OutreachEnrollmentRow, (str(self.tenant), attempt.enrollment_id)) if attempt else None
            if enrollment is None or attempt is None:
                raise PermissionDenied('冷回复缺少唯一SENT来源')
            identity = await db.get(SendingIdentityRow, (str(self.tenant), enrollment.sending_identity_id))
            contact = await db.get(ContactPointRow, (str(self.tenant), enrollment.contact_point_id))
            owner = await db.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == self.tenant,
                OwnershipLockRow.account_id == enrollment.account_id).with_for_update(read=True))
            raw = await db.get(RawArtifactRow, (str(self.tenant), mail.artifact_id))
            if (identity is None or contact is None or owner is None or owner.owner != employee.employee_id
                or identity.address != box.address or mail.reply_recipient != box.address or mail.sender != contact.value
                or raw is None or raw.kind != 'email_raw' or raw.content_hash != mail.source_sha256
                or mail.mime_sha256 != mail.source_sha256):
                raise PermissionDenied('冷回复原件双方或当前归属不匹配')
            validate_reply_message_id(mail.message_header)
            existing = await db.scalar(select(MessageRow).where(MessageRow.tenant_id == self.tenant,
                MessageRow.external_message_id == mail.message_header))
            if existing is not None and (existing.outbound_message_id != mail.in_reply_to
                or existing.raw_artifact_ref != mail.artifact_id):
                raise PermissionDenied('冷回复Message-ID冲突')
            conversations = ConversationServiceImpl(lambda tenant: cast(ConversationsUnitOfWork,
                _BoundConversations(db, tenant, lambda: datetime.now(UTC))), now=lambda: datetime.now(UTC))
            canonical = await conversations.ingest_inbound(self.tenant, None, ProspectAccountId(enrollment.account_id),
                mail.artifact_id, str(mail.message_header), mail.received_at, outbound_message_id=OutboundMessageId(str(mail.in_reply_to)))
            await self.mailbox.binding.check(actor, box.address, box.binding_operation_id)
            db.add(DialogueBusinessReceiptRow(tenant_id=self.tenant, business_message_id=business_id,
                binding_id=None, cold_attempt_id=attempt.attempt_id, canonical_message_id=canonical,
                result='received', created_at=datetime.now(UTC)))
            return canonical
