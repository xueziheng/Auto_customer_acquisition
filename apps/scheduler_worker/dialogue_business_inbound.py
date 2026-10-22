"""本人普通业务收件的精确SENT桥；私人QQ试发单target链保持独立。"""
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.dialogue_source import DialogueSource
from domains.conversations.service import ConversationsUnitOfWork
from domains.conversations.service_impl import ConversationServiceImpl
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    SuppressionReason,
    SuppressionRequest,
    SuppressionTarget,
)
from domains.outreach.service import OutreachService
from domains.sending_identity.service import Actor as IdentityActor
from domains.sending_identity.service import (
    DeliveryEventRecord,
    DeliveryEventType,
    SendingIdentityScope,
    SendingIdentityService,
)
from domains.sending_identity.service import ScopeLevel as IdentityScope
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_tables import BusinessMailboxRow, BusinessMailRow
from infra.db.dialogue_execution_tables import (
    DialogueBindingRow,
    DialogueBusinessReceiptRow,
    DialogueExecutionRow,
)
from infra.db.dialogue_owner_access import SqlDialogueOwnerAccess
from infra.db.dialogue_source import SqlDialogueSources, source_from_row
from infra.db.email_inbound_uow import _BoundConversations
from infra.db.tables import ContactPointRow, RawArtifactRow, SendingIdentityRow
from shared.dialogue_feedback import DialogueDeliveryReport
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.business_reply import validate_reply_message_id
from shared.schemas.identifiers import (
    EmployeeId,
    IdempotencyKey,
    MessageId,
    OutboundMessageId,
    TenantId,
)


class DialogueBusinessInboundBridge:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                 mailbox: SqlBusinessMailRepository, owners: SqlDialogueOwnerAccess,
                 outreach: OutreachService, identities: SendingIdentityService, *, cold_classifier: EmployeeId | None = None) -> None:
        self.sessions, self.tenant, self.mailbox, self.owners = sessions, tenant, mailbox, owners
        self.outreach, self.identities = outreach, identities
        from .dialogue_cold_inbound import ColdBusinessInboundBridge
        self.cold = ColdBusinessInboundBridge(sessions, tenant, mailbox, cold_classifier) if cold_classifier is not None else None
        self.sources = SqlDialogueSources(sessions, tenant)

    async def scan_once(self, limit: int = 20) -> int:
        """只扫描新收取完整原件的安全元数据；没有精确已发来源不会调用模型。"""
        async with self.sessions() as db:
            rows = (await db.scalars(select(BusinessMailRow.message_id).join(DialogueExecutionRow,
                (DialogueExecutionRow.tenant_id == BusinessMailRow.tenant_id) & or_(
                    DialogueExecutionRow.deterministic_message_id == BusinessMailRow.in_reply_to,
                    DialogueExecutionRow.deterministic_message_id == BusinessMailRow.dialogue_feedback['original_message_id'].astext))
                .where(BusinessMailRow.tenant_id == self.tenant, DialogueExecutionRow.tenant_id == self.tenant,
                    BusinessMailRow.source_kind == 'full_mime', DialogueExecutionRow.state == 'sent',
                    ~exists(select(DialogueBusinessReceiptRow.business_message_id).where(
                        DialogueBusinessReceiptRow.tenant_id == self.tenant,
                        DialogueBusinessReceiptRow.business_message_id == BusinessMailRow.message_id)))
                .order_by(BusinessMailRow.ingested_at, BusinessMailRow.message_id).limit(limit))).all()
        for message_id in rows:
            try:
                await self.ingest(message_id)
            except (PermissionDenied, ValidationError):
                # 原件留在本人业务收件箱，固定可见失败，不构造任何Need或发送。
                await self._reject(message_id)
        return len(rows) + (await self.cold.scan_once(limit) if self.cold is not None else 0)

    async def _reject(self, business_id: str) -> None:
        async with self.sessions.begin() as db:
            mail = await db.get(BusinessMailRow, (str(self.tenant), business_id))
            if mail is None:
                return
            header = mail.in_reply_to or (mail.dialogue_feedback or {}).get('original_message_id')
            source = await self.sources.for_sent_header_in(db, self.tenant, str(header))
            if source is None:
                return
            mail.classification_status, mail.category, mail.failure_code = 'failed', 'review', 'ordinary_dialogue_admission_denied'
            binding = await db.get(DialogueBindingRow, (str(self.tenant), source.binding_id), with_for_update=True)
            contact = await db.get(ContactPointRow, (str(self.tenant), source.contact_point_id))
            box = await db.get(BusinessMailboxRow, (str(self.tenant), mail.mailbox_id))
            identity = await db.get(SendingIdentityRow, (str(self.tenant), source.sending_identity_id))
            # 不可信来信引用真实 SENT header 不构成暂停原客户对话的权限。
            correlated = (not mail.dialogue_feedback and contact is not None and box is not None
                and identity is not None and mail.sender == contact.value
                and mail.reply_recipient == box.address == identity.address
                and box.owner_id == source.owner_employee_id)
            if correlated and binding is not None and binding.state == 'active':
                binding.state = 'review'
            if await db.get(DialogueBusinessReceiptRow, (str(self.tenant), business_id)) is None:
                db.add(DialogueBusinessReceiptRow(tenant_id=self.tenant, business_message_id=business_id,
                    binding_id=source.binding_id, canonical_message_id=None, result='rejected', created_at=datetime.now(UTC)))

    async def ingest(self, business_id: str) -> MessageId | None:
        """当前邮箱/客户owner交集先核验；canonical Message/source/outbox/收据同事务提交。"""
        async with self.sessions() as read:
            mail = await read.get(BusinessMailRow, (str(self.tenant), business_id))
            if mail is None or mail.source_kind != 'full_mime':
                raise PermissionDenied('普通对话入站原件缺失')
            header = mail.in_reply_to or (mail.dialogue_feedback or {}).get('original_message_id')
            source = await self.sources.for_sent_header_in(read, self.tenant, str(header))
            if source is None:
                raise PermissionDenied('普通对话入站缺少SENT来源')
        # 已归档的确定性技术报告只读关联元数据；员工撤权不能撤销真实退信/投诉事实。
        # 客户正文路径仍逐轮要求当前 owner 与原邮箱权限，报告路径不读取原件字节或调用模型。
        actor = None
        if not mail.dialogue_feedback:
            owner = await self.owners.require(self.tenant, source.admitted_message_id, source.owner_employee_id)
            actor = BusinessMailActor(tenant_id=self.tenant, employee_id=source.owner_employee_id, user_id=owner.user_id)
        async with self.sessions.begin() as db:
            mail = await db.get(BusinessMailRow, (str(self.tenant), business_id), with_for_update=True)
            old = await db.get(DialogueBusinessReceiptRow, (str(self.tenant), business_id))
            if old is not None:
                return MessageId(old.canonical_message_id) if old.canonical_message_id else None
            if mail is None:
                raise PermissionDenied('普通对话入站原件缺失')
            if actor is not None:
                box = await self.mailbox._mailbox(db, actor, mail.mailbox_id)
            else:
                report_box = await db.get(BusinessMailboxRow, (str(self.tenant), mail.mailbox_id))
                if report_box is None or report_box.owner_id != source.owner_employee_id:
                    raise PermissionDenied('普通对话报告原邮箱关联无效')
                box = report_box
            binding = await db.get(DialogueBindingRow, (str(self.tenant), source.binding_id), with_for_update=True)
            contact = await db.get(ContactPointRow, (str(self.tenant), source.contact_point_id))
            identity = await db.get(SendingIdentityRow, (str(self.tenant), source.sending_identity_id))
            raw = await db.get(RawArtifactRow, (str(self.tenant), mail.artifact_id))
            execution = await db.scalar(select(DialogueExecutionRow).where(DialogueExecutionRow.tenant_id == self.tenant,
                DialogueExecutionRow.binding_id == source.binding_id, DialogueExecutionRow.deterministic_message_id == str(header),
                DialogueExecutionRow.state == 'sent'))
            if (binding is None or source_from_row(binding) != source or execution is None or contact is None or identity is None
                or box.address != identity.address or box.binding_operation_id != source.mailbox_operation_id
                or raw is None or raw.kind != 'email_raw' or raw.content_hash != mail.source_sha256 or mail.mime_sha256 != mail.source_sha256):
                raise PermissionDenied('普通对话入站来源改变')
            canonical = None
            result = 'received'
            if mail.dialogue_feedback:
                report = DialogueDeliveryReport.model_validate(mail.dialogue_feedback)
                if (report.original_message_id != execution.deterministic_message_id or not execution.idempotency_key
                    or report.original_idempotency_header != sha256(execution.idempotency_key.encode()).hexdigest()
                    or report.original_sender != box.address or report.original_recipient != contact.value):
                    raise PermissionDenied('普通对话投递报告来源不匹配')
                # 技术反馈无需自动scope继续有效；每个域 effect 稳定幂等，失败不写完成收据。
                await self._feedback(source, business_id, report)
                result = report.kind
                if report.kind != 'soft_bounce':
                    binding.state = 'stopped'
            else:
                if actor is None:
                    raise PermissionDenied('普通对话原件权限缺失')
                if mail.sender != contact.value or mail.reply_recipient != box.address or mail.in_reply_to != header:
                    raise PermissionDenied('普通对话入站双方不匹配')
                validate_reply_message_id(mail.message_header)
                conversations = ConversationServiceImpl(lambda tenant: cast(ConversationsUnitOfWork,
                    _BoundConversations(db, tenant, lambda:datetime.now(UTC))), now=lambda:datetime.now(UTC))
                canonical = await conversations.ingest_inbound(self.tenant, None, source.account_id,
                    mail.artifact_id, str(mail.message_header), mail.received_at,
                    outbound_message_id=OutboundMessageId(str(header)))
                await self.sources.bind_message(db, source.binding_id, canonical)
            if actor is not None:
                await self.mailbox.binding.check(actor, box.address, box.binding_operation_id)
            db.add(DialogueBusinessReceiptRow(tenant_id=self.tenant, business_message_id=business_id,
                binding_id=source.binding_id, canonical_message_id=canonical, result=result, created_at=datetime.now(UTC)))
            return canonical

    async def _feedback(self, source: DialogueSource, business_id: str, report: DialogueDeliveryReport) -> None:
        if report.kind == 'soft_bounce':
            return
        event = sha256(f'dialogue-report:{self.tenant}:{business_id}:{report.kind}'.encode()).hexdigest()
        identity = source.sending_identity_id
        await self.identities.record_delivery_event(self.tenant, identity, DeliveryEventRecord(
            tenant_id=self.tenant, identity_id=identity,
            event_type=DeliveryEventType.HARD_BOUNCED if report.kind == 'hard_bounce' else DeliveryEventType.COMPLAINT,
            occurred_at=report.occurred_at, dedup_key=IdempotencyKey(event), source_ref=f'dlgfb_{event[:56]}'),
            actor=IdentityActor('system:dialogue-report', SendingIdentityScope(level=IdentityScope.SYSTEM,
                allowed_identity_ids=frozenset({identity})), 'system'))
        target = SuppressionTarget(contact_point_id=source.contact_point_id)
        await self.outreach.add_suppression(self.tenant, SuppressionRequest(target=target,
            reason=SuppressionReason.HARD_BOUNCE if report.kind == 'hard_bounce' else SuppressionReason.COMPLAINT,
            source_ref=f'dlgfb_{event[:56]}', occurred_at=report.occurred_at,
            idempotency_key=IdempotencyKey(f'dialogue-report:{event}')),
            actor=Actor('system:dialogue-report', OutreachScope(level=ScopeLevel.SYSTEM,
                allowed_suppression_targets=frozenset({target.canonical_id})), 'system'))
