"""产品域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.products.models import (
    Product,
    ProductCandidateSource,
    ProductPool,
    SupplyCapability,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    ProductId,
    SourcingCaseId,
    SupplierCandidateId,
    TenantId,
)


@runtime_checkable
class ProductRepository(Protocol):
    async def add(self, tenant_id: TenantId, product: Product) -> None: ...

    async def get(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> Product | None: ...

    async def update(self, tenant_id: TenantId, product: Product) -> None: ...

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
    async def add(self, tenant_id: TenantId, capability: SupplyCapability) -> None: ...

    async def list_all(self, tenant_id: TenantId) -> list[SupplyCapability]: ...


@runtime_checkable
class ProductCandidateSourceRepository(Protocol):
    """候选产品来源与逐档 Evidence 价格的幂等聚合仓储。"""

    async def add(
        self, tenant_id: TenantId, source: ProductCandidateSource
    ) -> ProductCandidateSource: ...

    async def get_by_origin(
        self,
        tenant_id: TenantId,
        sourcing_case_id: SourcingCaseId,
        supplier_candidate_id: SupplierCandidateId,
    ) -> ProductCandidateSource | None: ...

    async def get_by_product(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> ProductCandidateSource | None:
        """按同租户产品读取来源；只供内部视图显示原始 Evidence。"""
        ...


@runtime_checkable
class ProductsUnitOfWork(Protocol):
    """产品聚合与 Outbox 共事务边界。"""

    products: ProductRepository
    capabilities: CapabilityRepository
    candidate_sources: ProductCandidateSourceRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
