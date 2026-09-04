"""Catalog Proposal Policy 生命周期的纯领域服务测试。"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import pytest

from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.errors import (
    CatalogPolicyApprovalConflictError,
    CatalogPolicyDecisionInvalidError,
    CatalogPolicyIdempotencyConflictError,
    CatalogPolicyNotFoundError,
    CatalogPolicyStateTransitionError,
)
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.schemas import (
    CatalogApprovalDecisionInput,
    CatalogProposalPolicyContent,
)
from domains.products.service import catalog_policy_creation_request_hash
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    TenantId,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TENANT = TenantId("tn_catalog_policy_unit")
OTHER_TENANT = TenantId("tn_catalog_policy_other")
PROPOSER = EmployeeId("emp_catalog_product")
BOSS = EmployeeId("emp_catalog_boss")


def _symbol(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


CatalogProposalPolicyState = _symbol(
    "domains.products.models", "CatalogProposalPolicyState"
)
CatalogProposalPolicyVersion = _symbol(
    "domains.products.models", "CatalogProposalPolicyVersion"
)
CatalogPage = _symbol("domains.products.repository", "CatalogPage")


def _content(accounts: int = 3) -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=accounts,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


def _actor(
    role: ProductRole = ProductRole.PRODUCT,
    *,
    actor_id: str = str(PROPOSER),
    tenant_id: TenantId = TENANT,
) -> ProductActor:
    return ProductActor(actor_id=actor_id, role=role, tenant_id=tenant_id)


def _system(tenant_id: TenantId = TENANT) -> ProductActor:
    return _actor(
        ProductRole.SYSTEM,
        actor_id="system:catalog-policy-workflow",
        tenant_id=tenant_id,
    )


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _Store:
    def __init__(self) -> None:
        self.policies: dict[
            CatalogProposalPolicyVersionId, CatalogProposalPolicyVersion
        ] = {}
        self.events: list[object] = []
        self.enter_count = 0
        self.active_lock_reads = 0
        self.policy_locks: dict[CatalogProposalPolicyVersionId, asyncio.Lock] = {}
        self.active_lock = asyncio.Lock()
        self.read_error: Exception | None = None


class _Policies:
    def __init__(self, store: _Store, uow: _Uow) -> None:
        self._store = store
        self._uow = uow

    async def add(
        self, tenant_id: TenantId, policy: CatalogProposalPolicyVersion
    ) -> CatalogProposalPolicyVersion:
        assert tenant_id == policy.tenant_id
        existing = await self.get_by_creation_key(
            tenant_id, policy.proposed_by, policy.creation_key
        )
        if existing is not None:
            return existing
        self._store.policies[policy.policy_version_id] = policy
        return policy

    async def get(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
    ) -> CatalogProposalPolicyVersion | None:
        if self._store.read_error is not None:
            raise self._store.read_error
        policy = self._store.policies.get(policy_version_id)
        return policy if policy is not None and policy.tenant_id == tenant_id else None

    async def get_for_update(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
    ) -> CatalogProposalPolicyVersion | None:
        lock = self._store.policy_locks.setdefault(policy_version_id, asyncio.Lock())
        await lock.acquire()
        self._uow.locks.append(lock)
        return await self.get(tenant_id, policy_version_id)

    async def get_active(
        self, tenant_id: TenantId, *, for_update: bool = False
    ) -> CatalogProposalPolicyVersion | None:
        if self._store.read_error is not None:
            raise self._store.read_error
        if for_update:
            self._store.active_lock_reads += 1
            await self._store.active_lock.acquire()
            self._uow.locks.append(self._store.active_lock)
        return next(
            (
                item
                for item in self._store.policies.values()
                if item.tenant_id == tenant_id
                and item.state is CatalogProposalPolicyState.ACTIVE
            ),
            None,
        )

    async def get_by_creation_key(
        self, tenant_id: TenantId, proposed_by: EmployeeId, creation_key: str
    ) -> CatalogProposalPolicyVersion | None:
        return next(
            (
                item
                for item in self._store.policies.values()
                if item.tenant_id == tenant_id
                and item.proposed_by == proposed_by
                and item.creation_key == creation_key
            ),
            None,
        )

    async def update(
        self, tenant_id: TenantId, policy: CatalogProposalPolicyVersion
    ) -> CatalogProposalPolicyVersion:
        assert tenant_id == policy.tenant_id
        self._store.policies[policy.policy_version_id] = policy
        return policy

    async def list_versions(
        self, tenant_id: TenantId, *, limit: int, cursor: object = None
    ) -> CatalogPage[CatalogProposalPolicyVersion]:
        del cursor
        values = sorted(
            (
                item
                for item in self._store.policies.values()
                if item.tenant_id == tenant_id
            ),
            key=lambda item: (item.created_at, str(item.policy_version_id)),
            reverse=True,
        )[:limit]
        return CatalogPage(tuple(values), None)


class _Bus:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def publish(self, event: object) -> None:
        self._store.events.append(event)


class _Unused:
    pass


class _Uow:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.locks: list[asyncio.Lock] = []
        self.policies = _Policies(store, self)
        self.evaluations = _Unused()
        self.proposals = _Unused()
        self.cultivation_cases = _Unused()
        self.bus = _Bus(store)

    async def __aenter__(self) -> Self:
        self._store.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        for lock in reversed(self.locks):
            lock.release()


def _service(store: _Store, clock: _Clock | None = None) -> CatalogProposalServiceImpl:
    return CatalogProposalServiceImpl(
        lambda _tenant: _Uow(store),
        Phase2ProductAuthorizer(TENANT),
        now=clock or _Clock(),
    )


def _pending(
    marker: str,
    *,
    base: CatalogProposalPolicyVersionId | None = None,
    content: CatalogProposalPolicyContent | None = None,
    created_at: datetime = NOW,
) -> CatalogProposalPolicyVersion:
    value = content or _content()
    return CatalogProposalPolicyVersion(
        tenant_id=TENANT,
        policy_version_id=CatalogProposalPolicyVersionId(f"cpv_{marker}"),
        content=value,
        content_hash=catalog_policy_content_hash(value),
        base_active_version_id=base,
        proposed_by=PROPOSER,
        creation_key=f"key-{marker}",
        creation_request_hash=catalog_policy_creation_request_hash(
            value, PROPOSER, base
        ),
        approval_id=None,
        state=CatalogProposalPolicyState.PENDING_APPROVAL,
        created_at=created_at,
    )


def _decision(
    policy: CatalogProposalPolicyVersion,
    state: str = "approved",
    *,
    approval_id: str | None = None,
    request_hash: str | None = None,
    change_set_ref: str | None = None,
    proposed_by_employee: str | None = None,
    owner_employee: str | None = None,
    approval_type: str = "catalog_proposal_policy_change",
    contract_namespace: str = "catalog-policy-v1",
) -> CatalogApprovalDecisionInput:
    decided = state in {"approved", "rejected"}
    return CatalogApprovalDecisionInput.model_validate(
        {
            "approval_id": approval_id or str(policy.approval_id or "apr_policy"),
            "approval_type": approval_type,
            "contract_namespace": contract_namespace,
            "change_set_ref": change_set_ref
            or f"catalog-policy:{policy.policy_version_id}:{policy.content_hash}",
            "request_hash": request_hash or policy.creation_request_hash,
            "state": state,
            "proposed_by_run": None,
            "proposed_by_employee": proposed_by_employee or str(policy.proposed_by),
            "owner_employee": owner_employee or str(policy.proposed_by),
            "decided_by_employee": str(BOSS) if decided else None,
            "decided_at": NOW if decided else None,
            "expires_at": NOW if state == "expired" else NOW + timedelta(days=7),
        }
    )


async def _bind(
    service: CatalogProposalServiceImpl,
    policy: CatalogProposalPolicyVersion,
    approval_id: str,
) -> CatalogProposalPolicyVersion:
    await service.bind_policy_approval(
        TENANT,
        policy.policy_version_id,
        ApprovalId(approval_id),
        policy.creation_request_hash,
        actor=_system(),
    )
    return replace(policy, approval_id=ApprovalId(approval_id))


@pytest.mark.asyncio
async def test_no_active_policy_is_none_and_never_constructs_a_default() -> None:
    store = _Store()
    assert await _service(store).get_active_policy(TENANT, actor=_actor()) is None
    assert store.policies == {}


@pytest.mark.asyncio
async def test_permissions_and_tenant_are_checked_before_storage() -> None:
    store = _Store()
    service = _service(store)

    with pytest.raises(PermissionDenied):
        await service.create_policy_candidate(
            TENANT,
            _content(),
            idempotency_key="customer-denied",
            actor=_actor(ProductRole.CUSTOMER),
        )
    with pytest.raises(PermissionDenied):
        await service.create_policy_candidate(
            OTHER_TENANT,
            _content(),
            idempotency_key="tenant-denied",
            actor=_actor(ProductRole.PRODUCT),
        )
    with pytest.raises(PermissionDenied):
        await service.apply_policy_decision(
            TENANT,
            CatalogProposalPolicyVersionId("cpv_absent"),
            _decision(_pending("permission")),
            actor=_actor(ProductRole.BOSS, actor_id=str(BOSS)),
        )

    assert store.enter_count == 0


@pytest.mark.parametrize("bad_key", ["", " ", " leading", "trailing ", "x" * 201])
@pytest.mark.asyncio
async def test_invalid_opaque_creation_key_fails_before_storage(bad_key: str) -> None:
    store = _Store()
    with pytest.raises(ValidationError):
        await _service(store).create_policy_candidate(
            TENANT, _content(), idempotency_key=bad_key, actor=_actor()
        )
    assert store.enter_count == 0


@pytest.mark.asyncio
async def test_create_binds_canonical_content_proposer_and_locked_base() -> None:
    store = _Store()
    old = replace(
        _pending("base"),
        approval_id=ApprovalId("apr_base"),
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW,
    )
    store.policies[old.policy_version_id] = old
    clock = _Clock(NOW + timedelta(minutes=1))
    service = _service(store, clock)

    candidate_id = await service.create_policy_candidate(
        TENANT,
        _content(4),
        idempotency_key="opaque/key?with=symbols",
        actor=_actor(ProductRole.SOURCING),
    )
    candidate = store.policies[candidate_id]

    assert candidate.base_active_version_id == old.policy_version_id
    assert candidate.proposed_by == PROPOSER
    assert candidate.content_hash == catalog_policy_content_hash(_content(4))
    assert candidate.creation_request_hash == catalog_policy_creation_request_hash(
        _content(4), PROPOSER, old.policy_version_id
    )
    assert candidate.creation_key == "opaque/key?with=symbols"
    assert store.active_lock_reads == 1


@pytest.mark.asyncio
async def test_creation_replay_returns_original_after_current_base_changes() -> None:
    store = _Store()
    service = _service(store)
    candidate_id = await service.create_policy_candidate(
        TENANT, _content(), idempotency_key="lost-response", actor=_actor()
    )
    competitor = replace(
        _pending("competitor", content=_content(4)),
        approval_id=ApprovalId("apr_competitor"),
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW,
    )
    store.policies[competitor.policy_version_id] = competitor

    replayed_id = await service.create_policy_candidate(
        TENANT, _content(), idempotency_key="lost-response", actor=_actor()
    )

    assert replayed_id == candidate_id
    assert len(store.policies) == 2


@pytest.mark.asyncio
async def test_same_creation_key_with_changed_content_conflicts() -> None:
    store = _Store()
    service = _service(store)
    await service.create_policy_candidate(
        TENANT, _content(), idempotency_key="same-key", actor=_actor()
    )

    with pytest.raises(CatalogPolicyIdempotencyConflictError):
        await service.create_policy_candidate(
            TENANT, _content(4), idempotency_key="same-key", actor=_actor()
        )


@pytest.mark.asyncio
async def test_binding_is_idempotent_only_for_same_approval_and_hash() -> None:
    store = _Store()
    candidate = _pending("binding")
    store.policies[candidate.policy_version_id] = candidate
    service = _service(store)

    first = await service.bind_policy_approval(
        TENANT,
        candidate.policy_version_id,
        ApprovalId("apr_binding"),
        candidate.creation_request_hash,
        actor=_system(),
    )
    replay = await service.bind_policy_approval(
        TENANT,
        candidate.policy_version_id,
        ApprovalId("apr_binding"),
        candidate.creation_request_hash,
        actor=_system(),
    )
    assert replay == first

    with pytest.raises(CatalogPolicyApprovalConflictError):
        await service.bind_policy_approval(
            TENANT,
            candidate.policy_version_id,
            ApprovalId("apr_other"),
            candidate.creation_request_hash,
            actor=_system(),
        )
    with pytest.raises(CatalogPolicyApprovalConflictError):
        await service.bind_policy_approval(
            TENANT,
            candidate.policy_version_id,
            ApprovalId("apr_binding"),
            "f" * 64,
            actor=_system(),
        )


@pytest.mark.asyncio
async def test_approved_candidate_activates_and_supersedes_in_one_lifecycle() -> None:
    store = _Store()
    old = replace(
        _pending("old"),
        approval_id=ApprovalId("apr_old"),
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW,
    )
    candidate = _pending(
        "new", base=old.policy_version_id, content=_content(4), created_at=NOW
    )
    store.policies[old.policy_version_id] = old
    store.policies[candidate.policy_version_id] = candidate
    clock = _Clock(NOW + timedelta(minutes=1))
    service = _service(store, clock)
    bound = await _bind(service, candidate, "apr_new")

    active = await service.apply_policy_decision(
        TENANT,
        bound.policy_version_id,
        _decision(bound),
        actor=_system(),
    )

    assert active.state == "active"
    assert active.activated_at == clock.value
    assert (
        store.policies[old.policy_version_id].state
        is CatalogProposalPolicyState.SUPERSEDED
    )
    assert store.policies[old.policy_version_id].terminal_at == clock.value
    assert len(store.events) == 1

    replay = await service.apply_policy_decision(
        TENANT,
        bound.policy_version_id,
        _decision(bound),
        actor=_system(),
    )
    assert replay == active
    assert len(store.events) == 1


@pytest.mark.parametrize(
    ("decision_state", "expected_state"),
    [("rejected", "rejected"), ("expired", "expired")],
)
@pytest.mark.asyncio
async def test_rejected_and_expired_are_exact_terminal_states(
    decision_state: str, expected_state: str
) -> None:
    store = _Store()
    candidate = _pending(decision_state)
    store.policies[candidate.policy_version_id] = candidate
    service = _service(store)
    bound = await _bind(service, candidate, f"apr_{decision_state}")

    view = await service.apply_policy_decision(
        TENANT,
        bound.policy_version_id,
        _decision(bound, decision_state),
        actor=_system(),
    )

    assert view.state == expected_state
    assert view.terminal_at == NOW
    assert await service.get_active_policy(TENANT, actor=_actor()) is None
    assert store.events == []

    with pytest.raises(CatalogPolicyStateTransitionError):
        await service.apply_policy_decision(
            TENANT,
            bound.policy_version_id,
            _decision(bound),
            actor=_system(),
        )


@pytest.mark.asyncio
async def test_base_mismatch_marks_candidate_stale_without_touching_current_or_event() -> (
    None
):
    store = _Store()
    original_base = replace(
        _pending("original-base"),
        approval_id=ApprovalId("apr_original"),
        state=CatalogProposalPolicyState.SUPERSEDED,
        activated_at=NOW,
        terminal_at=NOW + timedelta(seconds=1),
    )
    current = replace(
        _pending("current", content=_content(4)),
        approval_id=ApprovalId("apr_current"),
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW + timedelta(seconds=1),
    )
    candidate = _pending("stale", base=original_base.policy_version_id, created_at=NOW)
    store.policies.update(
        {
            original_base.policy_version_id: original_base,
            current.policy_version_id: current,
            candidate.policy_version_id: candidate,
        }
    )
    service = _service(store, _Clock(NOW + timedelta(minutes=2)))
    bound = await _bind(service, candidate, "apr_stale")

    view = await service.apply_policy_decision(
        TENANT,
        bound.policy_version_id,
        _decision(bound),
        actor=_system(),
    )

    assert view.state == "stale"
    assert store.policies[current.policy_version_id] == current
    assert store.events == []


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("approval_id", "apr_wrong"),
        ("request_hash", "e" * 64),
        ("change_set_ref", "catalog-policy:cpv_wrong:" + "a" * 64),
        ("proposed_by_employee", "emp_wrong"),
        ("owner_employee", "emp_wrong"),
        ("approval_type", "catalog_product_cultivation"),
        ("contract_namespace", "catalog-cultivation-v1"),
    ],
)
@pytest.mark.asyncio
async def test_decision_requires_exact_bound_subject(override: str, value: str) -> None:
    store = _Store()
    candidate = _pending(f"decision-{override}")
    store.policies[candidate.policy_version_id] = candidate
    service = _service(store)
    bound = await _bind(service, candidate, "apr_exact")
    values = {override: value}
    if override == "approval_type":
        values["contract_namespace"] = "catalog-cultivation-v1"
    elif override == "contract_namespace":
        values["approval_type"] = "catalog_product_cultivation"

    with pytest.raises(
        (CatalogPolicyApprovalConflictError, CatalogPolicyDecisionInvalidError)
    ):
        await service.apply_policy_decision(
            TENANT,
            bound.policy_version_id,
            _decision(bound, **values),
            actor=_system(),
        )

    assert (
        store.policies[bound.policy_version_id].state
        is CatalogProposalPolicyState.PENDING_APPROVAL
    )
    assert store.events == []


@pytest.mark.asyncio
async def test_snapshot_and_history_are_safe_and_bound_to_current_state() -> None:
    store = _Store()
    base = replace(
        _pending("snapshot-base", created_at=NOW),
        approval_id=ApprovalId("apr_snapshot_base"),
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW,
    )
    candidate = _pending(
        "snapshot-candidate",
        base=base.policy_version_id,
        content=_content(4),
        created_at=NOW + timedelta(minutes=1),
    )
    store.policies[base.policy_version_id] = base
    store.policies[candidate.policy_version_id] = candidate
    service = _service(store)

    snapshot = await service.get_policy_change_snapshot(
        TENANT, candidate.policy_version_id, actor=_system()
    )
    history = await service.list_policy_versions(TENANT, actor=_actor(), limit=10)

    assert snapshot.base is not None
    assert snapshot.current is not None
    assert snapshot.base.policy_version_id == base.policy_version_id
    assert snapshot.current.policy_version_id == base.policy_version_id
    assert snapshot.candidate.policy_version_id == candidate.policy_version_id
    assert snapshot.base_is_current is True
    assert [item.policy_version_id for item in history] == [
        candidate.policy_version_id,
        base.policy_version_id,
    ]
    assert "creation_key" not in snapshot.candidate.model_dump()
    assert "creation_request_hash" not in snapshot.candidate.model_dump()


@pytest.mark.parametrize("limit", [True, 0, 201, "10"])
@pytest.mark.asyncio
async def test_history_limit_is_strict(limit: object) -> None:
    store = _Store()
    with pytest.raises(ValidationError):
        await _service(store).list_policy_versions(
            TENANT,
            actor=_actor(),
            limit=limit,  # type: ignore[arg-type]
        )
    assert store.enter_count == 0


@pytest.mark.asyncio
async def test_missing_and_dependency_failures_use_fixed_redacted_errors() -> None:
    store = _Store()
    service = _service(store)
    with pytest.raises(CatalogPolicyNotFoundError, match="目录提案策略不存在"):
        await service.get_policy_change_snapshot(
            TENANT,
            CatalogProposalPolicyVersionId("cpv_missing"),
            actor=_system(),
        )

    store.read_error = RuntimeError("postgresql://secret@host/private")
    with pytest.raises(TransientError) as caught:
        await service.get_active_policy(TENANT, actor=_actor())
    assert "secret" not in str(caught.value)
    assert "postgresql" not in str(caught.value)
