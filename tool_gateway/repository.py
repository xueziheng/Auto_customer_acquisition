"""Tool Gateway durable ledger 的公开 records、claim 结果与 repository/UoW 接口。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import TracebackType
from typing import NewType, Protocol, Self, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
)

from .errors import ToolCallStatus, ToolErrorCategory

ToolCallId = NewType("ToolCallId", str)
_TOOL_ID_RE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_SAFE_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_TOOL_CALL_ID_RE = re.compile(r"tcl_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_EVENT_ID_RE = re.compile(r"tce_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_RISK_LEVELS = frozenset({"low", "medium", "high"})
_COST_CLASSES = frozenset({"free", "low", "medium", "high"})
_SECRET_MARKERS = ("bearer", "token", "secret", "password", "authorization")
_SECRET_TOKENS = frozenset(_SECRET_MARKERS)


class ClaimStatus(str, Enum):
    """一次 idempotency claim 的确定性结果。"""

    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    IN_PROGRESS = "in_progress"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ToolCallRecord:
    """不含原始参数/内容的 invocation durable view。"""

    tenant_id: TenantId
    tool_call_id: ToolCallId
    tool_id: str
    tool_version: str
    risk_level: str
    cost_class: str
    idempotency_key: IdempotencyKey | None
    request_fingerprint: str | None
    fingerprint_version: str | None
    status: ToolCallStatus
    duplicate_of: ToolCallId | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempt_count: int
    run_id: RunId | None
    user_id: UserId
    campaign_id: str | None
    message_attempt_id: str | None
    provider_ref: str | None
    error_category: ToolErrorCategory | None
    retry_after_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def __post_init__(self) -> None:
        _require_tenant(self.tenant_id)
        _require_tool_call_id(self.tool_call_id)
        if _TOOL_ID_RE.fullmatch(self.tool_id) is None:
            raise ValidationError("tool_id 无效")
        _require_safe_label(self.tool_version, "tool_version", max_length=100)
        if self.risk_level not in _RISK_LEVELS:
            raise ValidationError("risk_level 无效")
        if self.cost_class not in _COST_CLASSES:
            raise ValidationError("cost_class 无效")
        _require_safe_label(self.user_id, "user_id", max_length=32)
        _require_optional_label(self.run_id, "run_id", max_length=32)
        _require_optional_label(self.campaign_id, "campaign_id", max_length=32)
        _require_optional_label(
            self.message_attempt_id, "message_attempt_id", max_length=32
        )
        _require_utc(self.created_at, "created_at")
        _require_utc(self.updated_at, "updated_at")
        if self.completed_at is not None:
            _require_utc(self.completed_at, "completed_at")
        if self.lease_expires_at is not None:
            _require_utc(self.lease_expires_at, "lease_expires_at")
        if self.retry_after_at is not None:
            _require_utc(self.retry_after_at, "retry_after_at")
        if not isinstance(self.status, ToolCallStatus):
            raise ValidationError("tool call status 无效")
        if self.error_category is not None and not isinstance(
            self.error_category, ToolErrorCategory
        ):
            raise ValidationError("tool error category 无效")
        if self.duplicate_of is not None:
            _require_tool_call_id(self.duplicate_of)
        if self.idempotency_key is not None:
            _require_safe_label(
                self.idempotency_key, "idempotency_key", max_length=200
            )
        if (self.request_fingerprint is None) != (self.fingerprint_version is None):
            raise ValidationError("fingerprint pair 无效")
        if self.request_fingerprint is not None:
            if _FINGERPRINT_RE.fullmatch(self.request_fingerprint) is None:
                raise ValidationError("request_fingerprint 无效")
            _require_safe_label(
                self.fingerprint_version,
                "fingerprint_version",
                max_length=100,
            )
        if self.lease_owner is not None:
            _require_safe_label(self.lease_owner, "lease_owner", max_length=100)
        if not isinstance(self.attempt_count, int) or isinstance(
            self.attempt_count, bool
        ) or self.attempt_count < 0:
            raise ValidationError("attempt_count 无效")
        if self.provider_ref is not None:
            _require_provider_ref(self.provider_ref)
        _validate_record_state(self)


@dataclass(frozen=True)
class ToolCallEventRecord:
    """只增、无原始内容的 stage 审计事件。"""

    tenant_id: TenantId
    event_id: str
    tool_call_id: ToolCallId
    stage: str
    outcome: str
    rule: str | None
    category: ToolErrorCategory | None
    actor_id: str
    run_id: str | None
    campaign_id: str | None
    message_attempt_id: str | None
    occurred_at: datetime
    duration_ms: int
    cost_note: str | None

    def __post_init__(self) -> None:
        _require_tenant(self.tenant_id)
        if not isinstance(self.event_id, str) or _EVENT_ID_RE.fullmatch(
            self.event_id
        ) is None:
            raise ValidationError("event_id 无效")
        _require_tool_call_id(self.tool_call_id)
        _require_safe_label(self.stage, "stage", max_length=100)
        _require_safe_label(self.outcome, "outcome", max_length=100)
        _require_optional_label(self.rule, "rule", max_length=100)
        if self.category is not None and not isinstance(
            self.category, ToolErrorCategory
        ):
            raise ValidationError("event category 无效")
        _require_safe_label(self.actor_id, "actor_id", max_length=64)
        _require_optional_label(self.run_id, "run_id", max_length=32)
        _require_optional_label(self.campaign_id, "campaign_id", max_length=32)
        _require_optional_label(
            self.message_attempt_id, "message_attempt_id", max_length=32
        )
        _require_utc(self.occurred_at, "occurred_at")
        if not isinstance(self.duration_ms, int) or isinstance(
            self.duration_ms, bool
        ) or self.duration_ms < 0:
            raise ValidationError("duration_ms 无效")
        _require_optional_label(self.cost_note, "cost_note", max_length=100)


@dataclass(frozen=True)
class ClaimResult:
    """claim outcome 与真实 canonical 行。"""

    status: ClaimStatus
    canonical: ToolCallRecord
    reconciliation_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, ClaimStatus):
            raise ValidationError("claim status 无效")
        if not isinstance(self.canonical, ToolCallRecord):
            raise ValidationError("canonical tool call 无效")
        if not isinstance(self.reconciliation_only, bool):
            raise ValidationError("claim 恢复模式无效")
        if self.reconciliation_only and self.status is not ClaimStatus.CLAIMED:
            raise ValidationError("claim 恢复模式无效")


@runtime_checkable
class ToolCallRepository(Protocol):
    """tenant-bound durable ledger repository。"""

    async def create_received(self, record: ToolCallRecord) -> None: ...

    async def claim(
        self,
        tenant_id: TenantId,
        tool_call_id: ToolCallId,
        *,
        tool_id: str,
        idempotency_key: IdempotencyKey,
        request_fingerprint: str,
        fingerprint_version: str,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> ClaimResult: ...

    async def mark_executing(
        self, tenant_id: TenantId, tool_call_id: ToolCallId
    ) -> None: ...

    async def complete(
        self,
        tenant_id: TenantId,
        tool_call_id: ToolCallId,
        *,
        status: ToolCallStatus,
        provider_ref: str | None,
        error_category: ToolErrorCategory | None,
        retry_after_at: datetime | None,
    ) -> None: ...

    async def append_event(self, event: ToolCallEventRecord) -> None: ...

    async def get(
        self, tenant_id: TenantId, tool_call_id: ToolCallId
    ) -> ToolCallRecord | None: ...


@runtime_checkable
class ToolGatewayUnitOfWork(Protocol):
    """一次 Tool Gateway ledger 事务。"""

    calls: ToolCallRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class ToolGatewayUnitOfWorkFactory(Protocol):
    """按 tenant 创建 request-scoped UoW。"""

    def __call__(self, tenant_id: TenantId) -> ToolGatewayUnitOfWork: ...


def _validate_record_state(record: ToolCallRecord) -> None:
    canonical = record.status in {
        ToolCallStatus.CLAIMED,
        ToolCallStatus.EXECUTING,
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED_TRANSIENT,
        ToolCallStatus.FAILED_PERMANENT,
    }
    if canonical != (record.idempotency_key is not None):
        raise ValidationError("canonical tool call 字段无效")
    if canonical and record.request_fingerprint is None:
        raise ValidationError("canonical tool call 缺少 fingerprint")
    if (record.status is ToolCallStatus.DUPLICATE) != (
        record.duplicate_of is not None
    ):
        raise ValidationError("duplicate tool call 字段无效")
    leased = record.status in {
        ToolCallStatus.CLAIMED,
        ToolCallStatus.EXECUTING,
        ToolCallStatus.FAILED_TRANSIENT,
    }
    if leased != (
        record.lease_owner is not None and record.lease_expires_at is not None
    ):
        raise ValidationError("tool call lease 字段无效")
    if canonical != (record.attempt_count >= 1):
        raise ValidationError("tool call attempt_count 无效")
    completed = record.status in {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.REJECTED,
        ToolCallStatus.DUPLICATE,
        ToolCallStatus.FAILED_PERMANENT,
    }
    if completed != (record.completed_at is not None):
        raise ValidationError("tool call completed_at 无效")
    if (record.status is ToolCallStatus.SUCCEEDED) != (
        record.provider_ref is not None
    ):
        raise ValidationError("tool call provider_ref 无效")
    failed = record.status in {
        ToolCallStatus.REJECTED,
        ToolCallStatus.FAILED_TRANSIENT,
        ToolCallStatus.FAILED_PERMANENT,
    }
    if failed != (record.error_category is not None):
        raise ValidationError("tool call error_category 无效")
    if record.retry_after_at is not None and record.status is not ToolCallStatus.FAILED_TRANSIENT:
        raise ValidationError("tool call retry_after_at 无效")


def _require_tenant(value: object) -> str:
    return _require_safe_label(value, "tenant_id", max_length=32)


def _require_tool_call_id(value: object) -> str:
    if not isinstance(value, str) or _TOOL_CALL_ID_RE.fullmatch(value) is None:
        raise ValidationError("tool_call_id 无效")
    return value


def _require_safe_label(value: object, field_name: str, *, max_length: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= max_length
        or value != value.strip()
        or _SAFE_LABEL_RE.fullmatch(value) is None
    ):
        raise ValidationError(f"{field_name} 无效")
    tokens = frozenset(re.split(r"[_.:-]", value.casefold()))
    if tokens & _SECRET_TOKENS:
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_optional_label(
    value: object | None, field_name: str, *, max_length: int
) -> str | None:
    if value is None:
        return None
    return _require_safe_label(value, field_name, max_length=max_length)


def _require_utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError(f"{field_name} 必须是 UTC aware datetime")
    return value


def _require_provider_ref(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("provider_ref 无效")
    lowered = value.casefold()
    if (
        not 1 <= len(value) <= 200
        or value != value.strip()
        or any(character.isspace() for character in value)
        or "@" in value
        or "://" in value
        or any(marker in lowered for marker in _SECRET_MARKERS)
    ):
        raise ValidationError("provider_ref 无效")
    return value
