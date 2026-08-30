"""产品与供应商供给池仓储合同。"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from infra.db.tables import (
    OutboxEventRow,
    ProductCandidatePriceRefRow,
    ProductCandidateSourceRow,
    ProductRow,
    SupplierRow,
)
from shared.errors import ValidationError
from shared.events.catalog import SourcingCandidatesReady
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

NOW = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)


def _symbol(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


CandidateStatus = _symbol("domains.products.models", "CandidateStatus")
Product = _symbol("domains.products.models", "Product")
ProductPool = _symbol("domains.products.models", "ProductPool")
SupplyCapability = _symbol("domains.products.models", "SupplyCapability")
Supplier = _symbol("domains.suppliers.models", "Supplier")
SupplierPriceRecord = _symbol("domains.suppliers.models", "SupplierPriceRecord")
SupplierVerification = _symbol("domains.suppliers.models", "SupplierVerification")


@pytest_asyncio.fixture
async def supply_engine(db_url: str) -> AsyncEngine:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _tenant() -> TenantId:
    return TenantId(new_id("tn"))


def _product(
    tenant_id: TenantId,
    product_id: ProductId,
    *,
    category: str = " Stainless Hinges ",
    status: CandidateStatus = CandidateStatus.SOURCE_ONLY,
) -> Product:
    return Product(
        product_id=product_id, tenant_id=tenant_id, pool=ProductPool.CANDIDATE,
        candidate_status=status, name_zh="不锈钢铰链", name_en="Stainless Hinge",
        category=category, spec_summary="304 stainless", moq=1000,
        created_at=NOW, allowed_price_min=Money(Decimal("0.123456789012"), CurrencyCode("USD")),
        allowed_price_max=Money(Decimal("0.987654321098"), CurrencyCode("USD")),
        sellable_markets=["US"], customizable=True, selling_points=["corrosion resistant"],
        known_issues=["finish requires confirmation"],
    )


def _supplier(tenant_id: TenantId, supplier_id: SupplierId, tags: list[str]) -> Supplier:
    return Supplier(
        supplier_id=supplier_id, tenant_id=tenant_id, name=f"Factory {supplier_id}",
        created_at=NOW, region="CN-ZJ", platform_refs=["official-site"],
        capability_tags=tags, verification=SupplierVerification.BASIC_CHECKED,
    )


async def _seed_artifact(
    engine: AsyncEngine, tenant_id: TenantId, artifact_id: ArtifactId
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
                "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :key, :now)"
            ),
            {
                "tenant": tenant_id, "artifact": artifact_id, "hash": "f" * 64,
                "key": f"raw/{tenant_id}/{artifact_id}", "now": NOW,
            },
        )


async def _seed_sourcing_candidate(
    engine: AsyncEngine,
    tenant_id: TenantId,
    case_id: SourcingCaseId,
    candidate_id: SupplierCandidateId,
    artifact_id: ArtifactId,
) -> None:
    need_id = ValidatedNeedId(new_id("need"))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, "
                "status, created_at) VALUES "
                "(:tenant, :need, 'account-a', '{\"value\":\"hinges\"}', "
                "'message-a', 'sourcing_ready', :now)"
            ),
            {"tenant": tenant_id, "need": need_id, "now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO sourcing_cases "
                "(tenant_id, case_id, need_id, workflow_version, trigger_key, "
                "need_snapshot, need_snapshot_hash, state, version, opened_at, "
                "state_changed_at) VALUES "
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
                            "minimum_quantity": 1000,
                            "amount": "1.25",
                            "currency": "USD",
                            "unit": "piece",
                            "provenance": {
                                "source_type": "web_page",
                                "source_id": "page-a",
                                "extracted_by": "human",
                                "extracted_at": NOW.isoformat(),
                                "confirmed_by": None,
                                "confirmed_at": None,
                            },
                            "evidence_ref": str(artifact_id),
                        }
                    ]
                ),
                "now": NOW,
            },
        )


@pytest.mark.parametrize(
    "status",
    [CandidateStatus.SOURCE_ONLY, CandidateStatus.PARTIAL, CandidateStatus.NOT_APPROVED],
)
async def test_product_round_trip_preserves_candidate_lifecycle_and_decimal(
    supply_engine: AsyncEngine, status: CandidateStatus
) -> None:
    """删除 candidate 状态或用 float 映射 NUMERIC 会破坏产品卡往返。"""

    Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
    tenant_id, product_id = _tenant(), ProductId(new_id("prd"))
    product = _product(tenant_id, product_id, status=status)
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)
    async with Uow(sf, tenant_id) as uow:
        await uow.products.add(tenant_id, product)
        await uow.capabilities.add(
            tenant_id,
            SupplyCapability(
                capability_id=new_id("cap"), tenant_id=tenant_id,
                kind=" Small Batch Custom ", description="Small batch metal work",
                proof_refs=["case-1"],
            ),
        )
    async with Uow(sf, tenant_id) as uow:
        loaded = await uow.products.get(tenant_id, product_id)
        capabilities = await uow.capabilities.list_all(tenant_id)
    assert loaded == product
    assert loaded is not None
    assert loaded.candidate_status is status
    assert loaded.allowed_price_min is not None
    assert loaded.allowed_price_min.amount == Decimal("0.123456789012")
    assert capabilities[0].kind == " Small Batch Custom "


async def test_product_internal_cost_round_trip_requires_basis_unit_and_artifact(
    supply_engine: AsyncEngine,
) -> None:
    """内部成本五项必须全有；金额精度、口径、单位和来源都不能丢。"""

    Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
    tenant_id, product_id = _tenant(), ProductId(new_id("prd"))
    artifact_id = ArtifactId(new_id("art"))
    await _seed_artifact(supply_engine, tenant_id, artifact_id)
    product = _product(tenant_id, product_id)
    product.internal_cost = Money(
        Decimal("0.333333333333"), CurrencyCode("USD")
    )
    product.internal_cost_basis = "supplier_page"
    product.internal_cost_unit = "piece"
    product.internal_cost_source_ref = artifact_id
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)

    async with Uow(sf, tenant_id) as uow:
        await uow.products.add(tenant_id, product)
    async with Uow(sf, tenant_id) as uow:
        loaded = await uow.products.get(tenant_id, product_id)

    assert loaded == product
    assert loaded is not None and loaded.internal_cost is not None
    assert loaded.internal_cost.amount == Decimal("0.333333333333")
    assert loaded.internal_cost_basis == "supplier_page"
    assert loaded.internal_cost_unit == "piece"
    assert loaded.internal_cost_source_ref == artifact_id

    with pytest.raises(ValidationError, match="内部成本"):
        invalid = _product(tenant_id, ProductId(new_id("prd")))
        invalid.internal_cost = Money(Decimal("1.00"), CurrencyCode("USD"))
        invalid.__post_init__()


async def test_candidate_source_is_idempotent_exact_and_rolls_back_as_one_aggregate(
    supply_engine: AsyncEngine,
) -> None:
    """来源幂等键与逐档价格证据必须同产品在一个事务内提交或回滚。"""

    Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
    CandidateSource = _symbol("domains.products.models", "ProductCandidateSource")
    PriceRef = _symbol("domains.products.models", "CandidateIndicativePriceRef")
    tenant_id = _tenant()
    case_id = SourcingCaseId(new_id("src"))
    candidate_id = SupplierCandidateId(new_id("spc"))
    product_id = ProductId(new_id("prd"))
    artifact_id = ArtifactId(new_id("art"))
    await _seed_artifact(supply_engine, tenant_id, artifact_id)
    await _seed_sourcing_candidate(
        supply_engine, tenant_id, case_id, candidate_id, artifact_id
    )
    source = CandidateSource(
        tenant_id=tenant_id,
        product_id=product_id,
        sourcing_case_id=case_id,
        supplier_candidate_id=candidate_id,
        created_at=NOW,
        indicative_prices=(
            PriceRef(
                minimum_quantity=1000,
                unit_amount=Decimal("0.123456789012"),
                currency="USD",
                unit="piece",
                evidence_ref=artifact_id,
            ),
        ),
    )
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)

    async with Uow(sf, tenant_id) as uow:
        await uow.products.add(tenant_id, _product(tenant_id, product_id))
        first = await uow.candidate_sources.add(tenant_id, source)
    async with Uow(sf, tenant_id) as uow:
        second = await uow.candidate_sources.add(tenant_id, source)
        loaded = await uow.candidate_sources.get_by_origin(
            tenant_id, case_id, candidate_id
        )

    assert first == second == loaded == source
    assert loaded.indicative_prices[0].unit_amount == Decimal("0.123456789012")
    assert loaded.indicative_prices[0].evidence_ref == artifact_id
    other_tenant = _tenant()
    async with Uow(sf, other_tenant) as uow:
        assert await uow.candidate_sources.get_by_origin(
            other_tenant, case_id, candidate_id
        ) is None
        with pytest.raises(ValueError, match="租户"):
            await uow.candidate_sources.get_by_origin(
                tenant_id, case_id, candidate_id
            )
    async with supply_engine.connect() as connection:
        source_count = await connection.scalar(
            select(func.count()).select_from(ProductCandidateSourceRow).where(
                ProductCandidateSourceRow.tenant_id == tenant_id,
                ProductCandidateSourceRow.sourcing_case_id == case_id,
                ProductCandidateSourceRow.supplier_candidate_id == candidate_id,
            )
        )
        price_count = await connection.scalar(
            select(func.count()).select_from(ProductCandidatePriceRefRow).where(
                ProductCandidatePriceRefRow.tenant_id == tenant_id,
                ProductCandidatePriceRefRow.product_id == product_id,
            )
        )
    assert source_count == price_count == 1

    rollback_product_id = ProductId(new_id("prd"))
    rollback_source = CandidateSource(
        tenant_id=tenant_id,
        product_id=rollback_product_id,
        sourcing_case_id=SourcingCaseId(new_id("src")),
        supplier_candidate_id=SupplierCandidateId(new_id("spc")),
        created_at=NOW,
        indicative_prices=source.indicative_prices,
    )
    await _seed_sourcing_candidate(
        supply_engine,
        tenant_id,
        rollback_source.sourcing_case_id,
        rollback_source.supplier_candidate_id,
        artifact_id,
    )
    with pytest.raises(RuntimeError, match="rollback aggregate"):
        async with Uow(sf, tenant_id) as uow:
            await uow.products.add(
                tenant_id, _product(tenant_id, rollback_product_id)
            )
            await uow.candidate_sources.add(tenant_id, rollback_source)
            raise RuntimeError("rollback aggregate")
    async with supply_engine.connect() as connection:
        assert await connection.scalar(
            select(func.count()).select_from(ProductRow).where(
                ProductRow.tenant_id == tenant_id,
                ProductRow.product_id == rollback_product_id,
            )
        ) == 0
        assert await connection.scalar(
            select(func.count()).select_from(ProductCandidateSourceRow).where(
                ProductCandidateSourceRow.tenant_id == tenant_id,
                ProductCandidateSourceRow.product_id == rollback_product_id,
            )
        ) == 0


async def test_product_search_normalizes_category_keywords_orders_and_caps_50(
    supply_engine: AsyncEngine,
) -> None:
    """去掉规范化交集、稳定 ID 排序或 50 上限会改变梯子确定性。"""

    Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
    tenant_id = _tenant()
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)
    ids = [ProductId(new_id("prd")) for _ in range(52)]
    async with Uow(sf, tenant_id) as uow:
        for product_id in reversed(ids):
            await uow.products.add(tenant_id, _product(tenant_id, product_id))
    async with Uow(sf, tenant_id) as uow:
        results = await uow.products.search(
            tenant_id, [ProductPool.CANDIDATE], "stainless hinges", ["HINGE"], 999
        )
    assert len(results) == 50
    assert [str(item.product_id) for item in results] == sorted(str(item) for item in ids)[:50]


async def test_supplier_search_and_price_history_are_tenant_bound_stable_and_exact(
    supply_engine: AsyncEngine,
) -> None:
    """移除 tag 规范化/tenant 谓词或 Decimal 直传会泄漏或损坏价格历史。"""

    Uow = _symbol("infra.db.suppliers_uow", "SqlAlchemySuppliersUnitOfWork")
    tenant_a, tenant_b = _tenant(), _tenant()
    supplier_ids = [SupplierId(new_id("sup")) for _ in range(52)]
    artifact_id = ArtifactId(new_id("art"))
    await _seed_artifact(supply_engine, tenant_a, artifact_id)
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)
    async with Uow(sf, tenant_a) as uow:
        for supplier_id in reversed(supplier_ids):
            await uow.suppliers.add(
                tenant_a, _supplier(tenant_a, supplier_id, [" Metal Fabrication ", "OEM"])
            )
        record = SupplierPriceRecord(
            supplier_id=supplier_ids[0], tenant_id=tenant_a,
            product_desc="304 hinge", quantity_tier=1000,
            price=Money(Decimal("0.100000000001"), CurrencyCode("USD")),
            basis="quoted", observed_at=NOW, evidence_ref=str(artifact_id),
            valid_until=NOW + timedelta(days=7),
        )
        await uow.prices.add(tenant_a, record)
    async with Uow(sf, tenant_a) as uow:
        found = await uow.suppliers.search_by_tags(tenant_a, ["metal fabrication"])
        prices = await uow.prices.list_for_supplier(
            tenant_a, supplier_ids[0], "304 hinge"
        )
    assert len(found) == 50
    assert [str(item.supplier_id) for item in found] == sorted(str(item) for item in supplier_ids)[:50]
    assert prices == [record]
    assert prices[0].price.amount == Decimal("0.100000000001")

    async with Uow(sf, tenant_b) as uow:
        assert await uow.suppliers.get(tenant_b, supplier_ids[0]) is None
        with pytest.raises(ValueError, match="租户"):
            await uow.suppliers.get(tenant_a, supplier_ids[0])
        with pytest.raises(ValueError, match="租户"):
            await uow.prices.add(tenant_b, record)


@pytest.mark.parametrize("kind", ["products", "suppliers"])
async def test_supply_uow_rolls_back_on_exception(
    supply_engine: AsyncEngine, kind: str
) -> None:
    """把异常路径改为 commit 会留下半成品供给记录。"""

    tenant_id = _tenant()
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)
    if kind == "products":
        Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
        identifier = ProductId(new_id("prd"))
        with pytest.raises(RuntimeError):
            async with Uow(sf, tenant_id) as uow:
                await uow.products.add(tenant_id, _product(tenant_id, identifier))
                raise RuntimeError("rollback")
        row_type, id_column = ProductRow, ProductRow.product_id
    else:
        Uow = _symbol("infra.db.suppliers_uow", "SqlAlchemySuppliersUnitOfWork")
        identifier = SupplierId(new_id("sup"))
        with pytest.raises(RuntimeError):
            async with Uow(sf, tenant_id) as uow:
                await uow.suppliers.add(
                    tenant_id, _supplier(tenant_id, identifier, ["oem"])
                )
                raise RuntimeError("rollback")
        row_type, id_column = SupplierRow, SupplierRow.supplier_id
    async with supply_engine.connect() as connection:
        count = await connection.scalar(
            select(func.count()).select_from(row_type).where(
                row_type.tenant_id == tenant_id, id_column == identifier
            )
        )
    assert count == 0


@pytest.mark.parametrize("kind", ["products", "suppliers"])
@pytest.mark.parametrize("raise_after_publish", [False, True])
async def test_supply_uow_commits_or_rolls_back_business_and_outbox_together(
    supply_engine: AsyncEngine,
    kind: str,
    raise_after_publish: bool,
) -> None:
    """产品/供应商事实与 outbox 必须共享本域 UoW 的同一事务。"""

    tenant_id = _tenant()
    case_id = SourcingCaseId(new_id("src"))
    sf = async_sessionmaker(supply_engine, expire_on_commit=False)
    if kind == "products":
        Uow = _symbol("infra.db.products_uow", "SqlAlchemyProductsUnitOfWork")
        identifier = ProductId(new_id("prd"))
        row_type, id_column = ProductRow, ProductRow.product_id
    else:
        Uow = _symbol("infra.db.suppliers_uow", "SqlAlchemySuppliersUnitOfWork")
        identifier = SupplierId(new_id("sup"))
        row_type, id_column = SupplierRow, SupplierRow.supplier_id

    async def execute() -> None:
        async with Uow(sf, tenant_id) as uow:
            if kind == "products":
                await uow.products.add(
                    tenant_id, _product(tenant_id, identifier)
                )
            else:
                await uow.suppliers.add(
                    tenant_id, _supplier(tenant_id, identifier, ["oem"])
                )
            await uow.bus.publish(
                SourcingCandidatesReady(
                    tenant_id=tenant_id,
                    occurred_at=NOW,
                    run_id=None,
                    case_id=case_id,
                    option_ids=(),
                    candidate_ids=(),
                )
            )
            if raise_after_publish:
                raise RuntimeError("rollback supply outbox")

    if raise_after_publish:
        with pytest.raises(RuntimeError, match="rollback supply outbox"):
            await execute()
    else:
        await execute()
    async with supply_engine.connect() as connection:
        business_count = await connection.scalar(
            select(func.count()).select_from(row_type).where(
                row_type.tenant_id == tenant_id,
                id_column == identifier,
            )
        )
        outbox_count = await connection.scalar(
            select(func.count()).select_from(OutboxEventRow).where(
                OutboxEventRow.tenant_id == tenant_id,
                OutboxEventRow.event_type == "SourcingCandidatesReady",
            )
        )
    expected = 0 if raise_after_publish else 1
    assert business_count == expected
    assert outbox_count == expected
