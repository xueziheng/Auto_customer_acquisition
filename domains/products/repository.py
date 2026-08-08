"""产品域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.products.models import Product, ProductPool, SupplyCapability
from shared.schemas.identifiers import ProductId, TenantId


@runtime_checkable
class ProductRepository(Protocol):
    async def add(self, product: Product) -> None: ...

    async def get(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Product | None: ...

    async def update(self, product: Product) -> None: ...

    async def search(
        self,
        tenant_id: TenantId,
        pools: list[ProductPool],
        category: str | None,
        keywords: list[str],
        limit: int,
    ) -> list[Product]: ...


@runtime_checkable
class CapabilityRepository(Protocol):
    async def add(self, capability: SupplyCapability) -> None: ...

    async def list_all(self, tenant_id: TenantId) -> list[SupplyCapability]: ...
