"""供应商域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.suppliers.models import Supplier, SupplierPriceRecord
from shared.schemas.identifiers import SupplierId, TenantId


@runtime_checkable
class SupplierService(Protocol):
    async def register(self, tenant_id: TenantId, supplier: Supplier) -> SupplierId: ...

    async def record_price(
        self, tenant_id: TenantId, record: SupplierPriceRecord
    ) -> None:
        """记录价格。

        basis 为 quoted 时必须有报价证据引用，否则拒绝——
        没有证据的 quoted 等于把参考价洗白成可承诺价（硬边界 7）。
        价格记录只增不改：价格历史本身是谈判情报。
        """
        ...

    async def search_by_capability(
        self, tenant_id: TenantId, capability_tags: list[str]
    ) -> list[Supplier]:
        """匹配梯子第 4–5 级的查询：现有供应商里找能供或能定制的。"""
        ...

    async def get(self, tenant_id: TenantId, supplier_id: SupplierId) -> Supplier: ...
