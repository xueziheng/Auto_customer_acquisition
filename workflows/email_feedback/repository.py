"""邮件反馈持久化与整页事务的 workflow 内部合同。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from enum import Enum
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.outreach.service import OutreachService
from domains.sending_identity.service import SendingIdentityService
from shared.errors import ValidationError
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

_MAILBOX_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_LOWER_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_LABEL_RE = re.compile(r"[a-z0-9-]{1,32}")
_ID_RE = re.compile(r"([a-z]+)_[0-7][0-9A-HJKMNP-TV-Z]{25}")


def _require_id(value: object, prefix: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 无效")
    matched = _ID_RE.fullmatch(value)
    if matched is None or matched.group(1) != prefix:
        raise ValidationError(f"{field} 无效")
    return value


def _require_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError(f"{field} 必须为 UTC 时间")
    if value.utcoffset() != timedelta(0):
        raise ValidationError(f"{field} 必须为 UTC 时间")
    return value


def _require_mailbox(value: object) -> str:
    if not isinstance(value, str) or _MAILBOX_RE.fullmatch(value) is None:
        raise ValidationError("mailbox_alias 无效")
    return value


def _require_lower_hex(value: object, field: str) -> str:
    if not isinstance(value, str) or _LOWER_HEX_64_RE.fullmatch(value) is None:
        raise ValidationError(f"{field} 无效")
    return value


@dataclass(frozen=True)
class FeedbackCursor:
    tenant_id: TenantId
    mailbox_alias: str
    provider_cursor: str | None = dataclass_field(repr=False)
    version: int
    bootstrap_started_at: datetime
    last_succeeded_at: datetime | None

    def __post_init__(self) -> None:
        _require_id(self.tenant_id, "tn", "tenant_id")
        _require_mailbox(self.mailbox_alias)
        if self.provider_cursor is not None and (
            not isinstance(self.provider_cursor, str)
            or not self.provider_cursor
            or len(self.provider_cursor) > 32768
        ):
            raise ValidationError("provider_cursor 无效")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 0
        ):
            raise ValidationError("cursor version 无效")
        _require_utc(self.bootstrap_started_at, "bootstrap_started_at")
        if self.last_succeeded_at is not None:
            _require_utc(self.last_succeeded_at, "last_succeeded_at")


@dataclass(frozen=True)
class FeedbackReceipt:
    tenant_id: TenantId
    mailbox_alias: str
    provider_event_id: str
    item_fingerprint: str
    ordinal: int
    kind: EmailFeedbackKind
    occurred_at: datetime
    result: EmailFeedbackResult
    attempt_id: MessageAttemptId | None
    enrollment_id: EnrollmentId | None
    account_id: ProspectAccountId | None
    contact_point_id: ContactPointId | None
    sending_identity_id: SendingIdentityId | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_id(self.tenant_id, "tn", "tenant_id")
        _require_mailbox(self.mailbox_alias)
        _require_lower_hex(self.provider_event_id, "provider_event_id")
        _require_lower_hex(self.item_fingerprint, "item_fingerprint")
        if (
            not isinstance(self.ordinal, int)
            or isinstance(self.ordinal, bool)
            or not 0 <= self.ordinal <= 99
        ):
            raise ValidationError("ordinal 无效")
        if not isinstance(self.kind, EmailFeedbackKind):
            raise ValidationError("feedback kind 无效")
        if not isinstance(self.result, EmailFeedbackResult):
            raise ValidationError("feedback result 无效")
        _require_utc(self.occurred_at, "occurred_at")
        _require_utc(self.created_at, "created_at")
        targets = (
            (self.attempt_id, "mat", "attempt_id"),
            (self.enrollment_id, "enr", "enrollment_id"),
            (self.account_id, "acc", "account_id"),
            (self.contact_point_id, "cp", "contact_point_id"),
            (self.sending_identity_id, "sid", "sending_identity_id"),
        )
        present = tuple(value is not None for value, _, _ in targets)
        if any(present) and not all(present):
            raise ValidationError("feedback correlation target 必须完整")
        for value, prefix, field in targets:
            if value is not None:
                _require_id(value, prefix, field)
        if self.result is EmailFeedbackResult.QUARANTINED:
            if self.kind is not EmailFeedbackKind.UNPARSEABLE or any(present):
                raise ValidationError("quarantined feedback 形态无效")
        elif not all(present) or self.kind is EmailFeedbackKind.UNPARSEABLE:
            raise ValidationError("correlated feedback 形态无效")


@dataclass(frozen=True)
class FeedbackQuarantine:
    tenant_id: TenantId
    mailbox_alias: str
    provider_event_id: str
    reason: EmailFeedbackQuarantineReason
    provider_ref_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_id(self.tenant_id, "tn", "tenant_id")
        _require_mailbox(self.mailbox_alias)
        _require_lower_hex(self.provider_event_id, "provider_event_id")
        if not isinstance(self.reason, EmailFeedbackQuarantineReason):
            raise ValidationError("quarantine reason 无效")
        _require_lower_hex(self.provider_ref_digest, "provider_ref_digest")
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, repr=False)
class UnsubscribeTokenRecord:
    tenant_id: TenantId
    nonce_sha256: bytes
    contact_point_id: ContactPointId
    message_attempt_id: MessageAttemptId
    key_id: str
    expires_at: datetime
    consumed_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_id(self.tenant_id, "tn", "tenant_id")
        _require_id(self.contact_point_id, "cp", "contact_point_id")
        _require_id(self.message_attempt_id, "mat", "message_attempt_id")
        if not isinstance(self.nonce_sha256, bytes) or len(self.nonce_sha256) != 32:
            raise ValidationError("nonce_sha256 无效")
        if (
            not isinstance(self.key_id, str)
            or _SAFE_LABEL_RE.fullmatch(self.key_id) is None
        ):
            raise ValidationError("key_id 无效")
        _require_utc(self.created_at, "created_at")
        _require_utc(self.expires_at, "expires_at")
        if self.expires_at != self.created_at + timedelta(days=90):
            raise ValidationError("token expiry 无效")
        if self.consumed_at is not None:
            _require_utc(self.consumed_at, "consumed_at")
            if not self.created_at <= self.consumed_at < self.expires_at:
                raise ValidationError("consumed_at 无效")


class FeedbackReceiptAppendStatus(str, Enum):
    CREATED = "created"
    EXISTING = "existing"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class FeedbackReceiptAppendResult:
    status: FeedbackReceiptAppendStatus
    winner: FeedbackReceipt | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, FeedbackReceiptAppendStatus):
            raise ValidationError("receipt append status 无效")
        expects_winner = self.status is not FeedbackReceiptAppendStatus.CONFLICT
        if expects_winner != (self.winner is not None):
            raise ValidationError("receipt append winner 无效")
        if self.winner is not None and not isinstance(self.winner, FeedbackReceipt):
            raise ValidationError("receipt append winner 类型无效")


@runtime_checkable
class FeedbackCursorRepository(Protocol):
    async def get(
        self, tenant_id: TenantId, mailbox_alias: str
    ) -> FeedbackCursor | None: ...

    async def lock_expected(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        expected_cursor: str | None,
    ) -> FeedbackCursor: ...

    async def advance(
        self, cursor: FeedbackCursor, next_cursor: str, at: datetime
    ) -> None: ...


@runtime_checkable
class FeedbackReceiptRepository(Protocol):
    async def get(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        provider_event_id: str,
    ) -> FeedbackReceipt | None: ...

    async def append_if_absent(
        self, receipt: FeedbackReceipt
    ) -> FeedbackReceiptAppendResult: ...


@runtime_checkable
class FeedbackQuarantineRepository(Protocol):
    async def append_if_absent(self, quarantine: FeedbackQuarantine) -> bool: ...


@runtime_checkable
class UnsubscribeTokenRepository(Protocol):
    async def add(self, token: UnsubscribeTokenRecord) -> None: ...

    async def add_if_absent(self, token: UnsubscribeTokenRecord) -> bool: ...

    async def get_for_update(
        self, tenant_id: TenantId, nonce_sha256: bytes
    ) -> UnsubscribeTokenRecord | None: ...

    async def mark_consumed(
        self, record: UnsubscribeTokenRecord, at: datetime
    ) -> bool: ...


@runtime_checkable
class FeedbackPageUnitOfWork(Protocol):
    cursors: FeedbackCursorRepository
    receipts: FeedbackReceiptRepository
    quarantines: FeedbackQuarantineRepository
    tokens: UnsubscribeTokenRepository
    outreach: OutreachService
    sending_identities: SendingIdentityService

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
