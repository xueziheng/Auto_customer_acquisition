"""候选产品服务在真实 Postgres UoW 中的幂等与回滚证明。"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from infra.db.tables import ProductCandidateSourceRow, ProductRow
from shared.errors import TransientError
from shared.schemas.identifiers import (
    ArtifactId,
    ProductId,
    SourcingCaseId,
    SupplierCandidateId,
    SupplierId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _symbol(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


@pytest_asyncio.fixture
async def candidate_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_origin(
    engine: AsyncEngine,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    candidate_id: SupplierCandidateId,
    artifact_ids: tuple[ArtifactId, ...],
) -> None:
    need_id = ValidatedNeedId(new_id("need"))
    async with engine.begin() as connection:
        for index, artifact_id in enumerate(artifact_ids):
            await connection.execute(
                text(
                    "INSERT INTO raw_artifacts "
                    "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
                    "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :key, :now)"
                ),
                {
                    "tenant": tenant_id,
                    "artifact": artifact_id,
                    "hash": f"{index + 1:064x}",
                    "key": f"raw/{tenant_id}/{artifact_id}",
                    "now": NOW,
                },
            )
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
                "VALUES (:tenant, :need, 'account-a', '{\"value\":\"hinges\"}', "
                "'message-a', 'sourcing_ready', :now)"
            ),
            {"tenant": tenant_id, "need": need_id, "now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO sourcing_cases "
                "(tenant_id, case_id, need_id, workflow_version, trigger_key, need_snapshot, "
                "need_snapshot_hash, state, version, opened_at, state_changed_at) VALUES "
                "(:tenant, :case, :need, 2, :trigger, '{}', :hash, 'opened', 1, :now, :now)"
            ),
            {
                "tenant": tenant_id,
                "case": case_id,
                "need": need_id,
                "trigger": f"sourcing-v2:{need_id}",
                "hash": "a" * 64,
                "now": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO sourcing_candidates "
                "(tenant_id, candidate_id, case_id, supplier_name, product_title, "
                "observed_facts, supplier_claims, match_inferences, verified_specs, "
                "indicative_price_tiers, rejection_reasons, rejected, created_at) VALUES "
                "(:tenant, :candidate, :case, 'Factory A', 'Hinge', '{}', '{}', '{}', "
                "'[]', CAST(:tiers AS jsonb), '[]', false, :now)"
            ),
            {
                "tenant": tenant_id,
                "candidate": candidate_id,
                "case": case_id,
                "tiers": json.dumps(
                    [
                        {
                            "minimum_quantity": 100,
                            "amount": "1.25",
                            "currency": "USD",
                            "unit": "piece",
                            "provenance": {},
                            "evidence_ref": str(artifact_ids[0]),
                        }
                    ]
                ),
                "now": NOW,
            },
        )


def _command(
    case_id: SourcingCaseId, candidate_id: SupplierCandidateId, artifact: ArtifactId
) -> Any:
    Command = _symbol("domains.products.schemas", "CandidateProductCreate")
    return Command.model_validate(
        {
            "sourcing_case_id": case_id,
            "supplier_candidate_id": candidate_id,
            "name_zh": "铰链",
            "name_en": "Hinge",
            "category": "hinges",
            "spec_summary": "304 stainless",
            "moq": 100,
            "evidence_refs": (artifact,),
            "indicative_prices": (
                {
                    "minimum_quantity": 100,
                    "unit_amount": Decimal("9999999999999999.999999999999"),
                    "currency": "USD",
                    "unit": "piece",
                    "evidence_ref": artifact,
                },
            ),
        }
    )


def _actor(tenant_id: TenantId, role: str = "system") -> Any:
    Actor = _symbol("domains.products.permissions", "ProductActor")
    Role = _symbol("domains.products.permissions", "ProductRole")
    return Actor(actor_id="system", role=Role(role), tenant_id=tenant_id)


def _service(factory: object) -> Any:
    Service = _symbol("domains.products.service_impl", "ProductServiceImpl")
    Authorizer = _symbol("domains.products.permissions", "Phase2ProductAuthorizer")
    tenant = getattr(factory, "tenant_id", None)
    return Service(factory, Authorizer(tenant), now=lambda: NOW)


class _SqlFactory:
    def __init__(self, sf: object, tenant_id: TenantId) -> None:
        self.sf = sf
        self.tenant_id = tenant_id

    def __call__(self, tenant_id: TenantId) -> Any:
        Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
        return Uow(self.sf, tenant_id)


@pytest.mark.asyncio
async def test_repeated_candidate_creation_persists_one_card_and_exact_price(
    candidate_engine: AsyncEngine,
) -> None:
    """重复事件必须返回同一 ID，真实表中只能有一张卡和一个来源。"""

    tenant_id = TenantId(new_id("tn"))
    case_id = SourcingCaseId(new_id("src"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    artifact = ArtifactId(new_id("art"))
    await _seed_origin(candidate_engine, tenant_id, case_id, candidate_id, (artifact,))
    sf = async_sessionmaker(candidate_engine, expire_on_commit=False)
    factory = _SqlFactory(sf, tenant_id)
    service = _service(factory)

    first = await service.create_candidate_from_sourcing(
        tenant_id, _command(case_id, candidate_id, artifact), actor=_actor(tenant_id)
    )
    second = await service.create_candidate_from_sourcing(
        tenant_id, _command(case_id, candidate_id, artifact), actor=_actor(tenant_id)
    )
    assert first == second
    internal = await service.get_internal_view(
        tenant_id, first, actor=_actor(tenant_id, "sourcing")
    )
    assert internal.candidate_source is not None
    assert internal.candidate_source.sourcing_case_id == case_id
    assert internal.candidate_source.supplier_candidate_id == candidate_id
    boundary_amount = Decimal("9999999999999999.999999999999")
    persisted_amount = internal.candidate_source.indicative_prices[0].unit_amount
    assert persisted_amount == boundary_amount
    assert persisted_amount.as_tuple() == boundary_amount.as_tuple()
    other_tenant = TenantId(new_id("tn"))
    async with factory(other_tenant) as uow:
        assert await uow.candidate_sources.get_by_product(
            other_tenant, first
        ) is None
        with pytest.raises(ValueError, match="租户"):
            await uow.candidate_sources.get_by_product(tenant_id, first)
    async with candidate_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ProductRow)
                .where(ProductRow.tenant_id == tenant_id)
            )
            == 1
        )
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ProductCandidateSourceRow)
                .where(ProductCandidateSourceRow.tenant_id == tenant_id)
            )
            == 1
        )
class _RaceSourceRepository:
    def __init__(self, real: Any, canonical: Any) -> None:
        self.real = real
        self.canonical = canonical
        self.first_lookup = True

    async def get_by_origin(
        self, tenant_id: TenantId, case_id: object, candidate_id: object
    ) -> Any | None:
        if self.first_lookup:
            self.first_lookup = False
            return None
        return await self.real.get_by_origin(tenant_id, case_id, candidate_id)

    async def get_by_product(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Any | None:
        return await self.real.get_by_product(tenant_id, product_id)

    async def add(self, tenant_id: TenantId, source: Any) -> Any:
        return self.canonical


class _RaceUow:
    def __init__(self, real: Any, canonical: Any) -> None:
        self.real = real
        self.canonical = canonical

    async def __aenter__(self) -> Self:
        entered = await self.real.__aenter__()
        self.products = entered.products
        self.capabilities = entered.capabilities
        self.bus = entered.bus
        self.candidate_sources = _RaceSourceRepository(
            entered.candidate_sources, self.canonical
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.real.__aexit__(exc_type, exc, tb)


class _RaceFactory(_SqlFactory):
    def __init__(self, sf: object, tenant_id: TenantId, canonical: Any) -> None:
        super().__init__(sf, tenant_id)
        self.canonical = canonical
        self.calls = 0

    def __call__(self, tenant_id: TenantId) -> Any:
        self.calls += 1
        real = super().__call__(tenant_id)
        if self.calls == 1:
            return _RaceUow(real, self.canonical)
        return real


class _InvisibleRaceFactory(_SqlFactory):
    def __init__(self, sf: object, tenant_id: TenantId, canonical: Any) -> None:
        super().__init__(sf, tenant_id)
        self.canonical = canonical

    def __call__(self, tenant_id: TenantId) -> Any:
        return _RaceUow(super().__call__(tenant_id), self.canonical)


class _TwoLookupBarrier:
    def __init__(self) -> None:
        self.arrivals = 0
        self.lock = asyncio.Lock()
        self.released = asyncio.Event()

    async def wait(self) -> None:
        async with self.lock:
            self.arrivals += 1
            if self.arrivals == 2:
                self.released.set()
        await self.released.wait()


class _BarrierSourceRepository:
    def __init__(self, real: Any, barrier: _TwoLookupBarrier) -> None:
        self.real = real
        self.barrier = barrier
        self.first_lookup = True

    async def get_by_origin(
        self, tenant_id: TenantId, case_id: object, candidate_id: object
    ) -> Any | None:
        result = await self.real.get_by_origin(tenant_id, case_id, candidate_id)
        if self.first_lookup and result is None:
            self.first_lookup = False
            await self.barrier.wait()
        return result

    async def get_by_product(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Any | None:
        return await self.real.get_by_product(tenant_id, product_id)

    async def add(self, tenant_id: TenantId, source: Any) -> Any:
        return await self.real.add(tenant_id, source)


class _BarrierUow:
    def __init__(self, real: Any, barrier: _TwoLookupBarrier) -> None:
        self.real = real
        self.barrier = barrier

    async def __aenter__(self) -> Self:
        entered = await self.real.__aenter__()
        self.products = entered.products
        self.capabilities = entered.capabilities
        self.bus = entered.bus
        self.candidate_sources = _BarrierSourceRepository(
            entered.candidate_sources, self.barrier
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.real.__aexit__(exc_type, exc, tb)


class _BarrierFactory(_SqlFactory):
    def __init__(
        self, sf: object, tenant_id: TenantId, barrier: _TwoLookupBarrier
    ) -> None:
        super().__init__(sf, tenant_id)
        self.barrier = barrier

    def __call__(self, tenant_id: TenantId) -> Any:
        return _BarrierUow(super().__call__(tenant_id), self.barrier)


@pytest.mark.asyncio
async def test_canonical_race_winner_rolls_back_new_orphan_product(
    candidate_engine: AsyncEngine,
) -> None:
    """来源唯一键被并发事务抢先时，新产品必须随冲突事务回滚。"""

    tenant_id = TenantId(new_id("tn"))
    case_id = SourcingCaseId(new_id("src"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    artifact = ArtifactId(new_id("art"))
    await _seed_origin(candidate_engine, tenant_id, case_id, candidate_id, (artifact,))
    sf = async_sessionmaker(candidate_engine, expire_on_commit=False)
    normal_factory = _SqlFactory(sf, tenant_id)
    service = _service(normal_factory)
    canonical_id = await service.create_candidate_from_sourcing(
        tenant_id, _command(case_id, candidate_id, artifact), actor=_actor(tenant_id)
    )
    async with normal_factory(tenant_id) as uow:
        canonical = await uow.candidate_sources.get_by_origin(
            tenant_id, case_id, candidate_id
        )
    assert canonical is not None and canonical.product_id == canonical_id

    racing = _service(_RaceFactory(sf, tenant_id, canonical))
    converged_id = await racing.create_candidate_from_sourcing(
        tenant_id,
        _command(case_id, candidate_id, artifact),
        actor=_actor(tenant_id),
    )
    assert converged_id == canonical_id
    async with candidate_engine.connect() as connection:
        product_ids = list(
            (
                await connection.execute(
                    select(ProductRow.product_id).where(
                        ProductRow.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
    assert product_ids == [canonical_id]


@pytest.mark.asyncio
async def test_concurrent_candidate_creates_converge_after_real_unique_race(
    candidate_engine: AsyncEngine,
) -> None:
    """两个真实事务先同时读空后，只能提交一个 Product/source 并返回同一 ID。"""

    tenant_id = TenantId(new_id("tn"))
    case_id = SourcingCaseId(new_id("src"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    artifact = ArtifactId(new_id("art"))
    await _seed_origin(candidate_engine, tenant_id, case_id, candidate_id, (artifact,))
    sf = async_sessionmaker(candidate_engine, expire_on_commit=False)
    factory = _BarrierFactory(sf, tenant_id, _TwoLookupBarrier())
    command = _command(case_id, candidate_id, artifact)

    first, second = await asyncio.gather(
        _service(factory).create_candidate_from_sourcing(
            tenant_id, command, actor=_actor(tenant_id)
        ),
        _service(factory).create_candidate_from_sourcing(
            tenant_id, command, actor=_actor(tenant_id)
        ),
    )

    assert first == second
    async with candidate_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ProductRow)
                .where(ProductRow.tenant_id == tenant_id)
            )
            == 1
        )
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ProductCandidateSourceRow)
                .where(ProductCandidateSourceRow.tenant_id == tenant_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_canonical_race_without_visible_winner_is_fixed_detached_transient(
    candidate_engine: AsyncEngine,
) -> None:
    """回滚后的 fresh read 尚不可见时只能安全重试，不能永久失败或泄漏下层。"""

    tenant_id = TenantId(new_id("tn"))
    case_id = SourcingCaseId(new_id("src"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    artifact = ArtifactId(new_id("art"))
    await _seed_origin(candidate_engine, tenant_id, case_id, candidate_id, (artifact,))
    sf = async_sessionmaker(candidate_engine, expire_on_commit=False)
    normal_factory = _SqlFactory(sf, tenant_id)
    canonical_id = await _service(normal_factory).create_candidate_from_sourcing(
        tenant_id,
        _command(case_id, candidate_id, artifact),
        actor=_actor(tenant_id),
    )
    async with normal_factory(tenant_id) as uow:
        canonical = await uow.candidate_sources.get_by_origin(
            tenant_id, case_id, candidate_id
        )
    assert canonical is not None

    with pytest.raises(TransientError, match="canonical 来源暂不可见") as caught:
        await _service(
            _InvisibleRaceFactory(sf, tenant_id, canonical)
        ).create_candidate_from_sourcing(
            tenant_id,
            _command(case_id, candidate_id, artifact),
            actor=_actor(tenant_id),
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    async with candidate_engine.connect() as connection:
        assert list(
            (
                await connection.execute(
                    select(ProductRow.product_id).where(
                        ProductRow.tenant_id == tenant_id
                    )
                )
            ).scalars()
        ) == [canonical_id]
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ProductCandidateSourceRow)
                .where(ProductCandidateSourceRow.tenant_id == tenant_id)
            )
            == 1
        )


class _SupplierSqlFactory:
    def __init__(self, sf: object, tenant_id: TenantId) -> None:
        self.sf = sf
        self.tenant_id = tenant_id

    def __call__(self, tenant_id: TenantId) -> Any:
        Uow = _symbol("infra.db.suppliers_uow", "SqlAlchemySuppliersUnitOfWork")
        return Uow(self.sf, tenant_id)


class _DirectQuoteReader:
    def __init__(self, evidence: Any) -> None:
        self.evidence = evidence

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> Any:
        assert tenant_id == self.evidence.tenant_id
        assert artifact_id == self.evidence.artifact_id
        return self.evidence


@pytest.mark.asyncio
async def test_supplier_quote_numeric_boundary_round_trips_exactly_in_postgres(
    candidate_engine: AsyncEngine,
) -> None:
    """供应商报价的 NUMERIC(28,12) 最大边界不能被数据库量化或截断。"""

    tenant_id = TenantId(new_id("tn"))
    case_id = SourcingCaseId(new_id("src"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    artifact = ArtifactId(new_id("art"))
    supplier_id = SupplierId(new_id("sup"))
    await _seed_origin(candidate_engine, tenant_id, case_id, candidate_id, (artifact,))
    sf = async_sessionmaker(candidate_engine, expire_on_commit=False)
    factory = _SupplierSqlFactory(sf, tenant_id)
    Supplier = _symbol("domains.suppliers.service", "Supplier")
    SupplierPriceRecord = _symbol(
        "domains.suppliers.service", "SupplierPriceRecord"
    )
    SupplierQuoteEvidence = _symbol(
        "domains.suppliers.service", "SupplierQuoteEvidence"
    )
    SupplierQuoteSourceKind = _symbol(
        "domains.suppliers.service", "SupplierQuoteSourceKind"
    )
    SupplierActor = _symbol("domains.suppliers.service", "SupplierActor")
    SupplierRole = _symbol("domains.suppliers.service", "SupplierRole")
    Phase2SupplierAuthorizer = _symbol(
        "domains.suppliers.service", "Phase2SupplierAuthorizer"
    )
    SupplierServiceImpl = _symbol(
        "domains.suppliers.service_impl", "SupplierServiceImpl"
    )
    amount = Decimal("9999999999999999.999999999999")
    valid_until = NOW + timedelta(days=1)
    record = SupplierPriceRecord(
        supplier_id=supplier_id,
        tenant_id=tenant_id,
        product_desc="304 stainless hinge",
        quantity_tier=100,
        price=Money(amount, CurrencyCode("USD")),
        basis="quoted",
        observed_at=NOW,
        evidence_ref=artifact,
        valid_until=valid_until,
    )
    evidence = SupplierQuoteEvidence(
        tenant_id=tenant_id,
        artifact_id=artifact,
        supplier_id=supplier_id,
        product_desc=record.product_desc,
        quantity_tier=record.quantity_tier,
        price=record.price,
        observed_at=NOW,
        valid_until=valid_until,
        source_kind=SupplierQuoteSourceKind.DIRECT_SUPPLIER_QUOTE,
    )
    service = SupplierServiceImpl(
        factory,
        Phase2SupplierAuthorizer(tenant_id),
        _DirectQuoteReader(evidence),
        now=lambda: NOW,
    )
    actor = SupplierActor(
        actor_id="system", role=SupplierRole.SYSTEM, tenant_id=tenant_id
    )
    await service.register(
        tenant_id,
        Supplier(
            supplier_id=supplier_id,
            tenant_id=tenant_id,
            name="Factory A",
            created_at=NOW,
        ),
        actor=actor,
    )
    await service.record_price(tenant_id, record, actor=actor)

    async with factory(tenant_id) as uow:
        rows = await uow.prices.list_for_supplier(
            tenant_id, supplier_id, record.product_desc
        )
    assert len(rows) == 1
    assert rows[0].price.amount == amount
    assert rows[0].price.amount.as_tuple() == amount.as_tuple()
