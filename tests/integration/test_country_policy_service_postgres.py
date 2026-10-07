"""国家政策服务在 PostgreSQL 中的提案、outbox 与基线原子性。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.compliance.errors import (
    CountryPolicyActivationConflictError,
    CountryPolicyBaseVersionConflictError,
    CountryPolicyIdempotencyConflictError,
)
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyActivationView,
    CountryPolicyApprovalFact,
    CountryPolicyProposalCreate,
    CountryPolicyProposalResult,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.session import create_engine_from
from shared.events.bus import EventBus
from shared.events.catalog import DomainEvent
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
)
from shared.schemas.provenance import SourceType

CountryPolicyActivation = importlib.import_module(
    "domains.compliance.models"
).CountryPolicyActivation

NOW = datetime(2026, 8, 24, 13, tzinfo=UTC)
PROPOSER = EmployeeId("emp_proposer")


def _command(**overrides: object) -> CountryPolicyProposalCreate:
    values: dict[str, object] = {
        "country": "Synthetic Market",
        "public_research_allowed": True,
        "contact_enrichment_allowed": False,
        "cold_b2b_email_allowed": False,
        "personal_data_basis_required": True,
        "subject_type_affects_judgment": True,
        "contact_type_affects_judgment": True,
        "opt_out_deadline_days": 30,
        "local_representative_required": False,
        "requirements": ["honor_opt_out", "retain.assessment_ref"],
        "notes": "Synthetic policy fixture reviewed by an employee.",
        "field_sources": {
            field: {
                "source_type": SourceType.EMPLOYEE_INPUT,
                "source_id": f"assessment:postgres:{index}",
            }
            for index, field in enumerate(
                sorted(DECISION_FIELDS, key=lambda item: item.value), 1
            )
        },
    }
    values.update(overrides)
    return CountryPolicyProposalCreate.model_validate(values)


def _boss(tenant_id: TenantId) -> ComplianceActor:
    return ComplianceActor(
        actor_id=str(PROPOSER),
        tenant_id=tenant_id,
        scope=ComplianceScope.TENANT,
        role="boss",
    )


def _system(tenant_id: TenantId) -> ComplianceActor:
    return ComplianceActor(
        actor_id="system:country-policy-reader",
        tenant_id=tenant_id,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )


def _approval(
    version: CountryPolicyProposalResult, suffix: str
) -> CountryPolicyApprovalFact:
    return CountryPolicyApprovalFact(
        approval_id=ApprovalId(f"apr_{suffix}"),
        approval_type="country_policy_change",
        change_set_ref=version.change_set_ref,
        decided_by=EmployeeId(f"emp_approver_{suffix}"),
        decided_at=NOW,
    )


class _Factory:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        fail_publish: bool = False,
        first_lookup_barrier: _FirstLookupBarrier | None = None,
        approval_race_barrier: _FirstLookupBarrier | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._fail_publish = fail_publish
        self._first_lookup_barrier = first_lookup_barrier
        self._approval_race_barrier = approval_race_barrier

    def __call__(self, tenant_id: TenantId) -> _ServiceUow:
        return _ServiceUow(
            SqlAlchemyComplianceUnitOfWork(
                self._session_factory, tenant_id, now=lambda: NOW
            ),
            fail_publish=self._fail_publish,
            first_lookup_barrier=self._first_lookup_barrier,
            approval_race_barrier=self._approval_race_barrier,
        )


class _FirstLookupBarrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._arrived += 1
            if self._arrived == self._parties:
                self._ready.set()
        await self._ready.wait()


class _FirstLookupBarrierVersions:
    def __init__(self, delegate: object, barrier: _FirstLookupBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier
        self._first_lookup = True

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> object:
        result = await self._delegate.find_by_idempotency_key(
            tenant_id, idempotency_key
        )
        if self._first_lookup:
            self._first_lookup = False
            await self._barrier.wait()
        return result


class _ApprovalRaceBarrierActivations:
    def __init__(self, delegate: object, barrier: _FirstLookupBarrier) -> None:
        self._delegate = delegate
        self._barrier = barrier
        self._first_lookup = True
        self._approval_lock_used = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    async def lock_approval(self, tenant_id: TenantId, approval_id: ApprovalId) -> None:
        self._approval_lock_used = True
        await self._barrier.wait()
        await self._delegate.lock_approval(tenant_id, approval_id)

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> object:
        result = await self._delegate.get_by_approval(tenant_id, approval_id)
        if self._first_lookup and not self._approval_lock_used:
            self._first_lookup = False
            await self._barrier.wait()
        return result


class _FailingPublishBus:
    def __init__(self, delegate: EventBus) -> None:
        self._delegate = delegate

    async def publish(self, event: DomainEvent) -> None:
        await self._delegate.publish(event)
        raise RuntimeError("injected service publish failure")

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)

    def subscribe(self, event_type: type[object], handler: object) -> None:
        del event_type, handler


class _ServiceUow:
    def __init__(
        self,
        delegate: SqlAlchemyComplianceUnitOfWork,
        *,
        fail_publish: bool,
        first_lookup_barrier: _FirstLookupBarrier | None,
        approval_race_barrier: _FirstLookupBarrier | None,
    ) -> None:
        self._delegate = delegate
        self._fail_publish = fail_publish
        self._first_lookup_barrier = first_lookup_barrier
        self._approval_race_barrier = approval_race_barrier

    async def __aenter__(self) -> Self:
        entered = await self._delegate.__aenter__()
        self.versions = (
            _FirstLookupBarrierVersions(entered.versions, self._first_lookup_barrier)
            if self._first_lookup_barrier is not None
            else entered.versions
        )
        self.provenance = entered.provenance
        self.activations = (
            _ApprovalRaceBarrierActivations(
                entered.activations, self._approval_race_barrier
            )
            if self._approval_race_barrier is not None
            else entered.activations
        )
        self.bus = (
            _FailingPublishBus(entered.bus) if self._fail_publish else entered.bus
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._delegate.__aexit__(exc_type, exc, traceback)


@pytest_asyncio.fixture
async def postgres_service_factory(
    db_url: str,
) -> AsyncIterator[
    tuple[
        Callable[[TenantId, bool], ComplianceServiceImpl],
        async_sessionmaker[AsyncSession],
        AsyncEngine,
    ]
]:
    engine = create_engine_from(db_url)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    def build(tenant_id: TenantId, fail_publish: bool = False) -> ComplianceServiceImpl:
        return ComplianceServiceImpl(
            _Factory(session_factory, fail_publish=fail_publish),
            Phase1ComplianceAuthorizer(tenant_id),
            now=lambda: NOW,
        )

    try:
        yield build, session_factory, engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_proposal_and_event_are_atomic_in_postgres(
    postgres_service_factory,
) -> None:
    build, _, engine = postgres_service_factory
    committed_tenant = TenantId("tenant-policy-svc-atomic-ok")
    committed = await build(committed_tenant).propose_country_policy(
        committed_tenant,
        _command(),
        actor=_boss(committed_tenant),
        idempotency_key=IdempotencyKey("service-atomic-success"),
    )

    async with engine.connect() as connection:
        committed_counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM country_policy_versions "
                    "WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM country_policy_field_provenance "
                    "WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM outbox_events WHERE tenant_id=:tenant "
                    "AND event_type='CountryPolicyVersionProposed')"
                ),
                {"tenant": str(committed_tenant)},
            )
        ).one()
        payload = await connection.scalar(
            text(
                "SELECT event_payload FROM outbox_events WHERE tenant_id=:tenant "
                "AND event_type='CountryPolicyVersionProposed'"
            ),
            {"tenant": str(committed_tenant)},
        )

    assert tuple(committed_counts) == (1, 9, 1)
    assert payload == {
        "tenant_id": str(committed_tenant),
        "occurred_at": NOW.isoformat(),
        "run_id": None,
        "country_policy_version_id": str(committed.country_policy_version_id),
        "country_key": "synthetic market",
        "content_hash": committed.content_hash,
        "proposed_by": str(PROPOSER),
    }

    failed_tenant = TenantId("tenant-policy-svc-atomic-fail")
    with pytest.raises(RuntimeError, match="injected service publish failure"):
        await build(failed_tenant, True).propose_country_policy(
            failed_tenant,
            _command(),
            actor=_boss(failed_tenant),
            idempotency_key=IdempotencyKey("service-atomic-failure"),
        )

    async with engine.connect() as connection:
        failed_counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM country_policy_versions "
                    "WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM country_policy_field_provenance "
                    "WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM outbox_events WHERE tenant_id=:tenant "
                    "AND event_type='CountryPolicyVersionProposed')"
                ),
                {"tenant": str(failed_tenant)},
            )
        ).one()
    assert tuple(failed_counts) == (0, 0, 0)


@pytest.mark.asyncio
async def test_change_snapshot_restores_persisted_revision_base(
    postgres_service_factory,
) -> None:
    build, session_factory, _ = postgres_service_factory
    tenant = TenantId("tenant-policy-svc-base")
    service = build(tenant)
    first = await service.propose_country_policy(
        tenant,
        _command(),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("persisted-base-first"),
    )
    first_view = await service.get_version(
        tenant, first.country_policy_version_id, actor=_boss(tenant)
    )
    activation = CountryPolicyActivation(
        tenant_id=tenant,
        activation_id=CountryPolicyActivationId("cpa_persisted-base-first"),
        activation_sequence=1,
        country_key=first.country_key,
        country_policy_version_id=first.country_policy_version_id,
        content_hash=first.content_hash,
        approval_id=ApprovalId("apr_persisted-base-first"),
        change_set_ref=first.change_set_ref,
        approved_by=EmployeeId("emp_independent_approver"),
        approved_at=NOW,
        activated_by="system:country-policy-workflow",
        activated_at=NOW,
    )
    async with SqlAlchemyComplianceUnitOfWork(
        session_factory, tenant, now=lambda: NOW
    ) as uow:
        await uow.activations.add(tenant, activation)

    revision = await service.propose_country_policy(
        tenant,
        _command(contact_enrichment_allowed=True),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("persisted-base-revision"),
    )
    snapshot = await service.get_change_snapshot(
        tenant, revision.country_policy_version_id, actor=_system(tenant)
    )

    assert snapshot.base is not None
    assert snapshot.current is not None
    assert (
        snapshot.base.country_policy_version_id == first_view.country_policy_version_id
    )
    assert snapshot.base.content_hash == first_view.content_hash
    assert (
        snapshot.current.country_policy_version_id
        == first_view.country_policy_version_id
    )
    assert snapshot.base_is_current is True


@pytest.mark.asyncio
async def test_same_tenant_idempotency_key_is_serialized_across_countries(
    postgres_service_factory,
) -> None:
    _, session_factory, engine = postgres_service_factory
    tenant = TenantId("tenant-policy-svc-idem-race")
    barrier = _FirstLookupBarrier(2)
    service = ComplianceServiceImpl(
        _Factory(session_factory, first_lookup_barrier=barrier),
        Phase1ComplianceAuthorizer(tenant),
        now=lambda: NOW,
    )
    shared_key = IdempotencyKey("same-tenant-cross-country")

    outcomes = await asyncio.gather(
        service.propose_country_policy(
            tenant,
            _command(country="Synthetic Market"),
            actor=_boss(tenant),
            idempotency_key=shared_key,
        ),
        service.propose_country_policy(
            tenant,
            _command(country="Synthetic Republic"),
            actor=_boss(tenant),
            idempotency_key=shared_key,
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, CountryPolicyProposalResult) for item in outcomes) == 1
    assert (
        sum(
            isinstance(item, CountryPolicyIdempotencyConflictError) for item in outcomes
        )
        == 1
    ), repr(outcomes)
    assert not any(isinstance(item, IntegrityError) for item in outcomes)
    async with engine.connect() as connection:
        counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM country_policy_versions "
                    "WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM country_policy_field_provenance "
                    "WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM outbox_events WHERE tenant_id=:tenant "
                    "AND event_type='CountryPolicyVersionProposed')"
                ),
                {"tenant": str(tenant)},
            )
        ).one()
    assert tuple(counts) == (1, 9, 1)


@pytest.mark.asyncio
async def test_two_approved_revisions_cannot_both_become_current(
    postgres_service_factory,
) -> None:
    build, _, engine = postgres_service_factory
    tenant = TenantId("tenant-policy-activation-race")
    service = build(tenant)
    initial = await service.propose_country_policy(
        tenant,
        _command(),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("activation-race-initial"),
    )
    await service.activate_country_policy(
        tenant,
        initial.country_policy_version_id,
        _approval(initial, "activation_race_initial"),
        actor=_system(tenant),
    )
    first = await service.propose_country_policy(
        tenant,
        _command(contact_enrichment_allowed=True),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("activation-race-first"),
    )
    second = await service.propose_country_policy(
        tenant,
        _command(cold_b2b_email_allowed=True),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("activation-race-second"),
    )

    outcomes = await asyncio.gather(
        service.activate_country_policy(
            tenant,
            first.country_policy_version_id,
            _approval(first, "activation_race_first"),
            actor=_system(tenant),
        ),
        service.activate_country_policy(
            tenant,
            second.country_policy_version_id,
            _approval(second, "activation_race_second"),
            actor=_system(tenant),
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, CountryPolicyActivationView) for item in outcomes) == 1
    assert (
        sum(
            isinstance(item, CountryPolicyBaseVersionConflictError) for item in outcomes
        )
        == 1
    ), repr(outcomes)
    async with engine.connect() as connection:
        activations = (
            await connection.execute(
                text(
                    "SELECT activation_sequence, country_policy_version_id "
                    "FROM country_policy_activations WHERE tenant_id=:tenant "
                    "ORDER BY activation_sequence"
                ),
                {"tenant": str(tenant)},
            )
        ).all()

    assert [row.activation_sequence for row in activations] == [1, 2]
    assert activations[1].country_policy_version_id in {
        str(first.country_policy_version_id),
        str(second.country_policy_version_id),
    }


@pytest.mark.asyncio
async def test_same_approval_concurrent_cross_country_returns_stable_conflict(
    postgres_service_factory,
) -> None:
    _, session_factory, engine = postgres_service_factory
    tenant = TenantId("tenant-policy-approval-race")
    barrier = _FirstLookupBarrier(2)
    service = ComplianceServiceImpl(
        _Factory(
            session_factory,
            approval_race_barrier=barrier,
        ),
        Phase1ComplianceAuthorizer(tenant),
        now=lambda: NOW,
    )
    first = await service.propose_country_policy(
        tenant,
        _command(country="Synthetic Market"),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("approval-race-first"),
    )
    second = await service.propose_country_policy(
        tenant,
        _command(country="Synthetic Republic"),
        actor=_boss(tenant),
        idempotency_key=IdempotencyKey("approval-race-second"),
    )
    shared_approval_id = ApprovalId("apr_cross_country_race")
    first_approval = _approval(first, "cross_country_first").model_copy(
        update={"approval_id": shared_approval_id}
    )
    second_approval = _approval(second, "cross_country_second").model_copy(
        update={"approval_id": shared_approval_id}
    )

    outcomes = await asyncio.gather(
        service.activate_country_policy(
            tenant,
            first.country_policy_version_id,
            first_approval,
            actor=_system(tenant),
        ),
        service.activate_country_policy(
            tenant,
            second.country_policy_version_id,
            second_approval,
            actor=_system(tenant),
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, CountryPolicyActivationView) for item in outcomes) == 1
    assert (
        sum(isinstance(item, CountryPolicyActivationConflictError) for item in outcomes)
        == 1
    ), repr(outcomes)
    assert not any(isinstance(item, IntegrityError) for item in outcomes)
    async with engine.connect() as connection:
        activation_count = await connection.scalar(
            text(
                "SELECT count(*) FROM country_policy_activations "
                "WHERE tenant_id=:tenant AND approval_id=:approval_id"
            ),
            {
                "tenant": str(tenant),
                "approval_id": str(shared_approval_id),
            },
        )

    assert activation_count == 1
