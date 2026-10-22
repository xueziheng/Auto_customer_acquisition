"""私有回信测试的有界历史关联事实；轮次许可由 Outreach 域判断。"""
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.schemas import PrivateReplyTestHistory, PrivateReplyTestMessage
from infra.db.tables import (
    ConversationClassificationCorrectionRow,
    ConversationClassificationRow,
    ConversationRow,
    MessageRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    PrivateReplyTestRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    MessageId,
    ProspectAccountId,
    TenantId,
)


class PrivateReplyTestHistoryReader:
    """只读原入站与 canonical SENT 关联，不读取正文、凭证或邮箱。"""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession],
        *, now: Callable[[], datetime],
    ) -> None:
        self._sessions, self._now = sessions, now

    async def read(
        self, tenant_id: TenantId, contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> PrivateReplyTestHistory:
        async with self._sessions() as session:
            marker = await session.scalar(select(PrivateReplyTestRow).where(
                PrivateReplyTestRow.tenant_id == tenant_id,
                PrivateReplyTestRow.account_id == account_id,
                PrivateReplyTestRow.contact_point_id == contact_point_id,
            ))
            if marker is None:
                raise ValidationError("私有测试用途不存在")
            correction = (
                select(ConversationClassificationCorrectionRow.corrected_category)
                .where(
                    ConversationClassificationCorrectionRow.tenant_id == tenant_id,
                    ConversationClassificationCorrectionRow.message_id == MessageRow.message_id,
                )
                .order_by(
                    ConversationClassificationCorrectionRow.corrected_at.desc(),
                    ConversationClassificationCorrectionRow.correction_id.desc(),
                )
                .limit(1).correlate(MessageRow).scalar_subquery()
            )
            rows = (await session.execute(
                select(
                    MessageRow.message_id,
                    func.coalesce(correction, ConversationClassificationRow.category),
                    OutreachMessageAttemptRow,
                    OutreachEnrollmentRow,
                )
                .select_from(MessageRow)
                .join(ConversationRow, (
                    (ConversationRow.tenant_id == tenant_id)
                    & (ConversationRow.conversation_id == MessageRow.conversation_id)
                    & (ConversationRow.account_id == account_id)
                ))
                .outerjoin(ConversationClassificationRow, (
                    (ConversationClassificationRow.tenant_id == tenant_id)
                    & (ConversationClassificationRow.message_id == MessageRow.message_id)
                ))
                .outerjoin(OutreachMessageAttemptRow, (
                    (OutreachMessageAttemptRow.tenant_id == tenant_id)
                    & (OutreachMessageAttemptRow.deterministic_message_id == MessageRow.outbound_message_id)
                ))
                .outerjoin(OutreachEnrollmentRow, (
                    (OutreachEnrollmentRow.tenant_id == tenant_id)
                    & (OutreachEnrollmentRow.enrollment_id == OutreachMessageAttemptRow.enrollment_id)
                ))
                .where(MessageRow.tenant_id == tenant_id, MessageRow.direction == "inbound")
                .order_by(MessageRow.message_id).limit(101)
            )).all()
            if len(rows) > 100 or len({row[0] for row in rows}) != len(rows):
                raise ValidationError("私有测试回复关联过多或不唯一")
            messages = []
            for message_id, category, attempt, enrollment in rows:
                matched = (
                    attempt is not None and enrollment is not None
                    and attempt.state == "sent" and bool(attempt.provider_ref)
                    and attempt.sending_identity_id == marker.sending_identity_id
                    and enrollment.sending_identity_id == marker.sending_identity_id
                    and enrollment.account_id == account_id
                    and enrollment.contact_point_id == contact_point_id
                    and enrollment.campaign_id == attempt.campaign_id
                    and enrollment.campaign_version == attempt.campaign_version
                    and enrollment.execution_mode == "manual"
                    and enrollment.source_hypothesis_id is None
                )
                messages.append(PrivateReplyTestMessage(
                    message_id=MessageId(message_id),
                    category=category,
                    source_campaign_id=CampaignId(attempt.campaign_id) if attempt is not None else None,
                    source_is_private_sent=matched,
                ))
            return PrivateReplyTestHistory(
                tenant_id, contact_point_id, account_id, tuple(messages), self._now(),
            )
