"""Gateway 出站内容归档适配；只存提交副本，不访问邮箱历史或发送邮件。"""

from email.message import EmailMessage
from email.policy import SMTP

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import RawArtifactKind, RawArtifactStore
from connectors.gmail.client import GmailSendRequest
from domains.conversations.service import ConversationService
from infra.db.tables import (
    ContactPointRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    SendingIdentityRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    MessageAttemptId,
    MessageId,
    ProspectAccountId,
    TenantId,
)


class OutboundMailArchive:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        raw: RawArtifactStore,
        conversations: ConversationService,
    ) -> None:
        self._sessions, self._raw, self._conversations = sessions, raw, conversations

    async def record_sent(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        request: GmailSendRequest,
        provider_ref: str,
    ) -> None:
        """仅接受连接器确认后、原 Attempt 的精确材料，不保存退订能力链接。"""
        await self._record(
            tenant_id,
            attempt_id,
            request.subject,
            request.body,
            provider_ref,
            expected=(
                request.from_address,
                request.recipient_address,
                f"<{request.deterministic_message_id}>",
                request.idempotency_header,
            ),
        )

    async def recover_sent(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        subject: str,
        body: str,
        provider_ref: str,
    ) -> MessageId:
        """受信运维回填既有 SENT：内容必须取自该次已核实提交记录，不调用发送。"""
        return await self._record(
            tenant_id, attempt_id, subject, body, provider_ref, expected=None
        )

    async def _record(
        self,
        tenant_id: TenantId,
        attempt_id: MessageAttemptId,
        subject: str,
        body: str,
        provider_ref: str,
        expected: tuple[str, str, str, str] | None,
    ) -> MessageId:
        async with self._sessions() as db:
            attempt = await db.scalar(
                select(OutreachMessageAttemptRow).where(
                    OutreachMessageAttemptRow.tenant_id == tenant_id,
                    OutreachMessageAttemptRow.attempt_id == attempt_id,
                )
            )
            if (
                attempt is None
                or attempt.state not in ("sending", "sent")
                or not attempt.send_claimed_at
                or not attempt.deterministic_message_id
                or not attempt.idempotency_header
                or not provider_ref
                or (attempt.state == "sent" and attempt.provider_ref != provider_ref)
                or (expected is None and attempt.state != "sent")
            ):
                raise ValidationError("出站归档缺少确定发送关联")
            enrollment = await db.scalar(
                select(OutreachEnrollmentRow).where(
                    OutreachEnrollmentRow.tenant_id == tenant_id,
                    OutreachEnrollmentRow.enrollment_id == attempt.enrollment_id,
                )
            )
            if (
                enrollment is None
                or enrollment.sending_identity_id != attempt.sending_identity_id
            ):
                raise ValidationError("出站归档关联不一致")
            sender = await db.scalar(
                select(SendingIdentityRow.address).where(
                    SendingIdentityRow.tenant_id == tenant_id,
                    SendingIdentityRow.identity_id == attempt.sending_identity_id,
                )
            )
            recipient = await db.scalar(
                select(ContactPointRow.value).where(
                    ContactPointRow.tenant_id == tenant_id,
                    ContactPointRow.contact_point_id == enrollment.contact_point_id,
                )
            )
            facts = (
                sender,
                recipient,
                attempt.deterministic_message_id,
                attempt.idempotency_header,
            )
            if (
                not sender
                or not recipient
                or (expected is not None and facts != expected)
            ):
                raise ValidationError("出站归档材料已变化")
            account, stamp, external = (
                enrollment.account_id,
                attempt.send_claimed_at,
                attempt.deterministic_message_id,
            )
        # 此副本保留实际提交的正文；SMTP 服务商增删的头不冒充已观测事实。
        message = EmailMessage(policy=SMTP)
        message["From"], message["To"], message["Subject"] = sender, recipient, subject
        message["Message-ID"] = external
        message["X-TradeOS-Idempotency-V1"] = facts[3]
        message["X-TradeOS-Archive"] = "submitted-content"
        message.set_content(body)
        meta = await self._raw.put(
            tenant_id, RawArtifactKind.EMAIL_RAW, message.as_bytes(), "message/rfc822"
        )
        return await self._conversations.record_outbound(
            tenant_id,
            ProspectAccountId(account),
            str(meta.artifact_id),
            external,
            stamp,
        )
