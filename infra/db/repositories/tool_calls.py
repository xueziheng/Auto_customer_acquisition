"""Tool Gateway tenant-scoped PostgreSQL ledger repository。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.tables import ToolCallEventRow, ToolCallRow
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import IdempotencyKey, RunId, TenantId, UserId
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.repository import (
    ClaimResult,
    ClaimStatus,
    ToolCallEventRecord,
    ToolCallId,
    ToolCallRecord,
)

_security_logger = logging.getLogger("security.tenant_isolation")
_TRANSIENT_CATEGORIES = frozenset(
    {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
        ToolErrorCategory.RECONCILIATION_REQUIRED,
    }
)


class ToolCallRepositoryImpl:
    """所有 SQL 同时绑定 tenant 与资源键；canonical race 由 PG 裁决。"""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now or (lambda: datetime.now(UTC))

    async def create_received(self, record: ToolCallRecord) -> None:
        self._require_bound(record.tenant_id, "tool_call:create")
        if record.status is not ToolCallStatus.RECEIVED:
            raise ValidationError("新 tool call 必须是 received")
        self._session.add(ToolCallRow(**_record_values(record)))

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
    ) -> ClaimResult:
        self._require_bound(tenant_id, "tool_call:claim")
        now = self._now()
        candidate = await self._locked_call(tool_call_id)
        if candidate is None:
            raise ValidationError("tool call 不存在")
        if candidate.status != ToolCallStatus.RECEIVED.value:
            raise ValidationError("tool call 状态不可 claim")
        if candidate.tool_id != tool_id:
            raise ValidationError("tool call 工具不匹配")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValidationError("idempotency_key 无效")
        if lease_expires_at <= now:
            raise ValidationError("tool call lease 无效")

        base = _row_values(candidate)
        await self._session.execute(
            delete(ToolCallRow).where(
                ToolCallRow.tenant_id == self._tenant_id,
                ToolCallRow.tool_call_id == tool_call_id,
            )
        )
        canonical_values = {
            **base,
            "idempotency_key": str(idempotency_key),
            "request_fingerprint": request_fingerprint,
            "fingerprint_version": fingerprint_version,
            "status": ToolCallStatus.CLAIMED.value,
            "lease_owner": lease_owner,
            "lease_expires_at": lease_expires_at,
            "attempt_count": 1,
            "updated_at": now,
        }
        inserted = (
            await self._session.execute(
                insert(ToolCallRow)
                .values(**canonical_values)
                .on_conflict_do_nothing(
                    index_elements=(
                        ToolCallRow.tenant_id,
                        ToolCallRow.tool_id,
                        ToolCallRow.idempotency_key,
                    ),
                    index_where=ToolCallRow.idempotency_key.is_not(None),
                )
                .returning(ToolCallRow.tool_call_id)
            )
        ).scalar_one_or_none()
        if inserted is not None:
            return ClaimResult(
                ClaimStatus.CLAIMED,
                _values_to_record(canonical_values),
            )

        winner = (
            await self._session.execute(
                select(ToolCallRow)
                .where(
                    ToolCallRow.tenant_id == self._tenant_id,
                    ToolCallRow.tool_id == tool_id,
                    ToolCallRow.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if winner is None:
            raise ValidationError("canonical tool call 不存在")
        canonical = _row_to_record(winner)
        if (
            canonical.request_fingerprint != request_fingerprint
            or canonical.fingerprint_version != fingerprint_version
        ):
            await self._insert_resolution(
                base,
                request_fingerprint=request_fingerprint,
                fingerprint_version=fingerprint_version,
                status=ToolCallStatus.REJECTED,
                duplicate_of=None,
                error_category=ToolErrorCategory.IDEMPOTENCY_CONFLICT,
                now=now,
            )
            return ClaimResult(ClaimStatus.CONFLICT, canonical)

        if _can_reclaim(canonical, now):
            reconciliation_only = (
                canonical.status is ToolCallStatus.FAILED_TRANSIENT
                and canonical.error_category
                is ToolErrorCategory.RECONCILIATION_REQUIRED
            )
            winner.status = ToolCallStatus.CLAIMED.value
            winner.lease_owner = lease_owner
            winner.lease_expires_at = lease_expires_at
            winner.attempt_count += 1
            winner.error_category = None
            winner.retry_after_at = None
            winner.completed_at = None
            winner.updated_at = now
            await self._insert_resolution(
                base,
                request_fingerprint=request_fingerprint,
                fingerprint_version=fingerprint_version,
                status=ToolCallStatus.DUPLICATE,
                duplicate_of=ToolCallId(winner.tool_call_id),
                error_category=None,
                now=now,
            )
            return ClaimResult(
                ClaimStatus.CLAIMED,
                _row_to_record(winner),
                reconciliation_only=reconciliation_only,
            )

        outcome = (
            ClaimStatus.DUPLICATE
            if canonical.status
            in {ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED_PERMANENT}
            else ClaimStatus.IN_PROGRESS
        )
        await self._insert_resolution(
            base,
            request_fingerprint=request_fingerprint,
            fingerprint_version=fingerprint_version,
            status=ToolCallStatus.DUPLICATE,
            duplicate_of=canonical.tool_call_id,
            error_category=None,
            now=now,
        )
        return ClaimResult(outcome, canonical)

    async def mark_executing(
        self, tenant_id: TenantId, tool_call_id: ToolCallId
    ) -> None:
        self._require_bound(tenant_id, "tool_call:execute")
        row = await self._locked_call(tool_call_id)
        if row is None or row.status != ToolCallStatus.CLAIMED.value:
            raise ValidationError("tool call 不可进入 executing")
        row.status = ToolCallStatus.EXECUTING.value
        row.updated_at = self._now()

    async def complete(
        self,
        tenant_id: TenantId,
        tool_call_id: ToolCallId,
        *,
        status: ToolCallStatus,
        provider_ref: str | None,
        error_category: ToolErrorCategory | None,
        retry_after_at: datetime | None,
    ) -> None:
        self._require_bound(tenant_id, "tool_call:complete")
        _validate_completion(status, provider_ref, error_category, retry_after_at)
        row = await self._locked_call(tool_call_id)
        if row is None:
            raise ValidationError("tool call 不存在")
        current = ToolCallStatus(row.status)
        allowed_from = {
            ToolCallStatus.REJECTED: {ToolCallStatus.RECEIVED},
            ToolCallStatus.SUCCEEDED: {ToolCallStatus.EXECUTING},
            ToolCallStatus.FAILED_TRANSIENT: {
                ToolCallStatus.CLAIMED,
                ToolCallStatus.EXECUTING,
            },
            ToolCallStatus.FAILED_PERMANENT: {
                ToolCallStatus.CLAIMED,
                ToolCallStatus.EXECUTING,
            },
        }
        if current not in allowed_from[status]:
            raise ValidationError("tool call 完结状态无效")
        now = self._now()
        row.status = status.value
        row.provider_ref = provider_ref
        row.error_category = error_category.value if error_category else None
        row.retry_after_at = retry_after_at
        row.updated_at = now
        if status is ToolCallStatus.FAILED_TRANSIENT:
            row.completed_at = None
        else:
            row.lease_owner = None
            row.lease_expires_at = None
            row.completed_at = now

    async def append_event(self, event: ToolCallEventRecord) -> None:
        self._require_bound(event.tenant_id, "tool_call:event")
        self._session.add(
            ToolCallEventRow(
                tenant_id=str(event.tenant_id),
                event_id=event.event_id,
                tool_call_id=str(event.tool_call_id),
                stage=event.stage,
                outcome=event.outcome,
                rule=event.rule,
                category=event.category.value if event.category else None,
                actor_id=event.actor_id,
                run_id=event.run_id,
                campaign_id=event.campaign_id,
                message_attempt_id=event.message_attempt_id,
                occurred_at=event.occurred_at,
                duration_ms=event.duration_ms,
                cost_note=event.cost_note,
            )
        )

    async def get(
        self, tenant_id: TenantId, tool_call_id: ToolCallId
    ) -> ToolCallRecord | None:
        if tenant_id != self._tenant_id:
            return None
        row = (
            await self._session.execute(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == self._tenant_id,
                    ToolCallRow.tool_call_id == tool_call_id,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _row_to_record(row)

    async def _locked_call(self, tool_call_id: ToolCallId) -> ToolCallRow | None:
        return (
            await self._session.execute(
                select(ToolCallRow)
                .where(
                    ToolCallRow.tenant_id == self._tenant_id,
                    ToolCallRow.tool_call_id == tool_call_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _insert_resolution(
        self,
        base: dict[str, object],
        *,
        request_fingerprint: str,
        fingerprint_version: str,
        status: ToolCallStatus,
        duplicate_of: ToolCallId | None,
        error_category: ToolErrorCategory | None,
        now: datetime,
    ) -> None:
        values = {
            **base,
            "request_fingerprint": request_fingerprint,
            "fingerprint_version": fingerprint_version,
            "status": status.value,
            "duplicate_of": str(duplicate_of) if duplicate_of else None,
            "error_category": error_category.value if error_category else None,
            "completed_at": now,
            "updated_at": now,
        }
        await self._session.execute(
            insert(ToolCallRow).values(**values)
        )

    def _require_bound(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _security_logger.critical(
            "检测到跨租户工具账本访问",
            extra={
                "tenant_id": str(self._tenant_id),
                "action": action,
                "rule": "tenant-bound-repository",
            },
        )
        raise TenantIsolationViolation("跨租户工具账本访问被拒绝")


def _can_reclaim(record: ToolCallRecord, now: datetime) -> bool:
    if record.status is ToolCallStatus.EXECUTING:
        return False
    if record.status not in {ToolCallStatus.CLAIMED, ToolCallStatus.FAILED_TRANSIENT}:
        return False
    if record.lease_expires_at is None or record.lease_expires_at > now:
        return False
    return record.retry_after_at is None or record.retry_after_at <= now


def _validate_completion(
    status: ToolCallStatus,
    provider_ref: str | None,
    error_category: ToolErrorCategory | None,
    retry_after_at: datetime | None,
) -> None:
    allowed = {
        ToolCallStatus.REJECTED,
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED_TRANSIENT,
        ToolCallStatus.FAILED_PERMANENT,
    }
    if status not in allowed:
        raise ValidationError("tool call 完结状态无效")
    if status is ToolCallStatus.SUCCEEDED:
        if provider_ref is None or error_category is not None or retry_after_at is not None:
            raise ValidationError("成功 tool call 字段无效")
        _safe_provider_ref(provider_ref)
        return
    if provider_ref is not None or error_category is None:
        raise ValidationError("失败 tool call 字段无效")
    if status is ToolCallStatus.FAILED_TRANSIENT:
        if error_category not in _TRANSIENT_CATEGORIES:
            raise ValidationError("临时失败分类无效")
        if retry_after_at is not None and (
            retry_after_at.tzinfo is None or retry_after_at.utcoffset() is None
        ):
            raise ValidationError("retry_after_at 无效")
    elif retry_after_at is not None or (
        status is ToolCallStatus.FAILED_PERMANENT
        and error_category in _TRANSIENT_CATEGORIES
    ):
        raise ValidationError("永久失败分类无效")


def _safe_provider_ref(value: str) -> str:
    lowered = value.casefold()
    if (
        not 1 <= len(value) <= 200
        or value != value.strip()
        or any(character.isspace() for character in value)
        or "@" in value
        or "://" in value
        or any(
            marker in lowered
            for marker in ("bearer", "token", "secret", "password", "authorization")
        )
    ):
        raise ValidationError("provider_ref 无效")
    return value


def _record_values(record: ToolCallRecord) -> dict[str, object]:
    return {
        "tenant_id": str(record.tenant_id),
        "tool_call_id": str(record.tool_call_id),
        "tool_id": record.tool_id,
        "tool_version": record.tool_version,
        "risk_level": record.risk_level,
        "cost_class": record.cost_class,
        "idempotency_key": str(record.idempotency_key) if record.idempotency_key else None,
        "request_fingerprint": record.request_fingerprint,
        "fingerprint_version": record.fingerprint_version,
        "status": record.status.value,
        "duplicate_of": str(record.duplicate_of) if record.duplicate_of else None,
        "lease_owner": record.lease_owner,
        "lease_expires_at": record.lease_expires_at,
        "attempt_count": record.attempt_count,
        "run_id": str(record.run_id) if record.run_id else None,
        "user_id": str(record.user_id),
        "campaign_id": record.campaign_id,
        "message_attempt_id": record.message_attempt_id,
        "provider_ref": record.provider_ref,
        "error_category": record.error_category.value if record.error_category else None,
        "retry_after_at": record.retry_after_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "completed_at": record.completed_at,
    }


def _row_values(row: ToolCallRow) -> dict[str, object]:
    return {
        column.name: getattr(row, column.name)
        for column in ToolCallRow.__table__.columns
    }


def _values_to_record(values: dict[str, object]) -> ToolCallRecord:
    try:
        return ToolCallRecord(
            tenant_id=TenantId(str(values["tenant_id"])),
            tool_call_id=ToolCallId(str(values["tool_call_id"])),
            tool_id=str(values["tool_id"]),
            tool_version=str(values["tool_version"]),
            risk_level=str(values["risk_level"]),
            cost_class=str(values["cost_class"]),
            idempotency_key=(
                IdempotencyKey(str(values["idempotency_key"]))
                if values.get("idempotency_key") is not None
                else None
            ),
            request_fingerprint=_optional_str(values.get("request_fingerprint")),
            fingerprint_version=_optional_str(values.get("fingerprint_version")),
            status=ToolCallStatus(str(values["status"])),
            duplicate_of=(
                ToolCallId(str(values["duplicate_of"]))
                if values.get("duplicate_of") is not None
                else None
            ),
            lease_owner=_optional_str(values.get("lease_owner")),
            lease_expires_at=_optional_datetime(values.get("lease_expires_at")),
            attempt_count=int(str(values["attempt_count"])),
            run_id=(
                RunId(str(values["run_id"])) if values.get("run_id") is not None else None
            ),
            user_id=UserId(str(values["user_id"])),
            campaign_id=_optional_str(values.get("campaign_id")),
            message_attempt_id=_optional_str(values.get("message_attempt_id")),
            provider_ref=_optional_str(values.get("provider_ref")),
            error_category=(
                ToolErrorCategory(str(values["error_category"]))
                if values.get("error_category") is not None
                else None
            ),
            retry_after_at=_optional_datetime(values.get("retry_after_at")),
            created_at=_required_datetime(values.get("created_at")),
            updated_at=_required_datetime(values.get("updated_at")),
            completed_at=_optional_datetime(values.get("completed_at")),
        )
    except (KeyError, TypeError, ValueError):
        raise ValidationError("tool call 持久化状态损坏") from None


def _row_to_record(row: ToolCallRow) -> ToolCallRecord:
    return _values_to_record(_row_values(row))


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _required_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValidationError("tool call 时间损坏")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _required_datetime(value)
