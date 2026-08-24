"""国家政策服务的授权、fail-closed 判断与原子提案分支。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.compliance.errors import CountryPolicyIdempotencyConflictError
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyAction,
    CountryPolicyApprovalFact,
    CountryPolicyField,
    CountryPolicyProposalCreate,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.events.catalog import CountryPolicyVersionProposed
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
TENANT = TenantId("tenant-country-policy-service")
PROPOSER = EmployeeId("emp_proposer")
SYSTEM_ID = "system:country-policy-reader"


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
        "requirements": ["retain.assessment_ref", "honor_opt_out"],
        "notes": "Synthetic policy fixture reviewed by an employee.",
        "field_sources": {
            field: {
                "source_type": SourceType.EMPLOYEE_INPUT,
                "source_id": f"assessment:synthetic:{index}",
            }
            for index, field in enumerate(
                sorted(DECISION_FIELDS, key=lambda item: item.value), 1
            )
        },
    }
    values.update(overrides)
    return CountryPolicyProposalCreate.model_validate(values)


def _boss(tenant_id: TenantId = TENANT) -> ComplianceActor:
    return ComplianceActor(
        actor_id=str(PROPOSER),
        tenant_id=tenant_id,
        scope=ComplianceScope.TENANT,
        role="boss",
    )


def _system(tenant_id: TenantId = TENANT) -> ComplianceActor:
    return ComplianceActor(
        actor_id=SYSTEM_ID,
        tenant_id=tenant_id,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )


class _Store:
    def __init__(self) -> None:
        self.versions: dict[CountryPolicyVersionId, CountryPolicyVersion] = {}
        self.provenance: dict[
            CountryPolicyVersionId, dict[CountryPolicyField, object]
        ] = {}
        self.activations: list[CountryPolicyActivation] = []
        self.events: list[CountryPolicyVersionProposed] = []
        self.country_lock = asyncio.Lock()
        self.version_add_count = 0
        self.provenance_add_count = 0
        self.publish_count = 0
        self.activation_read_error: Exception | None = None


class _Versions:
    def __init__(self, store: _Store, uow: _Uow) -> None:
        self._store = store
        self._uow = uow

    async def lock_country(self, tenant_id: TenantId, country_key: str) -> None:
        del tenant_id, country_key
        await self._store.country_lock.acquire()
        self._uow.locked = True

    async def add(self, tenant_id: TenantId, version: CountryPolicyVersion) -> None:
        assert version.tenant_id == tenant_id
        self._store.versions[version.country_policy_version_id] = version
        self._store.version_add_count += 1

    async def get(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyVersion | None:
        version = self._store.versions.get(version_id)
        if version is None or version.tenant_id != tenant_id:
            return None
        return version

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> CountryPolicyVersion | None:
        return next(
            (
                version
                for version in self._store.versions.values()
                if version.tenant_id == tenant_id
                and version.idempotency_key == idempotency_key
            ),
            None,
        )

    async def next_version_number(self, tenant_id: TenantId, country_key: str) -> int:
        return (
            max(
                (
                    version.version_number
                    for version in self._store.versions.values()
                    if version.tenant_id == tenant_id
                    and version.country_key == country_key
                ),
                default=0,
            )
            + 1
        )

    async def list(
        self, tenant_id: TenantId, country_key: str, limit: int
    ) -> list[CountryPolicyVersion]:
        versions = sorted(
            (
                version
                for version in self._store.versions.values()
                if version.tenant_id == tenant_id and version.country_key == country_key
            ),
            key=lambda version: (
                version.version_number,
                str(version.country_policy_version_id),
            ),
            reverse=True,
        )
        return versions[:limit]


class _Provenance:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def add_for_version(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        field_provenance: dict[CountryPolicyField, object],
    ) -> None:
        assert self._store.versions[version_id].tenant_id == tenant_id
        self._store.provenance[version_id] = dict(field_provenance)
        self._store.provenance_add_count += 1

    async def list_for_version(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> dict[CountryPolicyField, object]:
        assert self._store.versions[version_id].tenant_id == tenant_id
        return dict(self._store.provenance.get(version_id, {}))


class _Activations:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def get_current(
        self, tenant_id: TenantId, country_key: str
    ) -> CountryPolicyActivation | None:
        if self._store.activation_read_error is not None:
            raise self._store.activation_read_error
        matches = [
            activation
            for activation in self._store.activations
            if activation.tenant_id == tenant_id
            and activation.country_key == country_key
        ]
        return max(matches, key=lambda item: item.activation_sequence, default=None)

    async def list_current(
        self, tenant_id: TenantId, limit: int
    ) -> list[CountryPolicyActivation]:
        current: dict[str, CountryPolicyActivation] = {}
        for activation in self._store.activations:
            if activation.tenant_id != tenant_id:
                continue
            previous = current.get(activation.country_key)
            if (
                previous is None
                or activation.activation_sequence > previous.activation_sequence
            ):
                current[activation.country_key] = activation
        return [current[key] for key in sorted(current)][:limit]

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
        matches = [
            activation.activation_sequence
            for activation in self._store.activations
            if activation.tenant_id == tenant_id
            and activation.country_key == country_key
        ]
        return max(matches, default=0) + 1

    async def add(
        self, tenant_id: TenantId, activation: CountryPolicyActivation
    ) -> None:
        assert activation.tenant_id == tenant_id
        self._store.activations.append(activation)


class _Bus:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def publish(self, event: CountryPolicyVersionProposed) -> None:
        self._store.events.append(event)
        self._store.publish_count += 1

    async def publish_many(self, events: list[CountryPolicyVersionProposed]) -> None:
        for event in events:
            await self.publish(event)

    def subscribe(self, event_type: type[object], handler: object) -> None:
        del event_type, handler


class _Uow:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.locked = False
        self.versions = _Versions(store, self)
        self.provenance = _Provenance(store)
        self.activations = _Activations(store)
        self.bus = _Bus(store)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        if self.locked:
            self._store.country_lock.release()


class _UowFactory:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def __call__(self, tenant_id: TenantId) -> _Uow:
        del tenant_id
        return _Uow(self._store)


def _service(
    store: _Store, *, now: Callable[[], datetime] = lambda: NOW
) -> ComplianceServiceImpl:
    return ComplianceServiceImpl(
        _UowFactory(store),
        Phase1ComplianceAuthorizer(TENANT),
        now=now,
    )


def _seed_version(
    store: _Store,
    suffix: str,
    command: CountryPolicyProposalCreate,
    *,
    version_number: int = 1,
    base_version_id: CountryPolicyVersionId | None = None,
    base_content_hash: str | None = None,
) -> CountryPolicyVersion:
    version = CountryPolicyVersion.from_command(
        tenant_id=TENANT,
        version_id=CountryPolicyVersionId(f"cpp_{suffix}"),
        version_number=version_number,
        command=command,
        base_version_id=base_version_id,
        base_content_hash=base_content_hash,
        proposed_by=PROPOSER,
        proposed_at=NOW,
        idempotency_key=IdempotencyKey(f"seed-{suffix}"),
    )
    store.versions[version.country_policy_version_id] = version
    store.provenance[version.country_policy_version_id] = dict(version.field_provenance)
    return version


def _activate(
    store: _Store, version: CountryPolicyVersion, *, sequence: int = 1
) -> CountryPolicyActivation:
    activation = CountryPolicyActivation(
        tenant_id=TENANT,
        activation_id=CountryPolicyActivationId(
            f"cpa_{version.country_policy_version_id}_{sequence}"
        ),
        activation_sequence=sequence,
        country_key=version.country_key,
        country_policy_version_id=version.country_policy_version_id,
        content_hash=version.content_hash,
        approval_id=ApprovalId(f"apr_{version.country_policy_version_id}_{sequence}"),
        change_set_ref=version.change_set_ref,
        approved_by=EmployeeId("emp_independent_approver"),
        approved_at=NOW,
        activated_by=SYSTEM_ID,
        activated_at=NOW,
    )
    store.activations.append(activation)
    return activation


@pytest.mark.asyncio
async def test_unknown_country_returns_configured_false_allowed_false() -> None:
    decision = await _service(_Store()).get_country_policy_decision(
        TENANT,
        "  SYNTHETIC   UNKNOWN  ",
        CountryPolicyAction.CONTACT_ENRICHMENT,
        actor=_system(),
    )

    assert decision.country_key == "synthetic unknown"
    assert decision.action is CountryPolicyAction.CONTACT_ENRICHMENT
    assert decision.configured is False
    assert decision.allowed is False
    assert decision.active_version_id is None
    assert decision.content_hash is None
    assert decision.requirements == ()


@pytest.mark.asyncio
async def test_explicit_denial_is_distinct_from_unknown_country() -> None:
    store = _Store()
    version = _seed_version(
        store, "explicit-denial", _command(contact_enrichment_allowed=False)
    )
    _activate(store, version)

    decision = await _service(store).get_country_policy_decision(
        TENANT,
        "Synthetic Market",
        CountryPolicyAction.CONTACT_ENRICHMENT,
        actor=_system(),
    )

    assert decision.configured is True
    assert decision.allowed is False
    assert decision.active_version_id == version.country_policy_version_id
    assert decision.content_hash == version.content_hash


@pytest.mark.asyncio
async def test_allowed_action_returns_exact_active_version_and_requirements() -> None:
    store = _Store()
    version = _seed_version(
        store,
        "allowed-action",
        _command(
            contact_enrichment_allowed=True,
            requirements=["retain.assessment_ref", "honor_opt_out"],
        ),
    )
    _activate(store, version)

    decision = await _service(store).get_country_policy_decision(
        TENANT,
        "Synthetic Market",
        CountryPolicyAction.CONTACT_ENRICHMENT,
        actor=_system(),
    )

    assert decision.allowed is True
    assert decision.active_version_id == version.country_policy_version_id
    assert decision.content_hash == version.content_hash
    assert decision.requirements == ("honor_opt_out", "retain.assessment_ref")


@pytest.mark.parametrize(
    ("changed_field", "literal_allowed"),
    [
        ("public_research_allowed", (True, False, False)),
        ("contact_enrichment_allowed", (False, True, False)),
        ("cold_b2b_email_allowed", (False, False, True)),
    ],
)
@pytest.mark.asyncio
async def test_action_maps_only_to_its_explicit_boolean(
    changed_field: str, literal_allowed: tuple[bool, bool, bool]
) -> None:
    store = _Store()
    action_values = {
        "public_research_allowed": False,
        "contact_enrichment_allowed": False,
        "cold_b2b_email_allowed": False,
    }
    action_values[changed_field] = True
    command = _command(**action_values)
    version = _seed_version(store, changed_field, command)
    _activate(store, version)

    actual: list[bool] = []
    for action in (
        CountryPolicyAction.PUBLIC_RESEARCH,
        CountryPolicyAction.CONTACT_ENRICHMENT,
        CountryPolicyAction.COLD_B2B_EMAIL,
    ):
        actual.append(
            (
                await _service(store).get_country_policy_decision(
                    TENANT,
                    "Synthetic Market",
                    action,
                    actor=_system(),
                )
            ).allowed
        )

    assert tuple(actual) == literal_allowed


@pytest.mark.asyncio
async def test_storage_failure_propagates_instead_of_returning_denial() -> None:
    store = _Store()
    failure = TransientError("safe injected storage failure")
    store.activation_read_error = failure

    with pytest.raises(TransientError) as captured:
        await _service(store).get_country_policy_decision(
            TENANT,
            "Synthetic Market",
            CountryPolicyAction.CONTACT_ENRICHMENT,
            actor=_system(),
        )

    assert captured.value is failure


@pytest.mark.asyncio
async def test_boss_can_read_and_propose_but_not_decide_or_activate() -> None:
    store = _Store()
    service = _service(store)
    proposal = await service.propose_country_policy(
        TENANT,
        _command(),
        actor=_boss(),
        idempotency_key=IdempotencyKey("boss-proposal"),
    )
    version = store.versions[proposal.country_policy_version_id]
    _activate(store, version)

    assert (
        await service.get_active_policy(TENANT, "Synthetic Market", actor=_boss())
    ).country_policy_version_id == proposal.country_policy_version_id
    assert (
        await service.get_version(
            TENANT, proposal.country_policy_version_id, actor=_boss()
        )
    ).content_hash == proposal.content_hash
    assert len(await service.list_active_policies(TENANT, actor=_boss())) == 1
    assert (
        len(await service.list_versions(TENANT, "Synthetic Market", actor=_boss())) == 1
    )
    assert (await service.get_coverage(TENANT, actor=_boss())).active_policy_count == 1

    with pytest.raises(PermissionDenied):
        await service.get_country_policy_decision(
            TENANT,
            "Synthetic Market",
            CountryPolicyAction.PUBLIC_RESEARCH,
            actor=_boss(),
        )
    with pytest.raises(PermissionDenied):
        await service.get_change_snapshot(
            TENANT, proposal.country_policy_version_id, actor=_boss()
        )
    approval = CountryPolicyApprovalFact(
        approval_id=ApprovalId("apr_boss-denied"),
        approval_type="country_policy_change",
        change_set_ref=proposal.change_set_ref,
        decided_by=EmployeeId("emp_other"),
        decided_at=NOW,
    )
    with pytest.raises(PermissionDenied):
        await service.activate_country_policy(
            TENANT,
            proposal.country_policy_version_id,
            approval,
            actor=_boss(),
        )


@pytest.mark.asyncio
async def test_system_can_decide_but_cannot_propose() -> None:
    service = _service(_Store())

    decision = await service.get_country_policy_decision(
        TENANT,
        "Synthetic Market",
        CountryPolicyAction.PUBLIC_RESEARCH,
        actor=_system(),
    )
    assert decision.configured is False

    with pytest.raises(PermissionDenied):
        await service.propose_country_policy(
            TENANT,
            _command(),
            actor=_system(),
            idempotency_key=IdempotencyKey("system-proposal-denied"),
        )


@pytest.mark.asyncio
async def test_proposal_is_idempotent_for_same_normalized_content() -> None:
    store = _Store()
    service = _service(store)
    first = await service.propose_country_policy(
        TENANT,
        _command(),
        actor=_boss(),
        idempotency_key=IdempotencyKey("same-normalized-content"),
    )
    second = await service.propose_country_policy(
        TENANT,
        _command(
            country="  Synthetic   Market ",
            requirements=["honor_opt_out", "retain.assessment_ref"],
        ),
        actor=_boss(),
        idempotency_key=IdempotencyKey("same-normalized-content"),
    )

    assert second == first
    assert second.country_policy_version_id == first.country_policy_version_id
    assert second.version_number == 1
    assert second.content_hash == first.content_hash
    assert store.version_add_count == 1
    assert store.provenance_add_count == 1
    assert store.publish_count == 1


@pytest.mark.asyncio
async def test_idempotency_key_with_different_content_conflicts() -> None:
    store = _Store()
    service = _service(store)
    await service.propose_country_policy(
        TENANT,
        _command(contact_enrichment_allowed=False),
        actor=_boss(),
        idempotency_key=IdempotencyKey("content-conflict"),
    )

    with pytest.raises(CountryPolicyIdempotencyConflictError):
        await service.propose_country_policy(
            TENANT,
            _command(contact_enrichment_allowed=True),
            actor=_boss(),
            idempotency_key=IdempotencyKey("content-conflict"),
        )

    assert store.version_add_count == 1
    assert store.publish_count == 1


@pytest.mark.asyncio
async def test_first_proposal_has_no_base_and_revision_captures_current_base() -> None:
    store = _Store()
    service = _service(store)
    first = await service.propose_country_policy(
        TENANT,
        _command(),
        actor=_boss(),
        idempotency_key=IdempotencyKey("first-policy"),
    )
    first_version = store.versions[first.country_policy_version_id]
    assert first_version.base_version_id is None
    assert first_version.base_content_hash is None

    _activate(store, first_version)
    revision = await service.propose_country_policy(
        TENANT,
        _command(contact_enrichment_allowed=True),
        actor=_boss(),
        idempotency_key=IdempotencyKey("policy-revision"),
    )
    revision_version = store.versions[revision.country_policy_version_id]

    assert revision_version.base_version_id == first.country_policy_version_id
    assert revision_version.base_content_hash == first.content_hash
    snapshot = await service.get_change_snapshot(
        TENANT, revision.country_policy_version_id, actor=_system()
    )
    assert snapshot.base is not None
    assert snapshot.current is not None
    assert snapshot.base.country_policy_version_id == first.country_policy_version_id
    assert snapshot.current.country_policy_version_id == first.country_policy_version_id
    assert snapshot.base_is_current is True


@pytest.mark.asyncio
async def test_coverage_counts_active_and_enrichment_allowed_countries() -> None:
    store = _Store()
    first = _seed_version(
        store,
        "coverage-first",
        _command(country="Synthetic Market", contact_enrichment_allowed=True),
    )
    second = _seed_version(
        store,
        "coverage-second",
        _command(country="Synthetic Republic", contact_enrichment_allowed=False),
    )
    _activate(store, first)
    _activate(store, second)

    coverage = await _service(store).get_coverage(TENANT, actor=_boss())

    assert coverage.active_policy_count == 2
    assert coverage.contact_enrichment_allowed_count == 1


@pytest.mark.asyncio
async def test_service_binds_provenance_to_authenticated_actor_and_server_time() -> (
    None
):
    unsafe_sources = {
        field: {
            "source_type": SourceType.EMPLOYEE_INPUT,
            "source_id": f"assessment:unsafe:{index}",
            "extracted_by": "client:forged",
            "confirmed_by": "client:forged",
            "extracted_at": "2000-01-01T00:00:00Z",
            "confirmed_at": "2000-01-01T00:00:00Z",
        }
        for index, field in enumerate(
            sorted(DECISION_FIELDS, key=lambda item: item.value), 1
        )
    }
    with pytest.raises(PydanticValidationError):
        _command(field_sources=unsafe_sources)

    store = _Store()
    service = _service(store)
    result = await service.propose_country_policy(
        TENANT,
        _command(),
        actor=_boss(),
        idempotency_key=IdempotencyKey("server-bound-provenance"),
    )
    stored = store.versions[result.country_policy_version_id]

    assert len(stored.field_provenance) == 9
    for provenance in stored.field_provenance.values():
        assert provenance.extracted_by == "human:emp_proposer"
        assert provenance.confirmed_by == PROPOSER
        assert provenance.extracted_at == NOW
        assert provenance.confirmed_at == NOW


@pytest.mark.parametrize("bad_key", ["", " bad", "bad key", "x" * 201])
@pytest.mark.asyncio
async def test_invalid_idempotency_key_fails_before_uow(bad_key: str) -> None:
    store = _Store()
    with pytest.raises(ValidationError):
        await _service(store).propose_country_policy(
            TENANT,
            _command(),
            actor=_boss(),
            idempotency_key=IdempotencyKey(bad_key),
        )

    assert store.version_add_count == 0
    assert store.publish_count == 0


@pytest.mark.parametrize("limit", [True, 0, 201, "10"])
@pytest.mark.asyncio
async def test_invalid_read_limit_fails_explicitly(limit: object) -> None:
    with pytest.raises(ValidationError):
        await _service(_Store()).list_active_policies(
            TENANT,
            actor=_boss(),
            limit=limit,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_mismatched_method_tenant_is_denied_before_storage() -> None:
    other = TenantId("tenant-country-policy-other")
    with pytest.raises(PermissionDenied):
        await _service(_Store()).get_country_policy_decision(
            other,
            "Synthetic Market",
            CountryPolicyAction.PUBLIC_RESEARCH,
            actor=_system(),
        )
