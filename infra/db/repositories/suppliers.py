"""供应商档案与只增价格历史的 tenant-bound SQLAlchemy 仓储。"""

from __future__ import annotations

import re
import unicodedata
from typing import cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from domains.suppliers.models import Supplier, SupplierPriceRecord, SupplierVerification
from infra.db.base import TenantScopedRepository
from infra.db.tables import SupplierPriceRecordRow, SupplierRow
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, SupplierId, TenantId, new_id
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


def _supplier_from_row(row: SupplierRow) -> Supplier:
    return Supplier(
        supplier_id=SupplierId(row.supplier_id),
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        created_at=row.created_at,
        region=row.region,
        platform_refs=list(row.platform_refs),
        capability_tags=list(row.capability_tags),
        verification=SupplierVerification(row.verification),
        notes=row.notes,
    )


class SupplierRepositoryImpl(_TenantBoundRepository):
    async def add(self, tenant_id: TenantId, supplier: Supplier) -> None:
        self._require_tenant(tenant_id)
        if supplier.tenant_id != tenant_id:
            raise ValueError("供应商租户与请求租户不一致")
        self._session.add(
            SupplierRow(
                tenant_id=tenant_id,
                supplier_id=supplier.supplier_id,
                name=supplier.name,
                normalized_name=_normalize(supplier.name),
                region=supplier.region,
                platform_refs=list(supplier.platform_refs),
                capability_tags=list(supplier.capability_tags),
                verification=supplier.verification.value,
                notes=supplier.notes,
                created_at=supplier.created_at,
            )
        )

    async def get(
        self, tenant_id: TenantId, supplier_id: SupplierId
    ) -> Supplier | None:
        self._require_tenant(tenant_id)
        row = (
            await self._session.execute(
                self.scoped_query(SupplierRow).where(
                    SupplierRow.supplier_id == supplier_id
                )
            )
        ).scalar_one_or_none()
        return _supplier_from_row(row) if row is not None else None

    async def update(self, tenant_id: TenantId, supplier: Supplier) -> None:
        self._require_tenant(tenant_id)
        if supplier.tenant_id != tenant_id:
            raise ValueError("供应商租户与请求租户不一致")
        result = await self._session.execute(
            update(SupplierRow)
            .where(
                SupplierRow.tenant_id == tenant_id,
                SupplierRow.supplier_id == supplier.supplier_id,
            )
            .values(
                name=supplier.name,
                normalized_name=_normalize(supplier.name),
                region=supplier.region,
                platform_refs=list(supplier.platform_refs),
                capability_tags=list(supplier.capability_tags),
                verification=supplier.verification.value,
                notes=supplier.notes,
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise ValidationError("供应商不存在或租户不匹配")

    async def search_by_tags(
        self, tenant_id: TenantId, tags: list[str]
    ) -> list[Supplier]:
        self._require_tenant(tenant_id)
        requested = {_normalize(tag) for tag in tags if _normalize(tag)}
        if not requested:
            return []
        rows = (
            await self._session.execute(
                self.scoped_query(SupplierRow).order_by(SupplierRow.supplier_id)
            )
        ).scalars()
        results: list[Supplier] = []
        for row in rows:
            normalized = {_normalize(tag) for tag in row.capability_tags}
            if requested.isdisjoint(normalized):
                continue
            results.append(_supplier_from_row(row))
            if len(results) == 50:
                break
        return results


class PriceRecordRepositoryImpl(_TenantBoundRepository):
    async def add(
        self, tenant_id: TenantId, record: SupplierPriceRecord
    ) -> None:
        self._require_tenant(tenant_id)
        if record.tenant_id != tenant_id:
            raise ValueError("供应商价格租户与请求租户不一致")
        await self._session.flush()
        self._session.add(
            SupplierPriceRecordRow(
                tenant_id=tenant_id,
                price_record_id=new_id("spr"),
                supplier_id=record.supplier_id,
                product_desc=record.product_desc,
                quantity_tier=record.quantity_tier,
                unit_amount=record.price.amount,
                currency=record.price.currency,
                basis=record.basis,
                artifact_id=ArtifactId(record.evidence_ref),
                observed_at=record.observed_at,
                valid_until=record.valid_until,
            )
        )

    async def list_for_supplier(
        self,
        tenant_id: TenantId,
        supplier_id: SupplierId,
        product_desc: str | None,
    ) -> list[SupplierPriceRecord]:
        self._require_tenant(tenant_id)
        query = self.scoped_query(SupplierPriceRecordRow).where(
            SupplierPriceRecordRow.supplier_id == supplier_id
        )
        if product_desc is not None:
            query = query.where(SupplierPriceRecordRow.product_desc == product_desc)
        rows = (
            await self._session.execute(
                query.order_by(
                    SupplierPriceRecordRow.observed_at,
                    SupplierPriceRecordRow.price_record_id,
                )
            )
        ).scalars()
        return [
            SupplierPriceRecord(
                supplier_id=SupplierId(row.supplier_id),
                tenant_id=TenantId(row.tenant_id),
                product_desc=row.product_desc,
                quantity_tier=row.quantity_tier,
                price=Money(row.unit_amount, CurrencyCode(row.currency)),
                basis=row.basis,
                observed_at=row.observed_at,
                evidence_ref=ArtifactId(row.artifact_id),
                valid_until=row.valid_until,
            )
            for row in rows
        ]


__all__ = ("PriceRecordRepositoryImpl", "SupplierRepositoryImpl")
