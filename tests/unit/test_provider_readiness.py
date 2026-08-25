"""Provider 运行就绪契约的纯状态机测试。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import IdempotencyKey, TenantId, new_id
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderCapability,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessActor,
    ProviderReadinessEvent,
    ProviderReadinessEventId,
    ProviderReadinessEventType,
    ProviderReadinessPermission,
    ProviderReadinessRepository,
    ProviderReadinessServiceImpl,
    ProviderReadinessState,
    ProviderReadinessUnavailableError,
    ProviderReadinessUnitOfWork,
    ProviderValidationFailureCode,
)

TENANT = TenantId("tenant-provider-readiness")
OTHER_TENANT = TenantId("tenant-other-provider-readiness")
HUNTER_CAPABILITIES = HUNTER_CONTACT_CAPABILITIES
CONFIG_V1 = ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
CONFIG_V2 = ProviderConfiguration.hunter_contacts("deploy-v2", "key-v2")
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)

CONFIGURER = ProviderReadinessActor(
    actor_id="operator:configure",
    tenant_id=TENANT,
    permissions=frozenset({ProviderReadinessPermission.CONFIGURE}),
)
VALIDATOR = ProviderReadinessActor(
    actor_id="operator:validate",
    tenant_id=TENANT,
    permissions=frozenset({ProviderReadinessPermission.VALIDATE}),
)
COMPOSER = ProviderReadinessActor(
    actor_id="system:compose",
    tenant_id=TENANT,
    permissions=frozenset({ProviderReadinessPermission.COMPOSE}),
)
READER = ProviderReadinessActor(
    actor_id="system:read",
    tenant_id=TENANT,
    permissions=frozenset({ProviderReadinessPermission.READ}),
)


class InMemoryProviderReadinessRepository:
    """测试专用 append-only 流，不模拟数据库的锁实现。"""

    def __init__(self, events: list[ProviderReadinessEvent]) -> None:
        self._events = events

    async def list_events(
        self,
        tenant_id: TenantId,
        provider: ProviderId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> list[ProviderReadinessEvent]:
        return [
            event
            for event in self._events
            if event.tenant_id == tenant_id
            and event.provider is provider
            and event.capabilities == capabilities
        ]

    async def append(
        self, tenant_id: TenantId, event: ProviderReadinessEvent
    ) -> ProviderReadinessEvent:
        if event.tenant_id != tenant_id:
            raise TenantIsolationViolation("测试仓储租户不匹配")
        persisted = replace(event, sequence=len(self._events) + 1)
        self._events.append(persisted)
        return persisted


class InMemoryProviderReadinessUnitOfWork:
    def __init__(self, events: list[ProviderReadinessEvent]) -> None:
        self.readiness: ProviderReadinessRepository = (
            InMemoryProviderReadinessRepository(events)
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


class FailingProviderReadinessRepository(InMemoryProviderReadinessRepository):
    async def list_events(
        self,
        tenant_id: TenantId,
        provider: ProviderId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> list[ProviderReadinessEvent]:
        del tenant_id, provider, capabilities
        raise RuntimeError("repository unavailable")


def in_memory_service(
    *, fail_reads: bool = False
) -> tuple[ProviderReadinessServiceImpl, list[ProviderReadinessEvent]]:
    events: list[ProviderReadinessEvent] = []

    def factory(tenant_id: TenantId) -> ProviderReadinessUnitOfWork:
        if tenant_id != TENANT:
            raise TenantIsolationViolation("测试 UoW 租户不匹配")
        if fail_reads:
            uow = InMemoryProviderReadinessUnitOfWork(events)
            uow.readiness = FailingProviderReadinessRepository(events)
            return uow
        return InMemoryProviderReadinessUnitOfWork(events)

    return ProviderReadinessServiceImpl(factory, runtime_actor=READER, now=lambda: NOW), events


async def declare_and_compose(
    service: ProviderReadinessServiceImpl,
    configuration: ProviderConfiguration,
) -> None:
    await service.declare_configuration(
        TENANT,
        configuration,
        actor=CONFIGURER,
        idempotency_key=IdempotencyKey(f"cfg:{configuration.configuration_version}"),
    )
    await service.mark_validation_started(
        TENANT,
        configuration.configuration_hash,
        validation_key=IdempotencyKey(
            f"validate:{configuration.configuration_version}"
        ),
        actor=VALIDATOR,
    )
    await service.mark_validation_passed(
        TENANT,
        configuration.configuration_hash,
        validation_key=IdempotencyKey(
            f"validate:{configuration.configuration_version}"
        ),
        evidence_ref="tool-call:validation",
        actor=VALIDATOR,
    )
    await service.mark_runtime_composed(
        TENANT,
        configuration.configuration_hash,
        actor=COMPOSER,
        idempotency_key=IdempotencyKey(
            f"runtime:{configuration.configuration_version}"
        ),
    )


def stored_event(
    *,
    sequence: int,
    configuration: ProviderConfiguration,
    event_type: ProviderReadinessEventType,
    validation_key: IdempotencyKey | None = None,
    evidence_ref: str | None = None,
) -> ProviderReadinessEvent:
    return ProviderReadinessEvent(
        tenant_id=TENANT,
        event_id=ProviderReadinessEventId(new_id("pre")),
        provider=ProviderId.HUNTER,
        capabilities=HUNTER_CAPABILITIES,
        sequence=sequence,
        event_type=event_type,
        configuration=configuration,
        validation_key=validation_key,
        failure_code=None,
        evidence_ref=evidence_ref,
        actor_id="system:stored-event",
        occurred_at=NOW,
        idempotency_key=IdempotencyKey(f"stored:{sequence}"),
    )


def test_hunter_configuration_hash_uses_safe_exact_metadata() -> None:
    first = ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
    replay = ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
    rotated = ProviderConfiguration.hunter_contacts("deploy-v2", "key-v2")

    assert first == replay
    assert first.configuration_hash == replay.configuration_hash
    assert first.configuration_hash != rotated.configuration_hash
    assert first.capabilities == (
        ProviderCapability.CONTACT_ENRICHMENT,
        ProviderCapability.CONTACT_VERIFICATION,
    )
    assert "secret" not in repr(first).casefold()


@pytest.mark.parametrize("value", ["", " V1", "v1 ", "V1", "v/1", "x" * 33])
def test_configuration_versions_are_canonical(value: str) -> None:
    with pytest.raises(ValidationError):
        ProviderConfiguration.hunter_contacts(value, "key-v1")


@pytest.mark.parametrize(
    ("connector_profile_version", "transport_profile"),
    [
        ("hunter-contacts-v2", "hunter_api_v2_fixed_host"),
        ("hunter-contacts-v1", "hunter_api_v3_fixed_host"),
    ],
)
def test_direct_configuration_rejects_nonfixed_hunter_profiles(
    connector_profile_version: str, transport_profile: str
) -> None:
    payload = {
        "provider": "hunter",
        "capabilities": ["contact.enrich", "contact.verify"],
        "connector_profile_version": connector_profile_version,
        "transport_profile": transport_profile,
        "configuration_version": "deploy-v1",
        "api_key_version": "key-v1",
    }

    with pytest.raises(ValidationError):
        ProviderConfiguration(
            provider=ProviderId.HUNTER,
            capabilities=HUNTER_CAPABILITIES,
            configuration_version="deploy-v1",
            connector_profile_version=connector_profile_version,
            transport_profile=transport_profile,
            api_key_version="key-v1",
            configuration_hash=hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        )


@pytest.mark.asyncio
async def test_no_configuration_is_not_configured() -> None:
    service, _ = in_memory_service()

    snapshot = await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)

    assert snapshot.state is ProviderReadinessState.PROVIDER_NOT_CONFIGURED
    assert snapshot.configuration is None


@pytest.mark.asyncio
async def test_new_configuration_invalidates_old_validation_and_runtime() -> None:
    service, _ = in_memory_service()
    await declare_and_compose(service, CONFIG_V1)
    assert (
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    ).state is ProviderReadinessState.READY

    await service.declare_configuration(
        TENANT,
        CONFIG_V2,
        actor=CONFIGURER,
        idempotency_key=IdempotencyKey("cfg:v2"),
    )
    snapshot = await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)

    assert snapshot.state is ProviderReadinessState.VALIDATION_NOT_RUN
    assert snapshot.configuration == CONFIG_V2


@pytest.mark.asyncio
async def test_started_without_terminal_fact_is_inconclusive() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(
        TENANT,
        CONFIG_V1,
        actor=CONFIGURER,
        idempotency_key=IdempotencyKey("cfg:v1"),
    )
    await service.mark_validation_started(
        TENANT,
        CONFIG_V1.configuration_hash,
        validation_key=IdempotencyKey("validate:v1"),
        actor=VALIDATOR,
    )

    assert (
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    ).state is ProviderReadinessState.VALIDATION_INCONCLUSIVE


@pytest.mark.asyncio
async def test_mismatched_stored_events_cannot_make_runtime_guard_ready() -> None:
    service, events = in_memory_service()
    events.extend(
        [
            stored_event(
                sequence=1,
                configuration=CONFIG_V2,
                event_type=ProviderReadinessEventType.CONFIGURED,
            ),
            stored_event(
                sequence=2,
                configuration=CONFIG_V1,
                event_type=ProviderReadinessEventType.VALIDATION_PASSED,
                validation_key=IdempotencyKey("validate:v1"),
                evidence_ref="tool-call:validation",
            ),
            stored_event(
                sequence=3,
                configuration=CONFIG_V1,
                event_type=ProviderReadinessEventType.RUNTIME_COMPOSED,
            ),
        ]
    )

    with pytest.raises(TransientError):
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    with pytest.raises(ProviderReadinessUnavailableError) as captured:
        await service.require_current(TENANT, CONFIG_V2.configuration_hash)

    assert str(captured.value) == "Provider 配置当前不可用"


@pytest.mark.asyncio
async def test_runtime_fact_without_current_passed_validation_is_rejected_on_read() -> None:
    service, events = in_memory_service()
    events.extend(
        [
            stored_event(
                sequence=1,
                configuration=CONFIG_V2,
                event_type=ProviderReadinessEventType.CONFIGURED,
            ),
            stored_event(
                sequence=2,
                configuration=CONFIG_V2,
                event_type=ProviderReadinessEventType.VALIDATION_STARTED,
                validation_key=IdempotencyKey("validate:v2"),
            ),
            stored_event(
                sequence=3,
                configuration=CONFIG_V2,
                event_type=ProviderReadinessEventType.RUNTIME_COMPOSED,
            ),
        ]
    )

    with pytest.raises(TransientError):
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    with pytest.raises(ProviderReadinessUnavailableError):
        await service.require_current(TENANT, CONFIG_V2.configuration_hash)


@pytest.mark.asyncio
async def test_failed_validation_is_not_ready() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1")
    )
    await service.mark_validation_started(
        TENANT,
        CONFIG_V1.configuration_hash,
        validation_key=IdempotencyKey("validate:v1"),
        actor=VALIDATOR,
    )
    await service.mark_validation_failed(
        TENANT,
        CONFIG_V1.configuration_hash,
        validation_key=IdempotencyKey("validate:v1"),
        failure_code=ProviderValidationFailureCode.AUTH_REQUIRED,
        actor=VALIDATOR,
    )

    snapshot = await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)

    assert snapshot.state is ProviderReadinessState.VALIDATION_FAILED
    assert snapshot.failure_code is ProviderValidationFailureCode.AUTH_REQUIRED


@pytest.mark.asyncio
async def test_passed_validation_without_runtime_is_not_composed() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1")
    )
    await service.mark_validation_started(
        TENANT,
        CONFIG_V1.configuration_hash,
        validation_key=IdempotencyKey("validate:v1"),
        actor=VALIDATOR,
    )
    await service.mark_validation_passed(
        TENANT,
        CONFIG_V1.configuration_hash,
        validation_key=IdempotencyKey("validate:v1"),
        evidence_ref="tool-call:validation",
        actor=VALIDATOR,
    )

    assert (
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    ).state is ProviderReadinessState.RUNTIME_NOT_COMPOSED


@pytest.mark.asyncio
async def test_passed_validation_with_matching_runtime_is_ready() -> None:
    service, _ = in_memory_service()
    await declare_and_compose(service, CONFIG_V1)

    assert (
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=READER)
    ).state is ProviderReadinessState.READY


@pytest.mark.asyncio
async def test_same_idempotency_key_with_same_configuration_is_a_no_op() -> None:
    service, events = in_memory_service()
    first = await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1")
    )
    replay = await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1")
    )

    assert replay == first
    assert len(events) == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_configuration_conflicts() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:once")
    )

    with pytest.raises(IdempotencyConflict):
        await service.declare_configuration(
            TENANT,
            CONFIG_V2,
            actor=CONFIGURER,
            idempotency_key=IdempotencyKey("cfg:once"),
        )


@pytest.mark.asyncio
async def test_terminal_validation_without_matching_started_is_rejected() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1")
    )

    with pytest.raises(InvalidStateTransition):
        await service.mark_validation_passed(
            TENANT,
            CONFIG_V1.configuration_hash,
            validation_key=IdempotencyKey("validate:v1"),
            evidence_ref="tool-call:validation",
            actor=VALIDATOR,
        )


@pytest.mark.asyncio
async def test_runtime_without_matching_pass_is_rejected() -> None:
    service, _ = in_memory_service()
    await service.declare_configuration(
        TENANT, CONFIG_V1, actor=CONFIGURER, idempotency_key=IdempotencyKey("cfg:v1")
    )
    await service.mark_validation_started(
        TENANT,
        CONFIG_V1.configuration_hash,
        validation_key=IdempotencyKey("validate:v1"),
        actor=VALIDATOR,
    )

    with pytest.raises(InvalidStateTransition):
        await service.mark_runtime_composed(
            TENANT,
            CONFIG_V1.configuration_hash,
            actor=COMPOSER,
            idempotency_key=IdempotencyKey("runtime:v1"),
        )


@pytest.mark.asyncio
async def test_cross_tenant_actor_is_rejected() -> None:
    service, _ = in_memory_service()
    foreign_reader = ProviderReadinessActor(
        actor_id="system:foreign-read",
        tenant_id=OTHER_TENANT,
        permissions=frozenset({ProviderReadinessPermission.READ}),
    )

    with pytest.raises(TenantIsolationViolation):
        await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=foreign_reader)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "actor"),
    [
        ("configure", READER),
        ("validate", READER),
        ("compose", READER),
        ("read", CONFIGURER),
    ],
)
async def test_permissions_are_exactly_fail_closed(
    method: str, actor: ProviderReadinessActor
) -> None:
    service, _ = in_memory_service()

    with pytest.raises(PermissionDenied):
        if method == "configure":
            await service.declare_configuration(
                TENANT,
                CONFIG_V1,
                actor=actor,
                idempotency_key=IdempotencyKey("cfg:v1"),
            )
        elif method == "validate":
            await service.mark_validation_started(
                TENANT,
                CONFIG_V1.configuration_hash,
                validation_key=IdempotencyKey("validate:v1"),
                actor=actor,
            )
        elif method == "compose":
            await service.mark_runtime_composed(
                TENANT,
                CONFIG_V1.configuration_hash,
                actor=actor,
                idempotency_key=IdempotencyKey("runtime:v1"),
            )
        else:
            await service.get_snapshot(TENANT, HUNTER_CAPABILITIES, actor=actor)


@pytest.mark.asyncio
async def test_runtime_guard_requires_matching_current_ready_configuration() -> None:
    service, _ = in_memory_service()
    await declare_and_compose(service, CONFIG_V1)

    await service.require_current(TENANT, CONFIG_V1.configuration_hash)

    with pytest.raises(ProviderReadinessUnavailableError) as captured:
        await service.require_current(TENANT, CONFIG_V2.configuration_hash)
    assert str(captured.value) == "Provider 配置当前不可用"
    assert "secret" not in repr(captured.value).casefold()


@pytest.mark.asyncio
async def test_runtime_guard_sanitizes_repository_failures() -> None:
    service, _ = in_memory_service(fail_reads=True)

    with pytest.raises(ProviderReadinessUnavailableError) as captured:
        await service.require_current(TENANT, CONFIG_V1.configuration_hash)

    assert str(captured.value) == "Provider 配置当前不可用"
    assert "repository unavailable" not in repr(captured.value)
