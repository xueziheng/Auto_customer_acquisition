"""Provider readiness tenant-bound append-only PostgreSQL 事件流仓储。"""

from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from infra.db.tables import ProviderReadinessEventRow
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    TenantIsolationViolation,
    ValidationError,
)
from shared.schemas.identifiers import IdempotencyKey, TenantId
from tool_gateway.provider_readiness import (
    ProviderCapability,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessEvent,
    ProviderReadinessEventId,
    ProviderReadinessEventType,
    ProviderReadinessRepository,
    ProviderValidationFailureCode,
)

_tenant_logger = logging.getLogger("security.tenant_isolation")


def _capability_values(
    capabilities: tuple[ProviderCapability, ...],
) -> list[str]:
    return [capability.value for capability in capabilities]


def _to_event(row: ProviderReadinessEventRow) -> ProviderReadinessEvent:
    capabilities = tuple(
        ProviderCapability(value) for value in row.capability_set
    )
    configuration = ProviderConfiguration(
        provider=ProviderId(row.provider),
        capabilities=capabilities,
        configuration_version=row.configuration_version,
        connector_profile_version=row.connector_profile_version,
        transport_profile=row.transport_profile,
        api_key_version=row.api_key_version,
        configuration_hash=row.configuration_hash,
    )
    return ProviderReadinessEvent(
        tenant_id=TenantId(row.tenant_id),
        event_id=ProviderReadinessEventId(row.provider_readiness_event_id),
        provider=ProviderId(row.provider),
        capabilities=capabilities,
        sequence=row.sequence,
        event_type=ProviderReadinessEventType(row.event_type),
        configuration=configuration,
        validation_key=(
            IdempotencyKey(row.validation_key)
            if row.validation_key is not None
            else None
        ),
        failure_code=(
            ProviderValidationFailureCode(row.outcome_code)
            if row.outcome_code is not None
            else None
        ),
        evidence_ref=row.evidence_ref,
        actor_id=row.actor_id,
        occurred_at=row.occurred_at,
        idempotency_key=IdempotencyKey(row.idempotency_key),
    )


def _to_row(event: ProviderReadinessEvent) -> ProviderReadinessEventRow:
    if event.sequence is None:
        raise ValidationError("Provider 事件序号缺失")
    return ProviderReadinessEventRow(
        tenant_id=str(event.tenant_id),
        provider_readiness_event_id=str(event.event_id),
        provider=event.provider.value,
        capability_set=_capability_values(event.capabilities),
        sequence=event.sequence,
        event_type=event.event_type.value,
        configuration_version=event.configuration.configuration_version,
        configuration_hash=event.configuration.configuration_hash,
        connector_profile_version=(
            event.configuration.connector_profile_version
        ),
        transport_profile=event.configuration.transport_profile,
        api_key_version=event.configuration.api_key_version,
        validation_key=(
            str(event.validation_key)
            if event.validation_key is not None
            else None
        ),
        outcome_code=event.outcome_code,
        evidence_ref=event.evidence_ref,
        actor_id=event.actor_id,
        occurred_at=event.occurred_at,
        idempotency_key=str(event.idempotency_key),
    )


def _same_operation(
    first: ProviderReadinessEvent, second: ProviderReadinessEvent
) -> bool:
    """对齐纯契约的幂等语义；审计 actor/time/ID 不改变业务操作。"""
    return (
        first.tenant_id == second.tenant_id
        and first.provider is second.provider
        and first.capabilities == second.capabilities
        and first.event_type is second.event_type
        and first.configuration == second.configuration
        and first.validation_key == second.validation_key
        and first.failure_code is second.failure_code
        and first.evidence_ref == second.evidence_ref
        and first.idempotency_key == second.idempotency_key
    )


def _current_configuration_events(
    events: list[ProviderReadinessEvent],
) -> list[ProviderReadinessEvent]:
    for index in range(len(events) - 1, -1, -1):
        if events[index].event_type is ProviderReadinessEventType.CONFIGURED:
            return events[index:]
    return []


def _validate_transition(
    events: list[ProviderReadinessEvent], event: ProviderReadinessEvent
) -> None:
    if event.event_type is ProviderReadinessEventType.CONFIGURED:
        return

    current = _current_configuration_events(events)
    if not current or current[0].configuration != event.configuration:
        raise InvalidStateTransition("Provider 事件必须引用当前配置")

    if event.event_type is ProviderReadinessEventType.VALIDATION_STARTED:
        validation_events = [
            persisted
            for persisted in current
            if persisted.event_type
            in {
                ProviderReadinessEventType.VALIDATION_STARTED,
                ProviderReadinessEventType.VALIDATION_PASSED,
                ProviderReadinessEventType.VALIDATION_FAILED,
            }
        ]
        if (
            validation_events
            and validation_events[-1].event_type
            is not ProviderReadinessEventType.VALIDATION_FAILED
        ):
            raise InvalidStateTransition("Provider 当前配置不可开始验证")
        return

    if event.event_type in {
        ProviderReadinessEventType.VALIDATION_PASSED,
        ProviderReadinessEventType.VALIDATION_FAILED,
    }:
        matching = [
            persisted
            for persisted in current
            if persisted.validation_key == event.validation_key
        ]
        if (
            not matching
            or matching[-1].event_type
            is not ProviderReadinessEventType.VALIDATION_STARTED
        ):
            raise InvalidStateTransition("Provider 验证缺少匹配的已开始事实")
        return

    validation_events = [
        persisted
        for persisted in current
        if persisted.event_type
        in {
            ProviderReadinessEventType.VALIDATION_STARTED,
            ProviderReadinessEventType.VALIDATION_PASSED,
            ProviderReadinessEventType.VALIDATION_FAILED,
        }
    ]
    if (
        not validation_events
        or validation_events[-1].event_type
        is not ProviderReadinessEventType.VALIDATION_PASSED
    ):
        raise InvalidStateTransition("Provider runtime 缺少当前验证通过事实")
    latest_validation_sequence = validation_events[-1].sequence
    if latest_validation_sequence is None or any(
        persisted.event_type is ProviderReadinessEventType.RUNTIME_COMPOSED
        and persisted.sequence is not None
        and persisted.sequence > latest_validation_sequence
        for persisted in current
    ):
        raise InvalidStateTransition("Provider runtime 已组合或验证事实无效")


class ProviderReadinessRepositoryImpl(ProviderReadinessRepository):
    """在单一 tenant-bound 事务中串行化流追加。"""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")

    def _require_event_tenant(self, event: ProviderReadinessEvent) -> None:
        if event.tenant_id == self._tenant_id:
            return
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={
                "action": "append_provider_readiness_event",
                "tenant_id": str(self._tenant_id),
            },
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")

    def _stream_predicates(
        self,
        tenant_id: TenantId,
        provider: ProviderId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> tuple[ColumnElement[bool], ...]:
        return (
            ProviderReadinessEventRow.tenant_id == str(tenant_id),
            ProviderReadinessEventRow.provider == provider.value,
            ProviderReadinessEventRow.capability_set
            == _capability_values(capabilities),
        )

    async def list_events(
        self,
        tenant_id: TenantId,
        provider: ProviderId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> list[ProviderReadinessEvent]:
        self._require_tenant(tenant_id, "list_provider_readiness_events")
        rows = (
            await self._session.scalars(
                select(ProviderReadinessEventRow)
                .where(
                    *self._stream_predicates(
                        tenant_id, provider, capabilities
                    )
                )
                .order_by(ProviderReadinessEventRow.sequence.asc())
            )
        ).all()
        return [_to_event(row) for row in rows]

    async def append(
        self, tenant_id: TenantId, event: ProviderReadinessEvent
    ) -> ProviderReadinessEvent:
        self._require_tenant(tenant_id, "append_provider_readiness_event")
        self._require_event_tenant(event)
        capability_key = ",".join(
            capability.value for capability in event.capabilities
        )
        await self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended("
                "'provider-readiness:' || :tenant_id || ':' || :provider || "
                "':' || :capability_key, 0))"
            ),
            {
                "tenant_id": str(tenant_id),
                "provider": event.provider.value,
                "capability_key": capability_key,
            },
        )
        predicates = self._stream_predicates(
            tenant_id, event.provider, event.capabilities
        )
        existing_row = await self._session.scalar(
            select(ProviderReadinessEventRow).where(
                *predicates,
                ProviderReadinessEventRow.idempotency_key
                == str(event.idempotency_key),
            )
        )
        if existing_row is not None:
            existing = _to_event(existing_row)
            if _same_operation(existing, event):
                return existing
            raise IdempotencyConflict("Provider readiness 幂等键冲突")

        rows = (
            await self._session.scalars(
                select(ProviderReadinessEventRow)
                .where(*predicates)
                .order_by(ProviderReadinessEventRow.sequence.asc())
            )
        ).all()
        events = [_to_event(row) for row in rows]
        max_sequence = await self._session.scalar(
            select(func.max(ProviderReadinessEventRow.sequence)).where(
                *predicates
            )
        )
        persisted = replace(event, sequence=(max_sequence or 0) + 1)
        _validate_transition(events, persisted)
        self._session.add(_to_row(persisted))
        await self._session.flush()
        return persisted


__all__ = ("ProviderReadinessRepositoryImpl",)
