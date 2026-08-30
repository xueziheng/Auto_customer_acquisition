"""产品与供应能力池的 tenant-bound SQLAlchemy 仓储。"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domains.products.models import (
    CandidateStatus,
    Product,
    ProductPool,
    SupplyCapability,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import ProductCandidateSourceRow, ProductRow, SupplyCapabilityRow
from shared.errors import ValidationError
from shared.schemas.identifiers import ProductId, SupplierId, TenantId
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
    if product.internal_cost is not None:
        raise ValidationError("内部成本缺少 basis 与来源时不得持久化或补零")
    return ProductRow(
        tenant_id=product.tenant_id,
        product_id=product.product_id,
        pool=product.pool.value,
        candidate_status=(
            product.candidate_status.value if product.candidate_status is not None else None
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
        internal_cost_amount=None,
        internal_cost_currency=None,
        internal_cost_basis=None,
        internal_cost_source_ref=None,
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
        moq=row.moq,
        lead_time_days_min=row.lead_time_days_min,
        lead_time_days_max=row.lead_time_days_max,
        supplier_id=SupplierId(row.supplier_id) if row.supplier_id else None,
        internal_cost=_money(row.internal_cost_amount, row.internal_cost_currency),
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
    async def add(self, tenant_id: TenantId, product: Product) -> None:
        self._require_tenant(tenant_id)
        if product.tenant_id != tenant_id:
            raise ValueError("产品租户与请求租户不一致")
        self._session.add(_product_to_row(product))

    async def get(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Product | None:
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
        normalized_keywords = [_normalize(item) for item in keywords if _normalize(item)]
        results: list[Product] = []
        cap = min(limit, 50)
        for row in rows:
            haystack = _normalize(
                " ".join(
                    [row.name_zh, row.name_en, row.category, *row.selling_points]
                )
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
    async def add(
        self, tenant_id: TenantId, capability: SupplyCapability
    ) -> None:
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


__all__ = ("CapabilityRepositoryImpl", "ProductRepositoryImpl")
