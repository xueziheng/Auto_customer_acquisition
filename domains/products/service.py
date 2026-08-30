"""产品域服务——本域面向工作流与 API 的公共契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.products.models import (
    CandidateStatus,
    Product,
    ProductCustomerView,
    ProductInternalView,
    ProductMatchResult,
    ProductPool,
    ProductSalesView,
    ProductSpecComparison,
    ProductSpecFact,
    ProductSpecMatchLevel,
    ProductSpecRequirement,
    QualifiedProductMatch,
)
from domains.products.permissions import ProductActor
from domains.products.schemas import CandidateProductCreate
from shared.schemas.identifiers import ProductId, TenantId


@runtime_checkable
class ProductService(Protocol):
    """产品服务；每个入口均以可信 actor 显式授权。"""

    async def search_for_matching(
        self,
        tenant_id: TenantId,
        category: str,
        keywords: list[str],
        required_specs: tuple[ProductSpecRequirement, ...],
        *,
        actor: ProductActor,
    ) -> ProductMatchResult:
        """匹配梯子 1–3；缺完整成本五元组只返回 finding。"""
        ...

    async def get_internal_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductInternalView: ...

    async def get_sales_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductSalesView: ...

    async def get_customer_view(
        self, tenant_id: TenantId, product_id: ProductId, *, actor: ProductActor
    ) -> ProductCustomerView:
        """客户视图。**API 层对外接口只允许调这个方法**——
        内部视图泄漏给客户是永久性商业损伤。"""
        ...

    async def create_candidate_from_sourcing(
        self,
        tenant_id: TenantId,
        command: CandidateProductCreate,
        *,
        actor: ProductActor,
    ) -> ProductId:
        """以 Case+Candidate 幂等生成 source_only 产品卡。"""
        ...


__all__ = (
    "CandidateStatus",
    "Product",
    "ProductActor",
    "ProductCustomerView",
    "ProductInternalView",
    "ProductMatchResult",
    "ProductPool",
    "ProductSalesView",
    "ProductService",
    "ProductSpecComparison",
    "ProductSpecFact",
    "ProductSpecMatchLevel",
    "ProductSpecRequirement",
    "QualifiedProductMatch",
)
