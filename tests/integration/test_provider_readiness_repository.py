"""Provider readiness append-only PostgreSQL 流的隔离、并发与迁移验收。"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.db.repositories.provider_readiness import ProviderReadinessRepositoryImpl
from infra.db.tables import ProviderReadinessEventRow
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    TenantIsolationViolation,
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
)

NOW = datetime(2026, 8, 25, 9, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def db_factory(
    integration_engine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(
        bind=integration_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
def alembic_runner(db_url: str) -> Callable[[str, str], None]:
    def _run(command: str, revision: str) -> None:
        result = subprocess.run(
            ["alembic", command, revision],
            cwd=_REPO_ROOT,
            env={**os.environ, "DATABASE_URL": db_url},
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, "Provider readiness Alembic 迁移失败"

    return _run


def _actor(tenant: TenantId, suffix: str) -> ProviderReadinessActor:
    return ProviderReadinessActor(
        actor_id=f"employee:{suffix}",
        tenant_id=tenant,
        permissions=frozenset(ProviderReadinessPermission),
    )


def _service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, suffix: str
) -> ProviderReadinessServiceImpl:
    return ProviderReadinessServiceImpl(
        lambda requested_tenant: SqlAlchemyProviderReadinessUnitOfWork(
            factory, requested_tenant, now=lambda: NOW
        ),
        runtime_actor=_actor(tenant, f"runtime-{suffix}"),
        now=lambda: NOW,
    )


def _event(
    tenant: TenantId,
    configuration: ProviderConfiguration,
    event_type: ProviderReadinessEventType,
    idempotency_key: str,
    *,
    actor_id: str = "employee:repository-test",
    validation_key: str | None = None,
    evidence_ref: str | None = None,
    occurred_at: datetime = NOW,
) -> ProviderReadinessEvent:
    return ProviderReadinessEvent(
        tenant_id=tenant,
        event_id=ProviderReadinessEventId(new_id("pre")),
        provider=ProviderId.HUNTER,
        capabilities=HUNTER_CONTACT_CAPABILITIES,
        sequence=None,
        event_type=event_type,
        configuration=configuration,
        validation_key=(
            IdempotencyKey(validation_key) if validation_key is not None else None
        ),
        failure_code=None,
        evidence_ref=evidence_ref,
        actor_id=actor_id,
        occurred_at=occurred_at,
        idempotency_key=IdempotencyKey(idempotency_key),
    )


async def _append(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    event: ProviderReadinessEvent,
) -> ProviderReadinessEvent:
    async with SqlAlchemyProviderReadinessUnitOfWork(factory, tenant) as uow:
        return await uow.readiness.append(tenant, event)


class _AppendBarrierRepository:
    """仅在真实 Repository append 前同步两个 Service 调用。"""

    def __init__(
        self,
        delegate: ProviderReadinessRepository,
        ready: asyncio.Queue[None],
        release: asyncio.Event,
    ) -> None:
        self._delegate = delegate
        self._ready = ready
        self._release = release

    async def list_events(
        self,
        tenant_id: TenantId,
        provider: ProviderId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> list[ProviderReadinessEvent]:
        return await self._delegate.list_events(
            tenant_id, provider, capabilities
        )

    async def append(
        self, tenant_id: TenantId, event: ProviderReadinessEvent
    ) -> ProviderReadinessEvent:
        self._ready.put_nowait(None)
        await self._release.wait()
        return await self._delegate.append(tenant_id, event)


class _AppendBarrierUnitOfWork:
    """保留真实 SQLAlchemy UoW/session，只在 append 边界加 barrier。"""

    readiness: ProviderReadinessRepository

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        ready: asyncio.Queue[None],
        release: asyncio.Event,
    ) -> None:
        self._delegate = SqlAlchemyProviderReadinessUnitOfWork(
            factory, tenant_id
        )
        self._ready = ready
        self._release = release

    async def __aenter__(self) -> Self:
        entered = await self._delegate.__aenter__()
        self.readiness = _AppendBarrierRepository(
            entered.readiness, self._ready, self._release
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._delegate.__aexit__(exc_type, exc, traceback)


async def test_configuration_and_validation_round_trip_is_tenant_scoped(
    db_factory,
) -> None:
    tenant = TenantId("tenant-readiness-roundtrip")
    actor = _actor(tenant, "roundtrip")
    service = _service(db_factory, tenant, "roundtrip")
    configuration = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")

    await service.declare_configuration(
        tenant,
        configuration,
        actor=actor,
        idempotency_key=IdempotencyKey("configure-roundtrip"),
    )
    await service.mark_validation_started(
        tenant,
        configuration.configuration_hash,
        validation_key=IdempotencyKey("validate-roundtrip"),
        actor=actor,
    )
    await service.mark_validation_passed(
        tenant,
        configuration.configuration_hash,
        validation_key=IdempotencyKey("validate-roundtrip"),
        evidence_ref="tool-call:roundtrip",
        actor=actor,
    )

    snapshot = await service.get_snapshot(
        tenant, HUNTER_CONTACT_CAPABILITIES, actor=actor
    )

    assert snapshot.tenant_id == tenant
    assert snapshot.configuration == configuration
    assert snapshot.state is ProviderReadinessState.RUNTIME_NOT_COMPOSED
    assert [event.sequence for event in snapshot.events] == [1, 2, 3]
    assert {event.actor_id for event in snapshot.events} == {"employee:roundtrip"}


async def test_other_tenant_cannot_read_or_append_stream(db_factory) -> None:
    tenant_a = TenantId("tenant-readiness-isolation-a")
    tenant_b = TenantId("tenant-readiness-isolation-b")
    configuration = ProviderConfiguration.hunter_contacts("config-shared", "key-v1")
    actor_a = _actor(tenant_a, "isolation-a")
    actor_b = _actor(tenant_b, "isolation-b")
    service_a = _service(db_factory, tenant_a, "isolation-a")
    service_b = _service(db_factory, tenant_b, "isolation-b")

    snapshot_a = await service_a.declare_configuration(
        tenant_a,
        configuration,
        actor=actor_a,
        idempotency_key=IdempotencyKey("configure-shared"),
    )
    snapshot_b = await service_b.declare_configuration(
        tenant_b,
        configuration,
        actor=actor_b,
        idempotency_key=IdempotencyKey("configure-shared"),
    )

    assert {event.actor_id for event in snapshot_a.events} == {
        "employee:isolation-a"
    }
    assert {event.actor_id for event in snapshot_b.events} == {
        "employee:isolation-b"
    }
    assert {event.event_id for event in snapshot_a.events}.isdisjoint(
        event.event_id for event in snapshot_b.events
    )

    session = db_factory()
    repository = ProviderReadinessRepositoryImpl(session, tenant_a)
    try:
        with pytest.raises(TenantIsolationViolation):
            await repository.list_events(
                tenant_b, ProviderId.HUNTER, HUNTER_CONTACT_CAPABILITIES
            )
        with pytest.raises(TenantIsolationViolation):
            await repository.append(
                tenant_b,
                _event(
                    tenant_b,
                    configuration,
                    ProviderReadinessEventType.CONFIGURED,
                    "cross-tenant-append",
                ),
            )
    finally:
        await session.close()


async def test_same_stream_concurrent_validation_starts_allow_exactly_one(
    db_factory,
) -> None:
    tenant = TenantId("tenant-readiness-concurrency")
    configuration = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")
    configured = await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            configuration,
            ProviderReadinessEventType.CONFIGURED,
            "configure-concurrency",
        ),
    )
    assert configured.sequence == 1
    ready: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    async def _append_after_barrier(suffix: str) -> ProviderReadinessEvent:
        session = db_factory()
        repository = ProviderReadinessRepositoryImpl(session, tenant)
        try:
            ready.put_nowait(None)
            await release.wait()
            persisted = await repository.append(
                tenant,
                _event(
                    tenant,
                    configuration,
                    ProviderReadinessEventType.VALIDATION_STARTED,
                    f"validation:started:{suffix}",
                    validation_key=suffix,
                ),
            )
            await session.commit()
            return persisted
        finally:
            await session.close()

    tasks = [
        asyncio.create_task(_append_after_barrier("concurrent-a")),
        asyncio.create_task(_append_after_barrier("concurrent-b")),
    ]
    await ready.get()
    await ready.get()
    release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    persisted = [
        outcome for outcome in outcomes if isinstance(outcome, ProviderReadinessEvent)
    ]
    rejected = [
        outcome for outcome in outcomes if isinstance(outcome, InvalidStateTransition)
    ]
    assert len(persisted) == 1
    assert persisted[0].sequence == 2
    assert len(rejected) == 1
    async with SqlAlchemyProviderReadinessUnitOfWork(db_factory, tenant) as uow:
        events = await uow.readiness.list_events(
            tenant, ProviderId.HUNTER, HUNTER_CONTACT_CAPABILITIES
        )
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        ProviderReadinessEventType.CONFIGURED,
        ProviderReadinessEventType.VALIDATION_STARTED,
    ]


async def test_same_idempotency_key_same_payload_is_noop(db_factory) -> None:
    tenant = TenantId("tenant-ready-idem-replay")
    configuration = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")
    first = await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            configuration,
            ProviderReadinessEventType.CONFIGURED,
            "configure-idempotent-replay",
            actor_id="employee:first-actor",
        ),
    )
    replay = await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            configuration,
            ProviderReadinessEventType.CONFIGURED,
            "configure-idempotent-replay",
            actor_id="employee:replay-actor",
            occurred_at=NOW + timedelta(minutes=1),
        ),
    )

    assert replay == first
    async with db_factory() as session:
        count = await session.scalar(
            select(text("count(*)")).select_from(ProviderReadinessEventRow).where(
                ProviderReadinessEventRow.tenant_id == str(tenant)
            )
        )
    assert count == 1


async def test_concurrent_service_same_operation_returns_canonical_replay(
    db_factory,
) -> None:
    tenant = TenantId("tenant-ready-service-replay")
    actor = _actor(tenant, "service-replay")
    configuration = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")
    ready: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    def _service_at(occurred_at: datetime) -> ProviderReadinessServiceImpl:
        return ProviderReadinessServiceImpl(
            lambda requested_tenant: _AppendBarrierUnitOfWork(
                db_factory, requested_tenant, ready, release
            ),
            runtime_actor=_actor(tenant, "runtime-service-replay"),
            now=lambda: occurred_at,
        )

    tasks = [
        asyncio.create_task(
            _service_at(NOW).declare_configuration(
                tenant,
                configuration,
                actor=actor,
                idempotency_key=IdempotencyKey("configure-service-replay"),
            )
        ),
        asyncio.create_task(
            _service_at(NOW + timedelta(minutes=1)).declare_configuration(
                tenant,
                configuration,
                actor=actor,
                idempotency_key=IdempotencyKey("configure-service-replay"),
            )
        ),
    ]
    await ready.get()
    await ready.get()
    release.set()
    snapshots = await asyncio.gather(*tasks)

    assert snapshots[0].state is ProviderReadinessState.VALIDATION_NOT_RUN
    assert snapshots[1].state is ProviderReadinessState.VALIDATION_NOT_RUN
    assert snapshots[0].events == snapshots[1].events
    assert len(snapshots[0].events) == 1
    async with db_factory() as session:
        rows = await session.scalars(
            select(ProviderReadinessEventRow).where(
                ProviderReadinessEventRow.tenant_id == str(tenant)
            )
        )
    assert len(rows.all()) == 1


async def test_same_idempotency_key_different_payload_conflicts(db_factory) -> None:
    tenant = TenantId("tenant-ready-idem-conflict")
    first_configuration = ProviderConfiguration.hunter_contacts(
        "config-v1", "key-v1"
    )
    second_configuration = ProviderConfiguration.hunter_contacts(
        "config-v2", "key-v1"
    )
    await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            first_configuration,
            ProviderReadinessEventType.CONFIGURED,
            "configure-conflict",
        ),
    )

    with pytest.raises(IdempotencyConflict):
        await _append(
            db_factory,
            tenant,
            _event(
                tenant,
                second_configuration,
                ProviderReadinessEventType.CONFIGURED,
                "configure-conflict",
            ),
        )


async def test_new_configuration_invalidates_persisted_old_runtime(db_factory) -> None:
    tenant = TenantId("tenant-ready-new-config")
    actor = _actor(tenant, "new-configuration")
    service = _service(db_factory, tenant, "new-configuration")
    first = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")
    second = ProviderConfiguration.hunter_contacts("config-v2", "key-v2")

    await service.declare_configuration(
        tenant,
        first,
        actor=actor,
        idempotency_key=IdempotencyKey("configure-v1"),
    )
    await service.mark_validation_started(
        tenant,
        first.configuration_hash,
        validation_key=IdempotencyKey("validate-v1"),
        actor=actor,
    )
    await service.mark_validation_passed(
        tenant,
        first.configuration_hash,
        validation_key=IdempotencyKey("validate-v1"),
        evidence_ref="tool-call:validate-v1",
        actor=actor,
    )
    await service.mark_runtime_composed(
        tenant,
        first.configuration_hash,
        actor=actor,
        idempotency_key=IdempotencyKey("runtime-v1"),
    )
    ready = await service.get_snapshot(
        tenant, HUNTER_CONTACT_CAPABILITIES, actor=actor
    )
    assert ready.state is ProviderReadinessState.READY

    current = await service.declare_configuration(
        tenant,
        second,
        actor=actor,
        idempotency_key=IdempotencyKey("configure-v2"),
    )

    assert current.configuration == second
    assert current.state is ProviderReadinessState.VALIDATION_NOT_RUN
    assert [event.sequence for event in current.events] == [5]
    async with SqlAlchemyProviderReadinessUnitOfWork(db_factory, tenant) as uow:
        persisted = await uow.readiness.list_events(
            tenant, ProviderId.HUNTER, HUNTER_CONTACT_CAPABILITIES
        )
    assert [event.sequence for event in persisted] == [1, 2, 3, 4, 5]


async def test_runtime_composed_requires_current_passed_configuration(
    db_factory,
) -> None:
    tenant = TenantId("tenant-readiness-runtime-current")
    first = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")
    second = ProviderConfiguration.hunter_contacts("config-v2", "key-v2")
    await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            first,
            ProviderReadinessEventType.CONFIGURED,
            "configure-runtime-v1",
        ),
    )
    await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            first,
            ProviderReadinessEventType.VALIDATION_STARTED,
            "validation:started:runtime-v1",
            validation_key="runtime-v1",
        ),
    )
    await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            first,
            ProviderReadinessEventType.VALIDATION_PASSED,
            "validation:passed:runtime-v1",
            validation_key="runtime-v1",
            evidence_ref="tool-call:runtime-v1",
        ),
    )
    await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            second,
            ProviderReadinessEventType.CONFIGURED,
            "configure-runtime-v2",
        ),
    )

    with pytest.raises(InvalidStateTransition):
        await _append(
            db_factory,
            tenant,
            _event(
                tenant,
                first,
                ProviderReadinessEventType.RUNTIME_COMPOSED,
                "runtime-stale-v1",
            ),
        )


async def test_database_rejects_update_and_delete(db_factory) -> None:
    tenant = TenantId("tenant-readiness-append-only")
    configuration = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")
    persisted = await _append(
        db_factory,
        tenant,
        _event(
            tenant,
            configuration,
            ProviderReadinessEventType.CONFIGURED,
            "configure-append-only",
        ),
    )

    async with db_factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "UPDATE provider_readiness_events SET actor_id='employee:changed' "
                    "WHERE tenant_id=:tenant AND provider_readiness_event_id=:event_id"
                ),
                {"tenant": str(tenant), "event_id": str(persisted.event_id)},
            )
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                text(
                    "DELETE FROM provider_readiness_events "
                    "WHERE tenant_id=:tenant AND provider_readiness_event_id=:event_id"
                ),
                {"tenant": str(tenant), "event_id": str(persisted.event_id)},
            )
        await session.rollback()


async def test_migration_upgrade_downgrade_upgrade_round_trip(
    alembic_runner,
) -> None:
    alembic_runner("downgrade", "0032")
    alembic_runner("upgrade", "0033")
    alembic_runner("downgrade", "0032")
    alembic_runner("upgrade", "0033")
