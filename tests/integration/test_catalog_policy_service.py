"""Catalog Proposal Policy 服务的真实 PostgreSQL 事务与并发测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.errors import CatalogPolicyApprovalConflictError
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.schemas import (
    CatalogApprovalDecisionInput,
    CatalogProposalPolicyContent,
)
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.repositories.catalog_products import CatalogPolicyRepositoryImpl
from infra.db.tables import CatalogProposalPolicyVersionRow, OutboxEventRow
from shared.errors import TransientError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProposalPolicyVersionId,
    TenantId,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _NamespaceBarrierRepository(CatalogPolicyRepositoryImpl):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        barrier: asyncio.Barrier,
        label: str,
        acquisition_order: list[str],
    ) -> None:
        super().__init__(session, tenant_id, session_factory)
        self._barrier = barrier
        self._label = label
        self._acquisition_order = acquisition_order
        self._namespace_locked = False

    async def lock_policy_namespace(self, tenant_id: TenantId) -> None:
        await self._barrier.wait()
        await super().lock_policy_namespace(tenant_id)
        self._namespace_locked = True
        self._acquisition_order.append(self._label)

    async def get_for_update(
        self,
        tenant_id: TenantId,
        policy_version_id: CatalogProposalPolicyVersionId,
    ):
        if not self._namespace_locked:
            raise AssertionError("policy row lock preceded namespace lock")
        return await super().get_for_update(tenant_id, policy_version_id)

    async def get_active(self, tenant_id: TenantId, *, for_update: bool = False):
        if for_update and not self._namespace_locked:
            raise AssertionError("active row lock preceded namespace lock")
        return await super().get_active(tenant_id, for_update=for_update)


class _NamespaceBarrierUow(SqlAlchemyCatalogProductsUnitOfWork):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        *,
        barrier: asyncio.Barrier,
        label: str,
        acquisition_order: list[str],
    ) -> None:
        super().__init__(session_factory, tenant_id)
        self._barrier = barrier
        self._label = label
        self._acquisition_order = acquisition_order

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        self.policies = _NamespaceBarrierRepository(
            self._session,
            self._tenant_id,
            self._factory,
            barrier=self._barrier,
            label=self._label,
            acquisition_order=self._acquisition_order,
        )
        return self


@pytest_asyncio.fixture
async def policy_engine(db_url: str) -> AsyncEngine:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _content(accounts: int = 3) -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=accounts,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


def _product(tenant: TenantId, employee: str) -> ProductActor:
    return ProductActor(employee, ProductRole.PRODUCT, tenant)


def _system(tenant: TenantId) -> ProductActor:
    return ProductActor("system:catalog-policy-workflow", ProductRole.SYSTEM, tenant)


def _service(
    factory: async_sessionmaker,
    tenant: TenantId,
    clock: _Clock,
) -> CatalogProposalServiceImpl:
    return CatalogProposalServiceImpl(
        lambda scoped_tenant: SqlAlchemyCatalogProductsUnitOfWork(
            factory, scoped_tenant
        ),
        Phase2ProductAuthorizer(tenant),
        now=clock,
    )


def _barrier_service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    clock: _Clock,
    *,
    barrier: asyncio.Barrier,
    label: str,
    acquisition_order: list[str],
) -> CatalogProposalServiceImpl:
    return CatalogProposalServiceImpl(
        lambda scoped_tenant: _NamespaceBarrierUow(
            factory,
            scoped_tenant,
            barrier=barrier,
            label=label,
            acquisition_order=acquisition_order,
        ),
        Phase2ProductAuthorizer(tenant),
        now=clock,
    )


async def _seed_employees(
    engine: AsyncEngine, tenant: str, proposer: str, reviewer: str
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (employee_id,tenant_id,name,role) VALUES "
                "(:proposer,:tenant,'Product owner','product'),"
                "(:reviewer,:tenant,'Independent boss','boss')"
            ),
            {"tenant": tenant, "proposer": proposer, "reviewer": reviewer},
        )


async def _approval(
    engine: AsyncEngine,
    *,
    tenant: str,
    policy_id: str,
    content_hash: str,
    request_hash: str,
    approval_id: str,
    proposer: str,
    reviewer: str,
    state: str = "approved",
) -> None:
    expires = NOW + timedelta(days=7)
    payload = {
        "schema_version": "catalog-policy-v1",
        "tenant_id": tenant,
        "approval_type": "catalog_proposal_policy_change",
        "policy_version_id": policy_id,
        "content_hash": content_hash,
        "request_hash": request_hash,
    }
    decided = state in {"approved", "rejected"}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO approval_packages "
                "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
                "blast_radius,created_at,expires_at,state,proposed_by_employee,"
                "evidence_refs,change_set_ref,owner_employee,contract_namespace,"
                "request_hash,expires_at_limit,decided_at,decided_by) VALUES "
                "(:tenant,:approval,'catalog_proposal_policy_change','Catalog policy',"
                "CAST(:payload AS jsonb),'internal only','{}'::jsonb,:now,:expires,"
                ":state,:proposer,'[]'::jsonb,:change_set,:proposer,'catalog-policy-v1',"
                ":request_hash,:expires,:decided_at,:decided_by)"
            ),
            {
                "tenant": tenant,
                "approval": approval_id,
                "payload": json.dumps(payload),
                "now": NOW,
                "expires": expires,
                "state": state,
                "proposer": proposer,
                "change_set": f"catalog-policy:{policy_id}:{content_hash}",
                "request_hash": request_hash,
                "decided_at": NOW if decided else None,
                "decided_by": reviewer if decided else None,
            },
        )


def _decision(
    *,
    policy_id: str,
    content_hash: str,
    request_hash: str,
    approval_id: str,
    proposer: str,
    reviewer: str,
    state: str = "approved",
) -> CatalogApprovalDecisionInput:
    decided = state in {"approved", "rejected"}
    return CatalogApprovalDecisionInput(
        approval_id=ApprovalId(approval_id),
        approval_type="catalog_proposal_policy_change",
        contract_namespace="catalog-policy-v1",
        change_set_ref=f"catalog-policy:{policy_id}:{content_hash}",
        request_hash=request_hash,
        state=state,  # type: ignore[arg-type]
        proposed_by_run=None,
        proposed_by_employee=proposer,
        owner_employee=proposer,
        decided_by_employee=reviewer if decided else None,
        decided_at=NOW if decided else None,
        expires_at=NOW + timedelta(days=7),
    )


async def _bind_and_apply(
    engine: AsyncEngine,
    service: CatalogProposalServiceImpl,
    tenant: TenantId,
    policy_id: str,
    approval_id: str,
    proposer: str,
    reviewer: str,
    *,
    state: str = "approved",
):
    async with SqlAlchemyCatalogProductsUnitOfWork(
        async_sessionmaker(engine, expire_on_commit=False), tenant
    ) as uow:
        stored = await uow.policies.get(tenant, policy_id)  # type: ignore[arg-type]
        assert stored is not None
    await _approval(
        engine,
        tenant=str(tenant),
        policy_id=str(stored.policy_version_id),
        content_hash=stored.content_hash,
        request_hash=stored.creation_request_hash,
        approval_id=approval_id,
        proposer=proposer,
        reviewer=reviewer,
        state=state,
    )
    await service.bind_policy_approval(
        tenant,
        stored.policy_version_id,
        ApprovalId(approval_id),
        stored.creation_request_hash,
        actor=_system(tenant),
    )
    return await service.apply_policy_decision(
        tenant,
        stored.policy_version_id,
        _decision(
            policy_id=str(stored.policy_version_id),
            content_hash=stored.content_hash,
            request_hash=stored.creation_request_hash,
            approval_id=approval_id,
            proposer=proposer,
            reviewer=reviewer,
            state=state,
        ),
        actor=_system(tenant),
    )


async def _cleanup(engine: AsyncEngine, tenant: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE catalog_proposal_policy_versions "
                "DISABLE TRIGGER trg_catalog_policy_version_guard"
            )
        )
        await connection.execute(
            text(
                "DELETE FROM catalog_proposal_policy_versions WHERE tenant_id=:tenant"
            ),
            {"tenant": tenant},
        )
        await connection.execute(
            text(
                "ALTER TABLE catalog_proposal_policy_versions "
                "ENABLE TRIGGER trg_catalog_policy_version_guard"
            )
        )
        await connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
        await connection.execute(
            text(
                "ALTER TABLE approval_packages DISABLE TRIGGER "
                "trg_approval_packages_guard"
            )
        )
        await connection.execute(
            text("DELETE FROM approval_packages WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
        await connection.execute(
            text(
                "ALTER TABLE approval_packages ENABLE TRIGGER "
                "trg_approval_packages_guard"
            )
        )
        await connection.execute(
            text("DELETE FROM employees WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )


async def test_real_postgres_lifecycle_supersedes_atomically_and_emits_once(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_policy_lifecycle")
    proposer = "emp_catalog_policy_owner"
    reviewer = "emp_catalog_policy_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    clock = _Clock(NOW)
    service = _service(factory, tenant, clock)
    try:
        assert (
            await service.get_active_policy(tenant, actor=_product(tenant, proposer))
            is None
        )
        first_id = await service.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="first-policy",
            actor=_product(tenant, proposer),
        )
        first = await _bind_and_apply(
            policy_engine,
            service,
            tenant,
            str(first_id),
            "apr_catalog_policy_first",
            proposer,
            reviewer,
        )
        assert first.state == "active"

        clock.value = NOW + timedelta(minutes=1)
        second_id = await service.create_policy_candidate(
            tenant,
            _content(4),
            idempotency_key="second-policy",
            actor=_product(tenant, proposer),
        )
        clock.value = NOW + timedelta(minutes=2)
        second = await _bind_and_apply(
            policy_engine,
            service,
            tenant,
            str(second_id),
            "apr_catalog_policy_second",
            proposer,
            reviewer,
        )
        replay = await service.apply_policy_decision(
            tenant,
            second.policy_version_id,
            _decision(
                policy_id=str(second.policy_version_id),
                content_hash=second.content_hash,
                request_hash=(
                    await _stored_request_hash(factory, tenant, str(second_id))
                ),
                approval_id="apr_catalog_policy_second",
                proposer=proposer,
                reviewer=reviewer,
            ),
            actor=_system(tenant),
        )

        assert second.state == "active"
        assert replay == second
        async with policy_engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(
                        CatalogProposalPolicyVersionRow.policy_version_id,
                        CatalogProposalPolicyVersionRow.state,
                    )
                    .where(CatalogProposalPolicyVersionRow.tenant_id == str(tenant))
                    .order_by(CatalogProposalPolicyVersionRow.created_at)
                )
            ).all()
            assert rows == [(str(first_id), "superseded"), (str(second_id), "active")]
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 2
            )
    finally:
        await _cleanup(policy_engine, str(tenant))


async def _stored_request_hash(
    factory: async_sessionmaker, tenant: TenantId, policy_id: str
) -> str:
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        policy = await uow.policies.get(tenant, policy_id)  # type: ignore[arg-type]
        assert policy is not None
        return policy.creation_request_hash


async def test_binding_atomically_rejects_persisted_approval_with_another_hash(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_policy_binding_hash")
    proposer = "emp_catalog_binding_owner"
    reviewer = "emp_catalog_binding_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    service = _service(factory, tenant, _Clock())
    try:
        policy_id = await service.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="approval-hash-policy",
            actor=_product(tenant, proposer),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            policy = await uow.policies.get(tenant, policy_id)
            assert policy is not None
        wrong_hash = "f" * 64
        assert wrong_hash != policy.creation_request_hash
        await _approval(
            policy_engine,
            tenant=str(tenant),
            policy_id=str(policy_id),
            content_hash=policy.content_hash,
            request_hash=wrong_hash,
            approval_id="apr_catalog_wrong_persisted_hash",
            proposer=proposer,
            reviewer=reviewer,
        )

        with pytest.raises(CatalogPolicyApprovalConflictError):
            await service.bind_policy_approval(
                tenant,
                policy_id,
                ApprovalId("apr_catalog_wrong_persisted_hash"),
                policy.creation_request_hash,
                actor=_system(tenant),
            )
        async with policy_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(CatalogProposalPolicyVersionRow.approval_id).where(
                        CatalogProposalPolicyVersionRow.tenant_id == str(tenant),
                        CatalogProposalPolicyVersionRow.policy_version_id
                        == str(policy_id),
                    )
                )
                is None
            )

        await _approval(
            policy_engine,
            tenant=str(tenant),
            policy_id=str(policy_id),
            content_hash=policy.content_hash,
            request_hash=policy.creation_request_hash,
            approval_id="apr_catalog_exact_persisted_hash",
            proposer=proposer,
            reviewer=reviewer,
        )
        first = await service.bind_policy_approval(
            tenant,
            policy_id,
            ApprovalId("apr_catalog_exact_persisted_hash"),
            policy.creation_request_hash,
            actor=_system(tenant),
        )
        replay = await service.bind_policy_approval(
            tenant,
            policy_id,
            ApprovalId("apr_catalog_exact_persisted_hash"),
            policy.creation_request_hash,
            actor=_system(tenant),
        )
        assert first == replay
        assert replay.approval_id == "apr_catalog_exact_persisted_hash"
    finally:
        await _cleanup(policy_engine, str(tenant))


async def test_concurrent_approved_replay_has_one_transition_and_one_event(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_policy_concurrent")
    proposer = "emp_catalog_concurrent_owner"
    reviewer = "emp_catalog_concurrent_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    service = _service(factory, tenant, _Clock())
    try:
        policy_id = await service.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="concurrent-policy",
            actor=_product(tenant, proposer),
        )
        request_hash = await _stored_request_hash(factory, tenant, str(policy_id))
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            policy = await uow.policies.get(tenant, policy_id)
            assert policy is not None
        await _approval(
            policy_engine,
            tenant=str(tenant),
            policy_id=str(policy_id),
            content_hash=policy.content_hash,
            request_hash=request_hash,
            approval_id="apr_catalog_concurrent",
            proposer=proposer,
            reviewer=reviewer,
        )
        await service.bind_policy_approval(
            tenant,
            policy_id,
            ApprovalId("apr_catalog_concurrent"),
            request_hash,
            actor=_system(tenant),
        )
        decision = _decision(
            policy_id=str(policy_id),
            content_hash=policy.content_hash,
            request_hash=request_hash,
            approval_id="apr_catalog_concurrent",
            proposer=proposer,
            reviewer=reviewer,
        )

        results = await asyncio.gather(
            service.apply_policy_decision(
                tenant, policy_id, decision, actor=_system(tenant)
            ),
            service.apply_policy_decision(
                tenant, policy_id, decision, actor=_system(tenant)
            ),
        )

        assert results[0] == results[1]
        assert results[0].state == "active"
        async with policy_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(policy_engine, str(tenant))


async def test_base_race_marks_loser_stale_and_never_emits_for_it(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_policy_stale")
    proposer = "emp_catalog_stale_owner"
    reviewer = "emp_catalog_stale_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    clock = _Clock()
    service = _service(factory, tenant, clock)
    try:
        base_id = await service.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="stale-base",
            actor=_product(tenant, proposer),
        )
        await _bind_and_apply(
            policy_engine,
            service,
            tenant,
            str(base_id),
            "apr_catalog_stale_base",
            proposer,
            reviewer,
        )
        clock.value = NOW + timedelta(minutes=1)
        winner_id = await service.create_policy_candidate(
            tenant,
            _content(4),
            idempotency_key="stale-winner",
            actor=_product(tenant, proposer),
        )
        loser_id = await service.create_policy_candidate(
            tenant,
            _content(5),
            idempotency_key="stale-loser",
            actor=_product(tenant, proposer),
        )
        clock.value = NOW + timedelta(minutes=2)
        await _bind_and_apply(
            policy_engine,
            service,
            tenant,
            str(winner_id),
            "apr_catalog_stale_winner",
            proposer,
            reviewer,
        )
        loser_hash = await _stored_request_hash(factory, tenant, str(loser_id))
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            loser = await uow.policies.get(tenant, loser_id)
            assert loser is not None
        await _approval(
            policy_engine,
            tenant=str(tenant),
            policy_id=str(loser_id),
            content_hash=loser.content_hash,
            request_hash=loser_hash,
            approval_id="apr_catalog_stale_loser",
            proposer=proposer,
            reviewer=reviewer,
        )
        await service.bind_policy_approval(
            tenant,
            loser_id,
            ApprovalId("apr_catalog_stale_loser"),
            loser_hash,
            actor=_system(tenant),
        )
        stale = await service.apply_policy_decision(
            tenant,
            loser_id,
            _decision(
                policy_id=str(loser_id),
                content_hash=loser.content_hash,
                request_hash=loser_hash,
                approval_id="apr_catalog_stale_loser",
                proposer=proposer,
                reviewer=reviewer,
            ),
            actor=_system(tenant),
        )

        assert stale.state == "stale"
        current = await service.get_active_policy(
            tenant, actor=_product(tenant, proposer)
        )
        assert current is not None
        assert current.policy_version_id == winner_id
        async with policy_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 2
            )
    finally:
        await _cleanup(policy_engine, str(tenant))


async def test_empty_active_concurrent_approvals_converge_to_active_and_stale(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_policy_empty_race")
    proposer = "emp_catalog_empty_race_owner"
    reviewer = "emp_catalog_empty_race_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    normal = _service(factory, tenant, _Clock())
    try:
        first_id = await normal.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="empty-race-first",
            actor=_product(tenant, proposer),
        )
        second_id = await normal.create_policy_candidate(
            tenant,
            _content(4),
            idempotency_key="empty-race-second",
            actor=_product(tenant, proposer),
        )
        subjects = []
        for policy_id, approval_id in (
            (first_id, "apr_catalog_empty_race_first"),
            (second_id, "apr_catalog_empty_race_second"),
        ):
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                policy = await uow.policies.get(tenant, policy_id)
                assert policy is not None
            assert policy.base_active_version_id is None
            subjects.append((policy, approval_id))
            await _approval(
                policy_engine,
                tenant=str(tenant),
                policy_id=str(policy_id),
                content_hash=policy.content_hash,
                request_hash=policy.creation_request_hash,
                approval_id=approval_id,
                proposer=proposer,
                reviewer=reviewer,
            )
            await normal.bind_policy_approval(
                tenant,
                policy_id,
                ApprovalId(approval_id),
                policy.creation_request_hash,
                actor=_system(tenant),
            )

        barrier = asyncio.Barrier(2)
        acquisition_order: list[str] = []
        concurrent = _barrier_service(
            factory,
            tenant,
            _Clock(),
            barrier=barrier,
            label="apply",
            acquisition_order=acquisition_order,
        )
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    concurrent.apply_policy_decision(
                        tenant,
                        policy.policy_version_id,
                        _decision(
                            policy_id=str(policy.policy_version_id),
                            content_hash=policy.content_hash,
                            request_hash=policy.creation_request_hash,
                            approval_id=approval_id,
                            proposer=proposer,
                            reviewer=reviewer,
                        ),
                        actor=_system(tenant),
                    )
                    for policy, approval_id in subjects
                )
            ),
            timeout=5,
        )

        assert sorted(item.state for item in results) == ["active", "stale"]
        assert acquisition_order == ["apply", "apply"]
        async with policy_engine.connect() as connection:
            states = (
                (
                    await connection.execute(
                        select(CatalogProposalPolicyVersionRow.state)
                        .where(
                            CatalogProposalPolicyVersionRow.tenant_id == str(tenant),
                            CatalogProposalPolicyVersionRow.policy_version_id.in_(
                                [str(first_id), str(second_id)]
                            ),
                        )
                        .order_by(CatalogProposalPolicyVersionRow.state)
                    )
                )
                .scalars()
                .all()
            )
            assert states == ["active", "stale"]
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(policy_engine, str(tenant))


async def test_policy_namespace_lock_does_not_block_another_tenant(
    policy_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    first_tenant = TenantId("tn_cat_lock_first")
    second_tenant = TenantId("tn_cat_lock_second")
    first_acquired = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first() -> None:
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, first_tenant) as uow:
            await uow.policies.lock_policy_namespace(first_tenant)
            first_acquired.set()
            await release_first.wait()

    task = asyncio.create_task(hold_first())
    await asyncio.wait_for(first_acquired.wait(), timeout=2)
    try:
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, second_tenant) as uow:
            await asyncio.wait_for(
                uow.policies.lock_policy_namespace(second_tenant), timeout=1
            )
    finally:
        release_first.set()
        await asyncio.wait_for(task, timeout=2)


async def test_create_and_activation_overlap_linearizes_base_without_deadlock(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_cat_create_active")
    proposer = "emp_cat_create_active_owner"
    reviewer = "emp_cat_create_active_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    normal = _service(factory, tenant, _Clock())
    try:
        activating_id = await normal.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="create-activate-first",
            actor=_product(tenant, proposer),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            activating = await uow.policies.get(tenant, activating_id)
            assert activating is not None
        await _approval(
            policy_engine,
            tenant=str(tenant),
            policy_id=str(activating_id),
            content_hash=activating.content_hash,
            request_hash=activating.creation_request_hash,
            approval_id="apr_catalog_create_activate",
            proposer=proposer,
            reviewer=reviewer,
        )
        await normal.bind_policy_approval(
            tenant,
            activating_id,
            ApprovalId("apr_catalog_create_activate"),
            activating.creation_request_hash,
            actor=_system(tenant),
        )

        barrier = asyncio.Barrier(2)
        acquisition_order: list[str] = []
        applying = _barrier_service(
            factory,
            tenant,
            _Clock(),
            barrier=barrier,
            label="activation",
            acquisition_order=acquisition_order,
        )
        creating = _barrier_service(
            factory,
            tenant,
            _Clock(),
            barrier=barrier,
            label="create",
            acquisition_order=acquisition_order,
        )
        activated, created_id = await asyncio.wait_for(
            asyncio.gather(
                applying.apply_policy_decision(
                    tenant,
                    activating_id,
                    _decision(
                        policy_id=str(activating_id),
                        content_hash=activating.content_hash,
                        request_hash=activating.creation_request_hash,
                        approval_id="apr_catalog_create_activate",
                        proposer=proposer,
                        reviewer=reviewer,
                    ),
                    actor=_system(tenant),
                ),
                creating.create_policy_candidate(
                    tenant,
                    _content(4),
                    idempotency_key="create-during-activation",
                    actor=_product(tenant, proposer),
                ),
            ),
            timeout=5,
        )

        assert activated.state == "active"
        assert len(acquisition_order) == 2
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            created = await uow.policies.get(tenant, created_id)
            assert created is not None
        expected_base = activating_id if acquisition_order[0] == "activation" else None
        assert created.base_active_version_id == expected_base
        assert created.state.value == "pending_approval"
        async with policy_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 1
            )
    finally:
        await _cleanup(policy_engine, str(tenant))


async def test_outbox_failure_rolls_back_activation_and_event(
    policy_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_policy_rollback")
    proposer = "emp_catalog_rollback_owner"
    reviewer = "emp_catalog_rollback_boss"
    await _seed_employees(policy_engine, str(tenant), proposer, reviewer)
    factory = async_sessionmaker(policy_engine, expire_on_commit=False)
    service = _service(factory, tenant, _Clock())
    armed = False

    def fail_outbox(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if armed and statement.lstrip().upper().startswith("INSERT INTO OUTBOX_EVENTS"):
            raise RuntimeError("private database detail")

    try:
        policy_id = await service.create_policy_candidate(
            tenant,
            _content(),
            idempotency_key="rollback-policy",
            actor=_product(tenant, proposer),
        )
        request_hash = await _stored_request_hash(factory, tenant, str(policy_id))
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            policy = await uow.policies.get(tenant, policy_id)
            assert policy is not None
        await _approval(
            policy_engine,
            tenant=str(tenant),
            policy_id=str(policy_id),
            content_hash=policy.content_hash,
            request_hash=request_hash,
            approval_id="apr_catalog_rollback",
            proposer=proposer,
            reviewer=reviewer,
        )
        await service.bind_policy_approval(
            tenant,
            policy_id,
            ApprovalId("apr_catalog_rollback"),
            request_hash,
            actor=_system(tenant),
        )
        event.listen(policy_engine.sync_engine, "before_cursor_execute", fail_outbox)
        armed = True
        with pytest.raises(TransientError) as caught:
            await service.apply_policy_decision(
                tenant,
                policy_id,
                _decision(
                    policy_id=str(policy_id),
                    content_hash=policy.content_hash,
                    request_hash=request_hash,
                    approval_id="apr_catalog_rollback",
                    proposer=proposer,
                    reviewer=reviewer,
                ),
                actor=_system(tenant),
            )
        assert "private" not in str(caught.value)
        armed = False
        event.remove(policy_engine.sync_engine, "before_cursor_execute", fail_outbox)

        async with policy_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(CatalogProposalPolicyVersionRow.state).where(
                        CatalogProposalPolicyVersionRow.tenant_id == str(tenant),
                        CatalogProposalPolicyVersionRow.policy_version_id
                        == str(policy_id),
                    )
                )
                == "pending_approval"
            )
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 0
            )
    finally:
        if armed:
            event.remove(
                policy_engine.sync_engine, "before_cursor_execute", fail_outbox
            )
        await _cleanup(policy_engine, str(tenant))
