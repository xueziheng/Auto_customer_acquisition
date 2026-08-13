"""邮件反馈 cursor、receipt、quarantine 与 token 的 tenant-bound 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    EmailFeedbackCursorRow,
    EmailFeedbackQuarantineRow,
    EmailFeedbackReceiptRow,
    UnsubscribeTokenRow,
)
from shared.errors import TenantIsolationViolation, TransientError, ValidationError
from shared.schemas.email_feedback import (
    EmailFeedbackKind,
    EmailFeedbackQuarantineReason,
    EmailFeedbackResult,
)
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)
from workflows.email_feedback.repository import (
    FeedbackCursor,
    FeedbackQuarantine,
    FeedbackReceipt,
    FeedbackReceiptAppendResult,
    FeedbackReceiptAppendStatus,
    UnsubscribeTokenRecord,
)

_security_logger = logging.getLogger("infra.db.email_feedback.security")


class _FeedbackRepository(TenantScopedRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _tenant_matches(self, tenant_id: TenantId, rule: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _security_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={
                "repository": type(self).__name__,
                "tenant_id": str(self._tenant_id),
                "rule": rule,
            },
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, rule: str) -> None:
        if not self._tenant_matches(tenant_id, rule):
            raise TenantIsolationViolation("跨租户数据隔离违规")


def _cursor_from_row(row: EmailFeedbackCursorRow) -> FeedbackCursor:
    return FeedbackCursor(
        tenant_id=TenantId(row.tenant_id),
        mailbox_alias=row.mailbox_alias,
        provider_cursor=row.provider_cursor,
        version=row.version,
        bootstrap_started_at=row.bootstrap_started_at,
        last_succeeded_at=row.last_succeeded_at,
    )


def _receipt_values(receipt: FeedbackReceipt) -> dict[str, object]:
    return {
        "tenant_id": str(receipt.tenant_id),
        "mailbox_alias": receipt.mailbox_alias,
        "provider_event_id": receipt.provider_event_id,
        "item_fingerprint": receipt.item_fingerprint,
        "ordinal": receipt.ordinal,
        "kind": receipt.kind.value,
        "occurred_at": receipt.occurred_at,
        "result": receipt.result.value,
        "attempt_id": str(receipt.attempt_id) if receipt.attempt_id else None,
        "enrollment_id": (
            str(receipt.enrollment_id) if receipt.enrollment_id else None
        ),
        "account_id": str(receipt.account_id) if receipt.account_id else None,
        "contact_point_id": (
            str(receipt.contact_point_id) if receipt.contact_point_id else None
        ),
        "sending_identity_id": (
            str(receipt.sending_identity_id) if receipt.sending_identity_id else None
        ),
        "created_at": receipt.created_at,
    }


def _receipt_from_row(row: EmailFeedbackReceiptRow) -> FeedbackReceipt:
    return FeedbackReceipt(
        tenant_id=TenantId(row.tenant_id),
        mailbox_alias=row.mailbox_alias,
        provider_event_id=row.provider_event_id,
        item_fingerprint=row.item_fingerprint,
        ordinal=row.ordinal,
        kind=EmailFeedbackKind(row.kind),
        occurred_at=row.occurred_at,
        result=EmailFeedbackResult(row.result),
        attempt_id=MessageAttemptId(row.attempt_id) if row.attempt_id else None,
        enrollment_id=EnrollmentId(row.enrollment_id) if row.enrollment_id else None,
        account_id=ProspectAccountId(row.account_id) if row.account_id else None,
        contact_point_id=(
            ContactPointId(row.contact_point_id) if row.contact_point_id else None
        ),
        sending_identity_id=(
            SendingIdentityId(row.sending_identity_id)
            if row.sending_identity_id
            else None
        ),
        created_at=row.created_at,
    )


def _quarantine_from_row(row: EmailFeedbackQuarantineRow) -> FeedbackQuarantine:
    return FeedbackQuarantine(
        tenant_id=TenantId(row.tenant_id),
        mailbox_alias=row.mailbox_alias,
        provider_event_id=row.provider_event_id,
        reason=EmailFeedbackQuarantineReason(row.reason),
        provider_ref_digest=row.provider_ref_digest,
        created_at=row.created_at,
    )


def _token_from_row(row: UnsubscribeTokenRow) -> UnsubscribeTokenRecord:
    return UnsubscribeTokenRecord(
        tenant_id=TenantId(row.tenant_id),
        nonce_sha256=bytes(row.nonce_sha256),
        contact_point_id=ContactPointId(row.contact_point_id),
        message_attempt_id=MessageAttemptId(row.message_attempt_id),
        key_id=row.key_id,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        created_at=row.created_at,
    )


class FeedbackCursorRepositoryImpl(_FeedbackRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime],
    ) -> None:
        super().__init__(session, tenant_id)
        self._now = now

    async def get(
        self, tenant_id: TenantId, mailbox_alias: str
    ) -> FeedbackCursor | None:
        if not self._tenant_matches(tenant_id, "feedback_cursor_get"):
            return None
        row = await self._session.get(
            EmailFeedbackCursorRow, (str(self._tenant_id), mailbox_alias)
        )
        return _cursor_from_row(row) if row is not None else None

    async def lock_expected(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        expected_cursor: str | None,
    ) -> FeedbackCursor:
        self._require_tenant(tenant_id, "feedback_cursor_lock")
        seed = FeedbackCursor(
            tenant_id,
            mailbox_alias,
            expected_cursor,
            0,
            self._now(),
            None,
        )
        if expected_cursor is None:
            await self._session.execute(
                insert(EmailFeedbackCursorRow)
                .values(
                    tenant_id=str(self._tenant_id),
                    mailbox_alias=mailbox_alias,
                    provider_cursor=None,
                    version=0,
                    bootstrap_started_at=seed.bootstrap_started_at,
                    last_succeeded_at=None,
                )
                .on_conflict_do_nothing(
                    constraint="pk_email_feedback_cursors"
                )
            )
        row = (
            await self._session.execute(
                select(EmailFeedbackCursorRow)
                .where(
                    EmailFeedbackCursorRow.tenant_id == self._tenant_id,
                    EmailFeedbackCursorRow.mailbox_alias == mailbox_alias,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.provider_cursor != expected_cursor:
            raise TransientError("邮件反馈 cursor 已变化")
        return _cursor_from_row(row)

    async def advance(
        self, cursor: FeedbackCursor, next_cursor: str, at: datetime
    ) -> None:
        self._require_tenant(cursor.tenant_id, "feedback_cursor_advance")
        desired = FeedbackCursor(
            cursor.tenant_id,
            cursor.mailbox_alias,
            next_cursor,
            cursor.version + 1,
            cursor.bootstrap_started_at,
            at,
        )
        result = await self._session.execute(
            update(EmailFeedbackCursorRow)
            .where(
                EmailFeedbackCursorRow.tenant_id == self._tenant_id,
                EmailFeedbackCursorRow.mailbox_alias == cursor.mailbox_alias,
                EmailFeedbackCursorRow.version == cursor.version,
                EmailFeedbackCursorRow.provider_cursor.is_(
                    None
                )
                if cursor.provider_cursor is None
                else EmailFeedbackCursorRow.provider_cursor
                == cursor.provider_cursor,
            )
            .values(
                provider_cursor=desired.provider_cursor,
                version=desired.version,
                last_succeeded_at=desired.last_succeeded_at,
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise TransientError("邮件反馈 cursor 已变化")


class FeedbackReceiptRepositoryImpl(_FeedbackRepository):
    async def get(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        provider_event_id: str,
    ) -> FeedbackReceipt | None:
        if not self._tenant_matches(tenant_id, "feedback_receipt_get"):
            return None
        row = (
            await self._session.execute(
                select(EmailFeedbackReceiptRow).where(
                    EmailFeedbackReceiptRow.tenant_id == self._tenant_id,
                    EmailFeedbackReceiptRow.mailbox_alias == mailbox_alias,
                    EmailFeedbackReceiptRow.provider_event_id == provider_event_id,
                )
            )
        ).scalar_one_or_none()
        return _receipt_from_row(row) if row is not None else None

    async def append_if_absent(
        self, receipt: FeedbackReceipt
    ) -> FeedbackReceiptAppendResult:
        self._require_tenant(receipt.tenant_id, "feedback_receipt_append")
        created = (
            await self._session.execute(
                insert(EmailFeedbackReceiptRow)
                .values(**_receipt_values(receipt))
                .on_conflict_do_nothing(
                    constraint="pk_email_feedback_receipts"
                )
                .returning(EmailFeedbackReceiptRow.provider_event_id)
            )
        ).scalar_one_or_none()
        if created is not None:
            return FeedbackReceiptAppendResult(
                FeedbackReceiptAppendStatus.CREATED, receipt
            )
        row = (
            await self._session.execute(
                select(EmailFeedbackReceiptRow).where(
                    EmailFeedbackReceiptRow.tenant_id == self._tenant_id,
                    EmailFeedbackReceiptRow.mailbox_alias == receipt.mailbox_alias,
                    EmailFeedbackReceiptRow.provider_event_id
                    == receipt.provider_event_id,
                )
            )
        ).scalar_one()
        winner = _receipt_from_row(row)
        if winner == receipt:
            return FeedbackReceiptAppendResult(
                FeedbackReceiptAppendStatus.EXISTING, winner
            )
        return FeedbackReceiptAppendResult(
            FeedbackReceiptAppendStatus.CONFLICT, None
        )


class FeedbackQuarantineRepositoryImpl(_FeedbackRepository):
    async def append_if_absent(self, quarantine: FeedbackQuarantine) -> bool:
        self._require_tenant(quarantine.tenant_id, "feedback_quarantine_append")
        created = (
            await self._session.execute(
                insert(EmailFeedbackQuarantineRow)
                .values(
                    tenant_id=str(quarantine.tenant_id),
                    mailbox_alias=quarantine.mailbox_alias,
                    provider_event_id=quarantine.provider_event_id,
                    reason=quarantine.reason.value,
                    provider_ref_digest=quarantine.provider_ref_digest,
                    created_at=quarantine.created_at,
                )
                .on_conflict_do_nothing(
                    constraint="pk_email_feedback_quarantines"
                )
                .returning(EmailFeedbackQuarantineRow.provider_event_id)
            )
        ).scalar_one_or_none()
        if created is not None:
            return True
        row = (
            await self._session.execute(
                select(EmailFeedbackQuarantineRow).where(
                    EmailFeedbackQuarantineRow.tenant_id == self._tenant_id,
                    EmailFeedbackQuarantineRow.mailbox_alias
                    == quarantine.mailbox_alias,
                    EmailFeedbackQuarantineRow.provider_event_id
                    == quarantine.provider_event_id,
                )
            )
        ).scalar_one()
        if _quarantine_from_row(row) != quarantine:
            raise ValidationError("邮件反馈 quarantine 冲突")
        return False


class UnsubscribeTokenRepositoryImpl(_FeedbackRepository):
    async def add(self, token: UnsubscribeTokenRecord) -> None:
        self._require_tenant(token.tenant_id, "unsubscribe_token_add")
        await self._session.execute(
            insert(UnsubscribeTokenRow).values(
                tenant_id=str(token.tenant_id),
                nonce_sha256=token.nonce_sha256,
                contact_point_id=str(token.contact_point_id),
                message_attempt_id=str(token.message_attempt_id),
                key_id=token.key_id,
                expires_at=token.expires_at,
                consumed_at=token.consumed_at,
                created_at=token.created_at,
            )
        )

    async def get_for_update(
        self, tenant_id: TenantId, nonce_sha256: bytes
    ) -> UnsubscribeTokenRecord | None:
        if not self._tenant_matches(tenant_id, "unsubscribe_token_lock"):
            return None
        row = (
            await self._session.execute(
                select(UnsubscribeTokenRow)
                .where(
                    UnsubscribeTokenRow.tenant_id == self._tenant_id,
                    UnsubscribeTokenRow.nonce_sha256 == nonce_sha256,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return _token_from_row(row) if row is not None else None

    async def mark_consumed(
        self, record: UnsubscribeTokenRecord, at: datetime
    ) -> bool:
        self._require_tenant(record.tenant_id, "unsubscribe_token_consume")
        if record.consumed_at is not None:
            return False
        desired = UnsubscribeTokenRecord(
            tenant_id=record.tenant_id,
            nonce_sha256=record.nonce_sha256,
            contact_point_id=record.contact_point_id,
            message_attempt_id=record.message_attempt_id,
            key_id=record.key_id,
            expires_at=record.expires_at,
            consumed_at=at,
            created_at=record.created_at,
        )
        result = await self._session.execute(
            update(UnsubscribeTokenRow)
            .where(
                UnsubscribeTokenRow.tenant_id == self._tenant_id,
                UnsubscribeTokenRow.nonce_sha256 == record.nonce_sha256,
                UnsubscribeTokenRow.consumed_at.is_(None),
            )
            .values(consumed_at=desired.consumed_at)
        )
        return cast(CursorResult[object], result).rowcount == 1
