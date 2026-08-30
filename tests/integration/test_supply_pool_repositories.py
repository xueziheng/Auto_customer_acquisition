"""产品与供应商供给池仓储合同。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from infra.db.tables import ProductRow, SupplierRow
from shared.schemas.identifiers import (
    ArtifactId,
    ProductId,
    SupplierId,
    TenantId,
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
