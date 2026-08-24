"""国家政策只允许精确审批事实驱动的串行激活。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from domains.compliance.errors import (
    CountryPolicyActivationConflictError,
    CountryPolicyApprovalFactInvalidError,
    CountryPolicyBaseVersionConflictError,
)
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyApprovalFact,
    CountryPolicyProposalCreate,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
)
from shared.schemas.provenance import SourceType

_models = importlib.import_module("domains.compliance.models")
CountryPolicyActivation = _models.CountryPolicyActivation
CountryPolicyVersion = _models.CountryPolicyVersion

NOW = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)
TENANT = TenantId("tenant-policy-activation")
PROPOSER = EmployeeId("emp_policy_proposer")
APPROVER = EmployeeId("emp_policy_approver")
SYSTEM_ID = "system:country-policy-workflow"


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
                "source_id": f"assessment:activation:{index}",
            }
            for index, field in enumerate(
                sorted(DECISION_FIELDS, key=lambda item: item.value), 1
            )
        },
    }
    values.update(overrides)
    return CountryPolicyProposalCreate.model_validate(values)


def _boss() -> ComplianceActor:
    return ComplianceActor(
        actor_id=str(PROPOSER),
        tenant_id=TENANT,
        scope=ComplianceScope.TENANT,
        role="boss",
    )


def _system() -> ComplianceActor:
    return ComplianceActor(
        actor_id=SYSTEM_ID,
        tenant_id=TENANT,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )


class _Store:
    def __init__(self) -> None:
        self.versions: dict[CountryPolicyVersionId, CountryPolicyVersion] = {}
        self.activations: list[CountryPolicyActivation] = []
        self.country_locks: dict[str, asyncio.Lock] = {}
        self.version_reads = 0


class _Versions:
    def __init__(self, store: _Store, uow: _Uow) -> None:
        self._store = store
        self._uow = uow

    async def get(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyVersion | None:
        self._store.version_reads += 1
        version = self._store.versions.get(version_id)
        if version is None or version.tenant_id != tenant_id:
            return None
        return version

    async def lock_country(self, tenant_id: TenantId, country_key: str) -> None:
        del tenant_id
        lock = self._store.country_locks.setdefault(country_key, asyncio.Lock())
        await lock.acquire()
        self._uow.locks.append(lock)


class _Activations:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def get_current(
        self, tenant_id: TenantId, country_key: str
    ) -> CountryPolicyActivation | None:
        matches = [
            activation
            for activation in self._store.activations
            if activation.tenant_id == tenant_id
            and activation.country_key == country_key
        ]
        return max(matches, key=lambda item: item.activation_sequence, default=None)

    async def get_by_version(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyActivation | None:
        return next(
            (
                activation
                for activation in self._store.activations
                if activation.tenant_id == tenant_id
                and activation.country_policy_version_id == version_id
            ),
            None,
        )

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CountryPolicyActivation | None:
        return next(
            (
                activation
                for activation in self._store.activations
                if activation.tenant_id == tenant_id
                and activation.approval_id == approval_id
            ),
            None,
        )

    async def next_activation_sequence(
        self, tenant_id: TenantId, country_key: str
    ) -> int:
        return (
            max(
                (
                    activation.activation_sequence
                    for activation in self._store.activations
                    if activation.tenant_id == tenant_id
                    and activation.country_key == country_key
                ),
                default=0,
            )
            + 1
        )

    async def add(
        self, tenant_id: TenantId, activation: CountryPolicyActivation
    ) -> None:
        assert activation.tenant_id == tenant_id
        self._store.activations.append(activation)


class _UnusedProvenance:
    pass


class _UnusedBus:
    pass


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.locks: list[asyncio.Lock] = []
        self.versions = _Versions(store, self)
        self.activations = _Activations(store)
        self.provenance = _UnusedProvenance()
        self.bus = _UnusedBus()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        for lock in reversed(self.locks):
            lock.release()


class _Factory:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def __call__(self, tenant_id: TenantId) -> _Uow:
        assert tenant_id == TENANT
        return _Uow(self._store)


def _service(
    store: _Store, *, now: Callable[[], datetime] = lambda: NOW
) -> ComplianceServiceImpl:
    return ComplianceServiceImpl(
        _Factory(store), Phase1ComplianceAuthorizer(TENANT), now=now
    )


def _seed_version(
    store: _Store,
    suffix: str,
    *,
    version_number: int,
    base: CountryPolicyVersion | None = None,
) -> CountryPolicyVersion:
    version = CountryPolicyVersion.from_command(
        tenant_id=TENANT,
        version_id=CountryPolicyVersionId(f"cpp_{suffix}"),
        version_number=version_number,
        command=_command(contact_enrichment_allowed=version_number > 1),
        base_version_id=(None if base is None else base.country_policy_version_id),
        base_content_hash=None if base is None else base.content_hash,
        proposed_by=PROPOSER,
        proposed_at=NOW - timedelta(hours=1),
        idempotency_key=IdempotencyKey(f"seed-{suffix}"),
    )
    store.versions[version.country_policy_version_id] = version
    return version


def _approval(
    version: CountryPolicyVersion,
    *,
    approval_id: str | None = None,
    approval_type: str = "country_policy_change",
    change_set_ref: str | None = None,
    decided_by: EmployeeId = APPROVER,
    decided_at: datetime = NOW,
) -> CountryPolicyApprovalFact:
    return CountryPolicyApprovalFact(
        approval_id=ApprovalId(approval_id or f"apr_{version.version_number}_exact"),
        approval_type=approval_type,
        change_set_ref=change_set_ref or version.change_set_ref,
        decided_by=decided_by,
        decided_at=decided_at,
    )


def _stored_activation(
    store: _Store,
    version: CountryPolicyVersion,
    *,
    sequence: int,
) -> CountryPolicyActivation:
    activation = CountryPolicyActivation(
        tenant_id=TENANT,
        activation_id=CountryPolicyActivationId(f"cpa_stored_{sequence}"),
        activation_sequence=sequence,
        country_key=version.country_key,
        country_policy_version_id=version.country_policy_version_id,
        content_hash=version.content_hash,
        approval_id=ApprovalId(f"apr_stored_{sequence}"),
        change_set_ref=version.change_set_ref,
        approved_by=APPROVER,
        approved_at=NOW,
        activated_by=SYSTEM_ID,
        activated_at=NOW,
    )
    store.activations.append(activation)
    return activation


@pytest.mark.asyncio
async def test_activation_requires_system_actor() -> None:
    store = _Store()
    candidate = _seed_version(store, "permission", version_number=1)
    approval = _approval(candidate)
    service = _service(store)

    with pytest.raises(PermissionDenied):
        await service.activate_country_policy(
            TENANT, candidate.country_policy_version_id, approval, actor=_boss()
        )
    assert store.version_reads == 0

    activated = await service.activate_country_policy(
        TENANT, candidate.country_policy_version_id, approval, actor=_system()
    )

    assert activated.country_policy_version_id == candidate.country_policy_version_id
    assert store.version_reads >= 1


@pytest.mark.parametrize(
    ("approval_type", "change_set_ref"),
    [
        ("quote_send", None),
        ("country_policy_change", "country_policy:cpp_wrong:" + "a" * 64),
        ("country_policy_change", None),
    ],
    ids=("type", "change-set-version", "hash"),
)
@pytest.mark.asyncio
async def test_activation_requires_exact_approval_type_change_set_and_hash(
    approval_type: str,
    change_set_ref: str | None,
) -> None:
    store = _Store()
    candidate = _seed_version(store, "approval-fact", version_number=1)
    if change_set_ref is None and approval_type == "country_policy_change":
        change_set_ref = (
            f"country_policy:{candidate.country_policy_version_id}:" + "f" * 64
        )

    with pytest.raises(CountryPolicyApprovalFactInvalidError):
        await _service(store).activate_country_policy(
            TENANT,
            candidate.country_policy_version_id,
            _approval(
                candidate,
                approval_type=approval_type,
                change_set_ref=change_set_ref,
            ),
            actor=_system(),
        )

    assert store.activations == []


@pytest.mark.parametrize("unsafe_time", ["naive", "future"])
@pytest.mark.asyncio
async def test_activation_rejects_future_or_naive_decided_time(
    unsafe_time: str,
) -> None:
    store = _Store()
    candidate = _seed_version(store, f"time-{unsafe_time}", version_number=1)
    if unsafe_time == "naive":
        approval = CountryPolicyApprovalFact.model_construct(
            approval_id=ApprovalId(f"apr_{unsafe_time}"),
            approval_type="country_policy_change",
            change_set_ref=candidate.change_set_ref,
            decided_by=APPROVER,
            decided_at=datetime(2026, 8, 24, 12, 30),  # noqa: DTZ001
        )
    else:
        approval = _approval(candidate, decided_at=NOW + timedelta(seconds=1))

    with pytest.raises(CountryPolicyApprovalFactInvalidError):
        await _service(store).activate_country_policy(
            TENANT,
            candidate.country_policy_version_id,
            approval,
            actor=_system(),
        )

    assert store.activations == []


@pytest.mark.asyncio
async def test_activation_rejects_candidate_proposer_as_decider() -> None:
    store = _Store()
    candidate = _seed_version(store, "self-approval", version_number=1)

    with pytest.raises(CountryPolicyApprovalFactInvalidError):
        await _service(store).activate_country_policy(
            TENANT,
            candidate.country_policy_version_id,
            _approval(candidate, decided_by=PROPOSER),
            actor=_system(),
        )

    assert store.activations == []


@pytest.mark.asyncio
async def test_first_activation_requires_country_to_still_have_no_current_version() -> (
    None
):
    store = _Store()
    candidate = _seed_version(store, "captured-empty-base", version_number=1)
    current = _seed_version(store, "became-current", version_number=2)
    _stored_activation(store, current, sequence=1)

    with pytest.raises(CountryPolicyBaseVersionConflictError):
        await _service(store).activate_country_policy(
            TENANT,
            candidate.country_policy_version_id,
            _approval(candidate),
            actor=_system(),
        )

    assert len(store.activations) == 1


@pytest.mark.asyncio
async def test_revision_rejects_stale_country_base() -> None:
    store = _Store()
    base = _seed_version(store, "stale-base", version_number=1)
    _stored_activation(store, base, sequence=1)
    sibling_a = _seed_version(store, "stale-a", version_number=2, base=base)
    sibling_b = _seed_version(store, "stale-b", version_number=3, base=base)
    service = _service(store)

    await service.activate_country_policy(
        TENANT,
        sibling_b.country_policy_version_id,
        _approval(sibling_b, approval_id="apr_stale_b"),
        actor=_system(),
    )

    with pytest.raises(CountryPolicyBaseVersionConflictError):
        await service.activate_country_policy(
            TENANT,
            sibling_a.country_policy_version_id,
            _approval(sibling_a, approval_id="apr_stale_a"),
            actor=_system(),
        )

    assert [item.country_policy_version_id for item in store.activations] == [
        base.country_policy_version_id,
        sibling_b.country_policy_version_id,
    ]


@pytest.mark.asyncio
async def test_exact_same_approval_and_version_replay_returns_existing_activation() -> (
    None
):
    store = _Store()
    candidate = _seed_version(store, "exact-replay", version_number=1)
    approval = _approval(candidate, approval_id="apr_exact_replay")
    service = _service(store)

    first = await service.activate_country_policy(
        TENANT, candidate.country_policy_version_id, approval, actor=_system()
    )
    replay = await service.activate_country_policy(
        TENANT, candidate.country_policy_version_id, approval, actor=_system()
    )

    assert replay.activation_id == first.activation_id
    assert replay.activation_sequence == first.activation_sequence == 1
    assert len(store.activations) == 1


@pytest.mark.parametrize(
    "mutation", ("decider", "decided-time", "approval-id", "version")
)
@pytest.mark.asyncio
async def test_same_approval_or_version_with_different_fact_conflicts(
    mutation: str,
) -> None:
    store = _Store()
    first = _seed_version(store, f"conflict-{mutation}-first", version_number=1)
    original = _approval(first, approval_id=f"apr_conflict_{mutation}")
    service = _service(store)
    await service.activate_country_policy(
        TENANT, first.country_policy_version_id, original, actor=_system()
    )

    requested_version = first
    requested_approval = original
    if mutation == "decider":
        requested_approval = _approval(
            first,
            approval_id=str(original.approval_id),
            decided_by=EmployeeId("emp_other_approver"),
        )
    elif mutation == "decided-time":
        requested_approval = _approval(
            first,
            approval_id=str(original.approval_id),
            decided_at=NOW - timedelta(seconds=1),
        )
    elif mutation == "approval-id":
        requested_approval = _approval(first, approval_id="apr_changed")
    else:
        requested_version = _seed_version(
            store, "conflict-other-version", version_number=2, base=first
        )
        requested_approval = _approval(
            requested_version, approval_id=str(original.approval_id)
        )

    with pytest.raises(CountryPolicyActivationConflictError):
        await service.activate_country_policy(
            TENANT,
            requested_version.country_policy_version_id,
            requested_approval,
            actor=_system(),
        )

    assert len(store.activations) == 1


@pytest.mark.asyncio
async def test_activation_sequence_is_monotonic_per_country() -> None:
    store = _Store()
    first = _seed_version(store, "sequence-first", version_number=1)
    service = _service(store)
    first_activation = await service.activate_country_policy(
        TENANT,
        first.country_policy_version_id,
        _approval(first, approval_id="apr_sequence_first"),
        actor=_system(),
    )
    revision = _seed_version(store, "sequence-second", version_number=2, base=first)

    second_activation = await service.activate_country_policy(
        TENANT,
        revision.country_policy_version_id,
        _approval(revision, approval_id="apr_sequence_second"),
        actor=_system(),
    )

    assert first_activation.activation_sequence == 1
    assert second_activation.activation_sequence == 2
