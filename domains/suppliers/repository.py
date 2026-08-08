"""供应商域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.suppliers.models import Supplier, SupplierPriceRecord
from shared.schemas.identifiers import SupplierId, TenantId


@runtime_checkable
class SupplierRepository(Protocol):
    async def add(self, supplier: Supplier) -> None: ...

    async def get(
        self, tenant_id: TenantId, supplier_id: SupplierId
    ) -> Supplier | None: ...

    async def update(self, supplier: Supplier) -> None: ...

    async def search_by_tags(
        self, tenant_id: TenantId, tags: list[str]
    ) -> list[Supplier]: ...


@runtime_checkable
class PriceRecordRepository(Protocol):
    async def add(self, record: SupplierPriceRecord) -> None:
        """只增。价格历史是谈判情报，不允许改写。"""
        ...

    async def list_for_supplier(
        self, tenant_id: TenantId, supplier_id: SupplierId, product_desc: str | None
    ) -> list[SupplierPriceRecord]: ...
