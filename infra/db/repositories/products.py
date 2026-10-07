"""产品与供应能力池的 tenant-bound SQLAlchemy 仓储。"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from domains.products.models import (
    CandidateIndicativePriceRef,
    CandidateStatus,
    Product,
    ProductCandidateSource,
    ProductPool,
    ProductSpecFact,
    SupplyCapability,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    ProductCandidatePriceRefRow,
    ProductCandidateSourceRow,
    ProductMatchSpecRow,
    ProductRow,
    SupplyCapabilityRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    ProductId,
    SourcingCaseId,
    SupplierCandidateId,
    SupplierId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

_SPACE_RE = re.compile(r"\s+")


def _normalize(value: str) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", value).strip()).casefold()


class _TenantBoundRepository(TenantScopedRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _require_tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise ValueError("请求租户与仓储绑定租户不一致")


def _money(amount: Decimal | None, currency: str | None) -> Money | None:
    if amount is None and currency is None:
        return None
    if amount is None or currency is None:
        raise ValidationError("金额与币种持久列不完整")
    return Money(amount, CurrencyCode(currency))


def _product_to_row(product: Product) -> ProductRow:
    product.__post_init__()
    return ProductRow(
        tenant_id=product.tenant_id,
        product_id=product.product_id,
        pool=product.pool.value,
        candidate_status=(
            product.candidate_status.value
            if product.candidate_status is not None
            else None
        ),
        name_zh=product.name_zh,
        name_en=product.name_en,
        category=product.category,
        normalized_category=_normalize(product.category),
        spec_summary=product.spec_summary,
        moq=product.moq,
        lead_time_days_min=product.lead_time_days_min,
        lead_time_days_max=product.lead_time_days_max,
        supplier_id=product.supplier_id,
        internal_cost_amount=(
            product.internal_cost.amount if product.internal_cost else None
        ),
        internal_cost_currency=(
            product.internal_cost.currency if product.internal_cost else None
        ),
        internal_cost_basis=product.internal_cost_basis,
        internal_cost_unit=product.internal_cost_unit,
        internal_cost_source_ref=product.internal_cost_source_ref,
        allowed_price_min_amount=(
            product.allowed_price_min.amount if product.allowed_price_min else None
        ),
        allowed_price_min_currency=(
            product.allowed_price_min.currency if product.allowed_price_min else None
        ),
        allowed_price_max_amount=(
            product.allowed_price_max.amount if product.allowed_price_max else None
        ),
        allowed_price_max_currency=(
            product.allowed_price_max.currency if product.allowed_price_max else None
        ),
        sellable_markets=list(product.sellable_markets),
        customizable=product.customizable,
        selling_points=list(product.selling_points),
        known_issues=list(product.known_issues),
        created_at=product.created_at,
    )


async def _row_to_product(session: AsyncSession, row: ProductRow) -> Product:
    source_case = await session.scalar(
        select(ProductCandidateSourceRow.sourcing_case_id).where(
            ProductCandidateSourceRow.tenant_id == row.tenant_id,
            ProductCandidateSourceRow.product_id == row.product_id,
        )
    )
    spec_rows = list(
        (
            await session.execute(
                select(ProductMatchSpecRow)
                .where(
                    ProductMatchSpecRow.tenant_id == row.tenant_id,
                    ProductMatchSpecRow.product_id == row.product_id,
                )
                .order_by(ProductMatchSpecRow.normalized_spec_name)
            )
        ).scalars()
    )
    return Product(
        product_id=ProductId(row.product_id),
        tenant_id=TenantId(row.tenant_id),
        pool=ProductPool(row.pool),
        name_zh=row.name_zh,
        name_en=row.name_en,
        category=row.category,
        created_at=row.created_at,
        candidate_status=(
            CandidateStatus(row.candidate_status) if row.candidate_status else None
        ),
        spec_summary=row.spec_summary,
        match_specs={
            item.normalized_spec_name: ProductSpecFact(
                value=item.value,
                evidence_ref=ArtifactId(item.evidence_ref),
            )
            for item in spec_rows
        },
        moq=row.moq,
        lead_time_days_min=row.lead_time_days_min,
        lead_time_days_max=row.lead_time_days_max,
        supplier_id=SupplierId(row.supplier_id) if row.supplier_id else None,
        internal_cost=_money(row.internal_cost_amount, row.internal_cost_currency),
        internal_cost_basis=row.internal_cost_basis,
        internal_cost_unit=row.internal_cost_unit,
        internal_cost_source_ref=(
            ArtifactId(row.internal_cost_source_ref)
            if row.internal_cost_source_ref
            else None
        ),
        allowed_price_min=_money(
            row.allowed_price_min_amount, row.allowed_price_min_currency
        ),
        allowed_price_max=_money(
            row.allowed_price_max_amount, row.allowed_price_max_currency
        ),
        sellable_markets=list(row.sellable_markets),
        customizable=row.customizable,
        selling_points=list(row.selling_points),
        known_issues=list(row.known_issues),
        source_sourcing_case=cast(str | None, source_case),
    )


class ProductRepositoryImpl(_TenantBoundRepository):
    @staticmethod
    def _match_spec_rows(product: Product) -> list[ProductMatchSpecRow]:
        rows: list[ProductMatchSpecRow] = []
        names: set[str] = set()
        for raw_name, fact in sorted(product.match_specs.items()):
            name = _normalize(raw_name)
            if (
                not name
                or len(name) > 100
                or name in names
                or not isinstance(fact, ProductSpecFact)
                or not isinstance(fact.value, str)
                or not fact.value.strip()
                or fact.evidence_ref is None
            ):
                raise ValidationError(
                    "产品匹配规格事实必须规范化后唯一、非空并绑定 Evidence"
                )
            names.add(name)
            rows.append(
                ProductMatchSpecRow(
                    tenant_id=product.tenant_id,
                    product_id=product.product_id,
                    normalized_spec_name=name,
                    value=fact.value,
                    evidence_ref=fact.evidence_ref,
                )
            )
        return rows

    async def add(self, tenant_id: TenantId, product: Product) -> None:
        self._require_tenant(tenant_id)
        if product.tenant_id != tenant_id:
            raise ValueError("产品租户与请求租户不一致")
        match_rows = self._match_spec_rows(product)
        self._session.add(_product_to_row(product))
        await self._session.flush()
        self._session.add_all(match_rows)

    async def get(self, tenant_id: TenantId, product_id: ProductId) -> Product | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(ProductRow).where(ProductRow.product_id == product_id)
            )
        ).scalar_one_or_none()
        return await _row_to_product(self._session, row) if row is not None else None

    async def update(self, tenant_id: TenantId, product: Product) -> None:
        self._require_tenant(tenant_id)
        if product.tenant_id != tenant_id:
            raise ValueError("产品租户与请求租户不一致")
        row = _product_to_row(product)
        result = await self._session.execute(
            update(ProductRow)
            .where(
                ProductRow.tenant_id == tenant_id,
                ProductRow.product_id == product.product_id,
            )
            .values(
                **{
                    column.name: getattr(row, column.name)
                    for column in ProductRow.__table__.columns
                    if column.name not in {"tenant_id", "product_id", "created_at"}
                }
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise ValidationError("产品不存在或租户不匹配")
        await self._session.execute(
            delete(ProductMatchSpecRow).where(
                ProductMatchSpecRow.tenant_id == tenant_id,
                ProductMatchSpecRow.product_id == product.product_id,
            )
        )
        self._session.add_all(self._match_spec_rows(product))

    async def search(
        self,
        tenant_id: TenantId,
        pools: list[ProductPool],
        category: str | None,
        keywords: list[str],
        limit: int,
    ) -> list[Product]:
        self._require_tenant(tenant_id)
        if not pools or limit <= 0:
            return []
        query = self.scoped_query(ProductRow).where(
            ProductRow.pool.in_([pool.value for pool in pools])
        )
        if category is not None:
            query = query.where(ProductRow.normalized_category == _normalize(category))
        rows = (
            await self._session.execute(query.order_by(ProductRow.product_id))
        ).scalars()
        normalized_keywords = [
            _normalize(item) for item in keywords if _normalize(item)
        ]
        results: list[Product] = []
        cap = min(limit, 50)
        for row in rows:
            haystack = _normalize(
                " ".join([row.name_zh, row.name_en, row.category, *row.selling_points])
            )
            if normalized_keywords and not any(
                keyword in haystack for keyword in normalized_keywords
            ):
                continue
            results.append(await _row_to_product(self._session, row))
            if len(results) == cap:
                break
        return results


class CapabilityRepositoryImpl(_TenantBoundRepository):
    async def add(self, tenant_id: TenantId, capability: SupplyCapability) -> None:
        self._require_tenant(tenant_id)
        if capability.tenant_id != tenant_id:
            raise ValueError("供应能力租户与请求租户不一致")
        self._session.add(
            SupplyCapabilityRow(
                tenant_id=tenant_id,
                capability_id=capability.capability_id,
                kind=capability.kind,
                normalized_kind=_normalize(capability.kind),
                description=capability.description,
                proof_refs=list(capability.proof_refs),
            )
        )

    async def list_all(self, tenant_id: TenantId) -> list[SupplyCapability]:
        self._require_tenant(tenant_id)
        rows = (
            await self._session.execute(
                self.scoped_query(SupplyCapabilityRow).order_by(
                    SupplyCapabilityRow.capability_id
                )
            )
        ).scalars()
        return [
            SupplyCapability(
                capability_id=row.capability_id,
                tenant_id=TenantId(row.tenant_id),
                kind=row.kind,
                description=row.description,
                proof_refs=list(row.proof_refs),
            )
            for row in rows
        ]


def _candidate_source_from_rows(
    source: ProductCandidateSourceRow,
    prices: list[ProductCandidatePriceRefRow],
) -> ProductCandidateSource:
    return ProductCandidateSource(
        tenant_id=TenantId(source.tenant_id),
        product_id=ProductId(source.product_id),
        sourcing_case_id=SourcingCaseId(source.sourcing_case_id),
        supplier_candidate_id=SupplierCandidateId(source.supplier_candidate_id),
        created_at=source.created_at,
        indicative_prices=tuple(
            CandidateIndicativePriceRef(
                minimum_quantity=row.minimum_quantity,
                unit_amount=row.unit_amount,
                currency=row.currency,
                unit=row.unit,
                evidence_ref=ArtifactId(row.artifact_id),
            )
            for row in prices
        ),
    )


class ProductCandidateSourceRepositoryImpl(_TenantBoundRepository):
    """以同租户 Case+Candidate 为幂等键保存候选产品来源聚合。"""

    async def get_by_origin(
        self,
        tenant_id: TenantId,
        sourcing_case_id: SourcingCaseId,
        supplier_candidate_id: SupplierCandidateId,
    ) -> ProductCandidateSource | None:
        self._require_tenant(tenant_id)
        source = (
            await self._session.execute(
                self.scoped_query(ProductCandidateSourceRow).where(
                    ProductCandidateSourceRow.sourcing_case_id == sourcing_case_id,
                    ProductCandidateSourceRow.supplier_candidate_id
                    == supplier_candidate_id,
                )
            )
        ).scalar_one_or_none()
        if source is None:
            return None
        prices = list(
            (
                await self._session.execute(
                    self.scoped_query(ProductCandidatePriceRefRow)
                    .where(ProductCandidatePriceRefRow.product_id == source.product_id)
                    .order_by(
                        ProductCandidatePriceRefRow.minimum_quantity,
                        ProductCandidatePriceRefRow.artifact_id,
                    )
                )
            ).scalars()
        )
        if not prices:
            raise ValidationError("候选产品来源缺少逐档价格 Evidence")
        return _candidate_source_from_rows(source, prices)

    async def get_by_product(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> ProductCandidateSource | None:
        self._require_tenant(tenant_id)
        source = (
            await self._session.execute(
                self.scoped_query(ProductCandidateSourceRow).where(
                    ProductCandidateSourceRow.product_id == product_id
                )
            )
        ).scalar_one_or_none()
        if source is None:
            return None
        prices = list(
            (
                await self._session.execute(
                    self.scoped_query(ProductCandidatePriceRefRow)
                    .where(ProductCandidatePriceRefRow.product_id == product_id)
                    .order_by(
                        ProductCandidatePriceRefRow.minimum_quantity,
                        ProductCandidatePriceRefRow.artifact_id,
                    )
                )
            ).scalars()
        )
        if not prices:
            raise ValidationError("候选产品来源缺少逐档价格 Evidence")
        return _candidate_source_from_rows(source, prices)

    async def add(
        self, tenant_id: TenantId, source: ProductCandidateSource
    ) -> ProductCandidateSource:
        self._require_tenant(tenant_id)
        if source.tenant_id != tenant_id:
            raise ValueError("候选产品来源租户与请求租户不一致")
        await self._session.flush()
        inserted_product_id = (
            await self._session.execute(
                pg_insert(ProductCandidateSourceRow)
                .values(
                    tenant_id=tenant_id,
                    product_id=source.product_id,
                    sourcing_case_id=source.sourcing_case_id,
                    supplier_candidate_id=source.supplier_candidate_id,
                    created_at=source.created_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_product_candidate_sources_origin"
                )
                .returning(ProductCandidateSourceRow.product_id)
            )
        ).scalar_one_or_none()
        if inserted_product_id is None:
            existing = await self.get_by_origin(
                tenant_id,
                source.sourcing_case_id,
                source.supplier_candidate_id,
            )
            if existing is None:
                raise ValidationError("候选产品来源幂等写入未返回现有聚合")
            return existing
        self._session.add_all(
            [
                ProductCandidatePriceRefRow(
                    tenant_id=tenant_id,
                    product_id=source.product_id,
                    minimum_quantity=price.minimum_quantity,
                    unit_amount=price.unit_amount,
                    currency=price.currency,
                    unit=price.unit,
                    artifact_id=price.evidence_ref,
                )
                for price in source.indicative_prices
            ]
        )
        await self._session.flush()
        return source


__all__ = (
    "CapabilityRepositoryImpl",
    "ProductCandidateSourceRepositoryImpl",
    "ProductRepositoryImpl",
)
