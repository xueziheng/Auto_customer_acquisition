"""通知任务的固定上下文与持久化契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from notification_gateway.models import NotificationPriority
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, NotificationJobId, TenantId

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
_SECRET_MARKER = re.compile(r"(?i)(bearer|token|secret|password|authorization|akia|sk-)")


class NotificationKind(str, Enum):
    HANDOFF_ESCALATION = "handoff_escalation"
    HANDOFF_QUEUE_BACKLOGGED = "handoff_queue_backlogged"
    SENDING_IDENTITY_SUSPENDED = "sending_identity_suspended"
    REPUTATION_THRESHOLD_BREACHED = "reputation_threshold_breached"
    COMMITMENT_OVERDUE = "commitment_overdue"
    APPROVAL_DECIDED = "approval_decided"
    QUOTE_APPROVAL_RESULT = "quote_approval_result"


class NotificationJobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REJECTED = "rejected"


@dataclass(frozen=True, repr=False)
class NotificationContext:
    """只携带固定 typed ID 与等级，禁止通知承载自由文本或凭证形态。"""

    kind: NotificationKind
    primary_id: str
    secondary_id: str | None
    reason_code: str | None
    level: int | None

    def __post_init__(self) -> None:
        values = (self.primary_id, self.secondary_id, self.reason_code)
        if (
            not isinstance(self.kind, NotificationKind)
            or not isinstance(self.primary_id, str)
            or not _safe_value(self.primary_id)
            or any(value is not None and (not isinstance(value, str) or not _safe_value(value)) for value in values[1:])
            or (self.level is not None and (type(self.level) is not int or not 0 <= self.level <= 99))
        ):
            raise ValidationError("通知上下文无效")


@dataclass(frozen=True)
class NotificationJob:
    job_id: NotificationJobId
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    context: NotificationContext
    source_event_fingerprint: str
    source_event: str
    dedup_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.priority, NotificationPriority)
            or not isinstance(self.context, NotificationContext)
            or not _is_utc(self.created_at)
            or not re.fullmatch(r"[0-9a-f]{64}", self.source_event_fingerprint)
            or not _safe_value(self.source_event)
            or not _safe_value(self.dedup_key)
        ):
            raise ValidationError("通知任务无效")


@dataclass(frozen=True)
class NotificationJobClaim:
    job_id: NotificationJobId
    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    context: NotificationContext
    source_event: str
    dedup_key: str
    claim_token: str
    attempt_count: int


class NotificationJobStore(Protocol):
    async def enqueue(self, job: NotificationJob) -> bool: ...
    async def claim_due(self, tenant_id: TenantId, *, limit: int, lease_owner: str) -> tuple[NotificationJobClaim, ...]: ...
    async def complete(self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str) -> bool: ...
    async def retry(self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str, error: BaseException) -> bool: ...
    async def reject(self, tenant_id: TenantId, job_id: NotificationJobId, *, claim_token: str, error: BaseException) -> bool: ...


def _safe_value(value: str) -> bool:
    return bool(_SAFE_ID.fullmatch(value)) and _SECRET_MARKER.search(value) is None


def _is_utc(value: datetime) -> bool:
    offset = value.utcoffset()
    return value.tzinfo is not None and offset is not None and offset.total_seconds() == 0
