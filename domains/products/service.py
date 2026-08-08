"""产品域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.products.models import (
    Product,
    ProductCustomerView,
    ProductInternalView,
    ProductSalesView,
)
from shared.schemas.identifiers import ProductId, TenantId


@runtime_checkable
class ProductService(Protocol):
    """产品服务。

    **视图方法按角色分开**，没有"一个方法加 role 参数"——参数会传错，
    方法签名不会。
    """

    async def search_for_matching(
        self, tenant_id: TenantId, category: str, keywords: list[str]
    ) -> list[Product]:
        """匹配梯子第 1–3 级的查询：正式产品与候选产品中找可能匹配的。

        返回实体（含内部字段）——调用方是寻源流程，属内部视角。
        """
        ...

    async def get_internal_view(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> ProductInternalView: ...

    async def get_sales_view(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> ProductSalesView: ...

    async def get_customer_view(
        self, tenant_id: TenantId, product_id: ProductId
    ) -> ProductCustomerView:
        """客户视图。**API 层对外接口只允许调这个方法**——
        内部视图泄漏给客户是永久性商业损伤。"""
        ...

    async def create_candidate_from_sourcing(
        self, tenant_id: TenantId, sourcing_case_ref: str, candidate_data: dict
    ) -> ProductId:
        """从寻源合格候选生成候选产品卡（SourcingCaseCompleted 处理器调）。

        初始状态 source_only；升级为正式产品需人工逐项确认。
        """
        ...
