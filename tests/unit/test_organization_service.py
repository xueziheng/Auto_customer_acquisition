"""OrganizationService 的幂等提案、快照与精确激活门禁。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from domains.organization.errors import (
    PlaybookActivationConflictError,
    PlaybookApprovalFactInvalidError,
    PlaybookBaseVersionConflictError,
    PlaybookIdempotencyConflictError,
    PlaybookNotConfiguredError,
)
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
    Phase1OrganizationAuthorizer,
)
from domains.organization.schemas import PlaybookApprovalFact, PlaybookProposalCreate
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    PlaybookVersionId,
    TenantId,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)
TENANT = TenantId("tenant-service")
PROPOSER = EmployeeId("emp_01K00000000000000000000000")
APPROVER = EmployeeId("emp_01K00000000000000000000001")

_models = importlib.import_module("domains.organization.models")
CompanyPlaybookVersion = _models.CompanyPlaybookVersion


def _command(**overrides: object) -> PlaybookProposalCreate:
    values: dict[str, object] = {
        "company_type": "trading_company",
        "minimum_deal_amount": Decimal(10000),
        "minimum_deal_currency": "USD",
        "excluded_categories": ["adult"],
        "sourcing_regions": ["guangdong"],
        "excluded_countries": ["north korea"],
        "monthly_budget_credits": 500,
        "approval_requirements": ["catalog_reference_price"],
        "supply_capabilities_note": "Hardware sourcing.",
    }
    values.update(overrides)
    return PlaybookProposalCreate.model_validate(values)


def _boss(tenant: TenantId = TENANT) -> OrganizationActor:
    return OrganizationActor(
        str(PROPOSER),
        OrganizationScope(OrganizationScopeLevel.TENANT, tenant),
        "boss",
    )


def _system(tenant: TenantId = TENANT) -> OrganizationActor:
    return OrganizationActor(
        "system:playbook-workflow",
        OrganizationScope(OrganizationScopeLevel.SYSTEM, tenant),
        "system",
    )


def _approval(
    version: object,
    *,
    approval_id: str = "apr_01K00000000000000000000000",
    approval_type: str = "playbook_change",
    change_set_ref: str | None = None,
    decided_at: datetime = NOW,
) -> PlaybookApprovalFact:
    return PlaybookApprovalFact(
        approval_id=ApprovalId(approval_id),
        approval_type=approval_type,
        change_set_ref=change_set_ref or version.change_set_ref,
        decided_by=APPROVER,
        decided_at=decided_at,
    )


class _Store:
    def __init__(self) -> None:
        self.versions: dict[PlaybookVersionId, object] = {}
        self.activations: list[object] = []
        self.lock = asyncio.Lock()
        self.enter_count = 0


class _Versions:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def add(self, version: object) -> None:
        self._store.versions[version.playbook_version_id] = version

    async def get(
        self, tenant_id: TenantId, version_id: PlaybookVersionId
    ) -> object | None:
        version = self._store.versions.get(version_id)
        return version if version is not None and version.tenant_id == tenant_id else None

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> object | None:
        return next(
            (
                version
                for version in self._store.versions.values()
                if version.tenant_id == tenant_id
                and version.idempotency_key == idempotency_key
            ),
            None,
        )

    async def next_version_number(self, tenant_id: TenantId) -> int:
        return (
            max(
                (
                    version.version_number
                    for version in self._store.versions.values()
                    if version.tenant_id == tenant_id
                ),
                default=0,
            )
            + 1
        )

    async def list(self, tenant_id: TenantId, limit: int) -> list[object]:
        versions = sorted(
            (
                version
                for version in self._store.versions.values()
                if version.tenant_id == tenant_id
            ),
            key=lambda version: (
                version.version_number,
                str(version.playbook_version_id),
            ),
            reverse=True,
        )
        return versions[:limit]


class _Activations:
    def __init__(self, store: _Store, uow: _Uow) -> None:
        self._store = store
        self._uow = uow

    async def lock_tenant(self, tenant_id: TenantId) -> None:
        del tenant_id
        await self._store.lock.acquire()
        self._uow.locked = True

    async def get_current(self, tenant_id: TenantId) -> object | None:
        matches = [
            item for item in self._store.activations if item.tenant_id == tenant_id
        ]
        return matches[-1] if matches else None

    async def get_by_version(
        self, tenant_id: TenantId, version_id: PlaybookVersionId
    ) -> object | None:
        return next(
            (
                item
                for item in self._store.activations
                if item.tenant_id == tenant_id
                and item.playbook_version_id == version_id
            ),
            None,
        )

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> object | None:
        return next(
            (
                item
                for item in self._store.activations
                if item.tenant_id == tenant_id and item.approval_id == approval_id
            ),
            None,
        )

    async def add(self, activation: object) -> None:
        self._store.activations.append(activation)


class _Uow:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.versions = _Versions(store)
        self.activations = _Activations(store, self)
        self.locked = False

    async def __aenter__(self) -> Self:
        self._store.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.locked:
            self._store.lock.release()


class _Factory:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def __call__(self, tenant_id: TenantId) -> _Uow:
        del tenant_id
        return _Uow(self.store)


def _service(store: _Store | None = None):
    implementation = importlib.import_module(
        "domains.organization.service_impl"
    ).OrganizationServiceImpl
    selected = store or _Store()
    return (
        implementation(
            _Factory(selected),
            Phase1OrganizationAuthorizer(TENANT),
            now=lambda: NOW + timedelta(minutes=1),
        ),
        selected,
    )


async def _propose(
    service: object,
    key: str,
    **command_overrides: object,
):
    result = await service.propose_playbook(
        TENANT,
        _command(**command_overrides),
        actor=_boss(),
        idempotency_key=IdempotencyKey(key),
    )
    return result


def _stored(store: _Store, version_id: PlaybookVersionId) -> object:
    return store.versions[version_id]


@pytest.mark.asyncio
async def test_missing_activation_raises_instead_of_returning_default() -> None:
    service, _ = _service()

    with pytest.raises(PlaybookNotConfiguredError):
        await service.get_playbook(TENANT, actor=_boss())


@pytest.mark.asyncio
async def test_first_proposal_captures_no_base_and_only_system_can_read_snapshot() -> None:
    service, _ = _service()
    proposed = await _propose(service, "first-proposal")

    snapshot = await service.get_change_snapshot(
        TENANT, proposed.playbook_version_id, actor=_system()
    )

    assert snapshot.base is None
    assert snapshot.current is None
    assert snapshot.candidate.playbook_version_id == proposed.playbook_version_id
    assert snapshot.base_is_current is True
    with pytest.raises(PermissionDenied):
        await service.get_change_snapshot(
            TENANT, proposed.playbook_version_id, actor=_boss()
        )


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_original_base_and_different_content_conflicts() -> None:
    service, store = _service()
    first = await _propose(service, "same-key")
    replay = await _propose(service, "same-key")

    assert replay == first
    assert len(store.versions) == 1
    with pytest.raises(PlaybookIdempotencyConflictError):
        await _propose(service, "same-key", minimum_deal_amount=Decimal(20000))


@pytest.mark.asyncio
async def test_concurrent_identical_proposals_create_one_version() -> None:
    service, store = _service()

    first, second = await asyncio.gather(
        _propose(service, "concurrent-key"),
        _propose(service, "concurrent-key"),
    )

    assert first == second
    assert len(store.versions) == 1


@pytest.mark.asyncio
async def test_activation_replay_returns_original_fact_and_active_provenance() -> None:
    service, store = _service()
    proposed = await _propose(service, "activate")
    version = _stored(store, proposed.playbook_version_id)
    approval = _approval(version)

    first = await service.activate_playbook(
        TENANT, version.playbook_version_id, approval, actor=_system()
    )
    second = await service.activate_playbook(
        TENANT, version.playbook_version_id, approval, actor=_system()
    )
    active = await service.get_playbook(TENANT, actor=_boss())

    assert second == first
    assert len(store.activations) == 1
    assert active.content_provenance.confirmed_by == APPROVER
    assert active.approved_at == approval.decided_at
    assert active.activated_at == NOW + timedelta(minutes=1)


@pytest.mark.parametrize(
    ("approval_type", "change_set_ref"),
    [
        ("quote_send", None),
        ("playbook_change", "playbook:wrong:" + "a" * 64),
    ],
)
@pytest.mark.asyncio
async def test_activation_rejects_non_playbook_or_mismatched_approval_fact(
    approval_type: str,
    change_set_ref: str | None,
) -> None:
    service, store = _service()
    proposed = await _propose(service, "invalid-approval")
    version = _stored(store, proposed.playbook_version_id)

    with pytest.raises(PlaybookApprovalFactInvalidError):
        await service.activate_playbook(
            TENANT,
            version.playbook_version_id,
            _approval(
                version,
                approval_type=approval_type,
                change_set_ref=change_set_ref,
            ),
            actor=_system(),
        )
    assert store.activations == []


@pytest.mark.asyncio
async def test_activation_same_approval_for_different_version_is_conflict() -> None:
    service, store = _service()
    first_result = await _propose(service, "approval-conflict-one")
    first = _stored(store, first_result.playbook_version_id)
    approval = _approval(first)
    await service.activate_playbook(
        TENANT, first.playbook_version_id, approval, actor=_system()
    )
    second_result = await _propose(service, "approval-conflict-two")
    second = _stored(store, second_result.playbook_version_id)

    with pytest.raises(PlaybookActivationConflictError):
        await service.activate_playbook(
            TENANT,
            second.playbook_version_id,
            _approval(second, approval_id=str(approval.approval_id)),
            actor=_system(),
        )


@pytest.mark.asyncio
async def test_activation_rejects_stale_base_without_appending_fact() -> None:
    service, store = _service()
    first_result = await _propose(service, "base-one")
    first = _stored(store, first_result.playbook_version_id)
    await service.activate_playbook(
        TENANT, first.playbook_version_id, _approval(first), actor=_system()
    )
    stale_result = await _propose(service, "stale-candidate")
    stale = _stored(store, stale_result.playbook_version_id)
    winner_result = await _propose(
        service, "winner-candidate", minimum_deal_amount=Decimal(12000)
    )
    winner = _stored(store, winner_result.playbook_version_id)
    await service.activate_playbook(
        TENANT,
        winner.playbook_version_id,
        _approval(winner, approval_id="apr_01K00000000000000000000002"),
        actor=_system(),
    )

    with pytest.raises(PlaybookBaseVersionConflictError):
        await service.activate_playbook(
            TENANT,
            stale.playbook_version_id,
            _approval(stale, approval_id="apr_01K00000000000000000000003"),
            actor=_system(),
        )
    assert all(
        item.playbook_version_id != stale.playbook_version_id
        for item in store.activations
    )


@pytest.mark.asyncio
async def test_snapshot_preserves_captured_base_after_later_activation() -> None:
    service, store = _service()
    base_result = await _propose(service, "snapshot-base")
    base = _stored(store, base_result.playbook_version_id)
    await service.activate_playbook(
        TENANT, base.playbook_version_id, _approval(base), actor=_system()
    )
    stale_result = await _propose(service, "snapshot-stale")
    stale = _stored(store, stale_result.playbook_version_id)
    winner_result = await _propose(
        service, "snapshot-winner", minimum_deal_amount=Decimal(13000)
    )
    winner = _stored(store, winner_result.playbook_version_id)
    await service.activate_playbook(
        TENANT,
        winner.playbook_version_id,
        _approval(winner, approval_id="apr_01K00000000000000000000004"),
        actor=_system(),
    )

    snapshot = await service.get_change_snapshot(
        TENANT, stale.playbook_version_id, actor=_system()
    )

    assert snapshot.base is not None
    assert snapshot.base.playbook_version_id == base.playbook_version_id
    assert snapshot.current is not None
    assert snapshot.current.playbook_version_id == winner.playbook_version_id
    assert snapshot.base_is_current is False


@pytest.mark.asyncio
async def test_same_content_new_key_after_base_change_creates_distinct_candidate() -> None:
    service, store = _service()
    first_result = await _propose(service, "content-first")
    first = _stored(store, first_result.playbook_version_id)
    await service.activate_playbook(
        TENANT, first.playbook_version_id, _approval(first), actor=_system()
    )

    second_result = await _propose(service, "content-second")

    assert second_result.playbook_version_id != first_result.playbook_version_id
    assert second_result.content_hash == first_result.content_hash
    second = _stored(store, second_result.playbook_version_id)
    assert second.base_version_id == first.playbook_version_id


@pytest.mark.parametrize("limit", [0, 201, True])
@pytest.mark.asyncio
async def test_list_versions_rejects_out_of_range_or_boolean_limit(limit: object) -> None:
    service, _ = _service()

    with pytest.raises(ValidationError):
        await service.list_versions(TENANT, actor=_boss(), limit=limit)


@pytest.mark.asyncio
async def test_malformed_idempotency_key_is_rejected_before_uow() -> None:
    service, store = _service()

    with pytest.raises(ValidationError, match="Playbook 幂等键格式无效"):
        await service.propose_playbook(
            TENANT,
            _command(),
            actor=_boss(),
            idempotency_key=IdempotencyKey("unicode-幂等键"),
        )
    assert store.enter_count == 0


@pytest.mark.asyncio
async def test_naive_approval_time_is_rejected_before_uow() -> None:
    service, store = _service()
    candidate = CompanyPlaybookVersion.from_command(
        tenant_id=TENANT,
        version_id=PlaybookVersionId("pbv_01K00000000000000000000009"),
        version_number=1,
        command=_command(),
        base_version_id=None,
        base_content_hash=None,
        proposed_by=PROPOSER,
        proposed_at=NOW,
        idempotency_key=IdempotencyKey("unsafe-approval-candidate"),
    )
    unsafe = PlaybookApprovalFact.model_construct(
        approval_id=ApprovalId("apr_01K00000000000000000000009"),
        approval_type="playbook_change",
        change_set_ref=candidate.change_set_ref,
        decided_by=APPROVER,
        decided_at=datetime(2026, 8, 24, 12),  # noqa: DTZ001 - 拒绝样本
    )

    with pytest.raises(PlaybookApprovalFactInvalidError):
        await service.activate_playbook(
            TENANT, candidate.playbook_version_id, unsafe, actor=_system()
        )
    assert store.enter_count == 0


@pytest.mark.asyncio
async def test_missing_version_referenced_by_activation_is_integrity_failure() -> None:
    service, store = _service()
    result = await _propose(service, "corrupt-current")
    version = _stored(store, result.playbook_version_id)
    await service.activate_playbook(
        TENANT, version.playbook_version_id, _approval(version), actor=_system()
    )
    del store.versions[version.playbook_version_id]

    with pytest.raises(TransientError):
        await service.get_playbook(TENANT, actor=_boss())
