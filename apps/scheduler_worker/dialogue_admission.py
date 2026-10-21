"""冷序列首封真实回复完成分类停序列后，接入独立获批普通对话范围。"""
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.dialogue_source import DialogueSource
from domains.conversations.schemas import ReplyWorkAction, ReplyWorkActionRequest
from domains.conversations.service import ConversationService
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.service import OutreachService
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_tables import BusinessMailboxRow
from infra.db.dialogue_execution_tables import DialogueBindingRow, DialogueMessageRow
from infra.db.dialogue_source import SqlDialogueSources, source_from_row
from infra.db.tables import (
    ConversationReplyWorkRow,
    EmployeeRow,
    MessageRow,
    OutreachCampaignVersionRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    RawArtifactRow,
    SendingIdentityRow,
    WorkflowRunRow,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageId,
    NeedHypothesisId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from workflows.reply_qualification.dialogue_authority import (
    CurrentOrdinaryDialogueAccess,
)


class ColdReplyDialogueAdmission:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant: TenantId,
                 outreach: OutreachService, conversations: ConversationService,
                 mailbox: SqlBusinessMailRepository, access: CurrentOrdinaryDialogueAccess,
                 now: Callable[[], datetime]) -> None:
        self.sessions, self.tenant, self.outreach, self.conversations = sessions, tenant, outreach, conversations
        self.mailbox, self.access, self.now = mailbox, access, now
        self.sources = SqlDialogueSources(sessions, tenant)

    async def scan_once(self, limit: int) -> None:
        """只处理真实已完成回复流程；legacy无scope保留原人工待办。"""
        review = aliased(ConversationReplyWorkRow)
        async with self.sessions() as db:
            rows = (await db.scalars(select(ConversationReplyWorkRow)
                .join(OutreachEnrollmentRow, (OutreachEnrollmentRow.tenant_id==ConversationReplyWorkRow.tenant_id)
                    & (OutreachEnrollmentRow.enrollment_id==ConversationReplyWorkRow.enrollment_id))
                .join(OutreachCampaignVersionRow, (OutreachCampaignVersionRow.tenant_id==OutreachEnrollmentRow.tenant_id)
                    & (OutreachCampaignVersionRow.campaign_id==OutreachEnrollmentRow.campaign_id)
                    & (OutreachCampaignVersionRow.version==OutreachEnrollmentRow.campaign_version))
                .where(ConversationReplyWorkRow.tenant_id==self.tenant, OutreachEnrollmentRow.tenant_id==self.tenant,
                    OutreachCampaignVersionRow.tenant_id==self.tenant,
                    OutreachCampaignVersionRow.ordinary_dialogue_policy.is_not(None),
                    ConversationReplyWorkRow.status=='pending', ConversationReplyWorkRow.action.in_(('start_qualification','create_follow_up')),
                    exists(select(WorkflowRunRow.run_id).where(WorkflowRunRow.tenant_id==self.tenant,
                        WorkflowRunRow.subject_ref==ConversationReplyWorkRow.message_id,
                        WorkflowRunRow.workflow_type=='reply_qualification', WorkflowRunRow.status=='completed')),
                    ~exists(select(DialogueMessageRow.message_id).where(DialogueMessageRow.tenant_id==self.tenant,
                        DialogueMessageRow.message_id==ConversationReplyWorkRow.message_id)),
                    ~exists(select(review.action_id).where(review.tenant_id==self.tenant,
                        review.message_id==ConversationReplyWorkRow.message_id, review.action=='review_reply')))
                .order_by(ConversationReplyWorkRow.created_at).limit(limit))).all()
        for work in rows:
            try:
                source = await self.admit(MessageId(work.message_id), str(work.enrollment_id))
                await self.access.require(self.tenant, MessageId(work.message_id), source.owner_employee_id)
            except Exception:  # noqa: BLE001 -- 真实准入缺条件明确留给当前客户owner，不静默吞首回信。
                bound_source = await self.sources.for_message(self.tenant, MessageId(work.message_id))
                await self.conversations.enqueue_reply_work_action(self.tenant, ReplyWorkActionRequest(
                    message_id=MessageId(work.message_id), outbound_message_id=OutboundMessageId(work.outbound_message_id) if work.outbound_message_id else None,
                    enrollment_id=EnrollmentId(work.enrollment_id) if work.enrollment_id else None, account_id=ProspectAccountId(work.account_id), contact_point_id=ContactPointId(work.contact_point_id),
                    dialogue_binding_id=bound_source.binding_id if bound_source else None,
                    action=ReplyWorkAction.REVIEW_REPLY, review_reason='scope_changed',
                    idempotency_key=IdempotencyKey(f'reply:review_reply:{work.message_id}')))

    async def admit(self, message_id: MessageId, enrollment_id: str) -> DialogueSource:
        async with self.sessions.begin() as db:
            enrollment = await db.get(OutreachEnrollmentRow, (str(self.tenant), enrollment_id), with_for_update=True)
            message = await db.get(MessageRow, (str(self.tenant), str(message_id)))
            if enrollment is None or message is None or enrollment.state!='replied' or not enrollment.source_hypothesis_id:
                raise PermissionDenied('冷回复尚未完成真实停序列')
            sent = await db.scalar(select(OutreachMessageAttemptRow).where(OutreachMessageAttemptRow.tenant_id==self.tenant,
                OutreachMessageAttemptRow.enrollment_id==enrollment_id,
                OutreachMessageAttemptRow.deterministic_message_id==message.outbound_message_id,
                OutreachMessageAttemptRow.state=='sent'))
            version = await db.get(OutreachCampaignVersionRow, (str(self.tenant), enrollment.campaign_id, enrollment.campaign_version))
            policy = version.ordinary_dialogue_policy if version else None
            if sent is None or not isinstance(policy,dict) or not isinstance(policy.get('owner_employee_id'),str):
                raise PermissionDenied('冷回复缺少独立普通对话范围')
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id==self.tenant,
                EmployeeRow.employee_id==policy['owner_employee_id']))
            owner = await db.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id==self.tenant,
                OwnershipLockRow.account_id==enrollment.account_id).with_for_update(read=True))
            identity = await db.get(SendingIdentityRow, (str(self.tenant), enrollment.sending_identity_id))
            raw = await db.get(RawArtifactRow, (str(self.tenant), message.raw_artifact_ref))
            if (employee is None or not employee.user_id or owner is None or owner.owner!=employee.employee_id
                or identity is None or raw is None or raw.kind!='email_raw'):
                raise PermissionDenied('冷回复当前客户或原件权限缺失')
            box_id = await db.scalar(select(BusinessMailboxRow.mailbox_id).where(BusinessMailboxRow.tenant_id==self.tenant,
                BusinessMailboxRow.owner_id==employee.employee_id, BusinessMailboxRow.address==identity.address))
            if box_id is None:
                raise PermissionDenied('普通回复当前本人邮箱未启用')
            actor = BusinessMailActor(tenant_id=self.tenant, employee_id=EmployeeId(employee.employee_id), user_id=UserId(employee.user_id))
            box = await self.mailbox._mailbox(db, actor, box_id)
            campaign = await self.outreach.get_ordinary_dialogue_scope(self.tenant, CampaignId(enrollment.campaign_id),
                version=enrollment.campaign_version, owner_employee_id=actor.employee_id,
                actor=Actor(str(actor.employee_id), OutreachScope(level={'boss':ScopeLevel.TENANT,'manager':ScopeLevel.MANAGER,'sales':ScopeLevel.SELF}[employee.role],
                    allowed_campaign_ids=frozenset({CampaignId(enrollment.campaign_id)})), employee.role))
            if not campaign.approval_id or campaign.boundary.ordinary_dialogue_policy is None:
                raise PermissionDenied('普通回复精确批准缺失')
            existing = await db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id==self.tenant,
                DialogueBindingRow.enrollment_id==enrollment_id))
            if existing is not None:
                source = source_from_row(existing)
                await self.sources.bind_message(db, source.binding_id, message_id)
                return source
            source = DialogueSource(tenant_id=self.tenant, binding_id=new_id('dlg'),
                account_id=ProspectAccountId(enrollment.account_id), contact_point_id=ContactPointId(enrollment.contact_point_id),
                hypothesis_id=NeedHypothesisId(enrollment.source_hypothesis_id), sending_identity_id=SendingIdentityId(enrollment.sending_identity_id),
                campaign_id=enrollment.campaign_id, campaign_version=enrollment.campaign_version,
                approval_id=campaign.approval_id, owner_employee_id=actor.employee_id,
                ownership_locked_at=owner.locked_at, mailbox_operation_id=box.binding_operation_id,
                source_kind='outreach', enrollment_id=EnrollmentId(enrollment.enrollment_id), business_message_id=None,
                admitted_message_id=message_id, admitted_source_sha256=raw.content_hash)
            await self.sources.admit(db, source)
            return source
