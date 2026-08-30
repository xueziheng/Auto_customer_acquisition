"""供应商域实体。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import ArtifactId, SupplierId, TenantId
from shared.schemas.money import Money


class SupplierVerification(str, Enum):
    UNVERIFIED = "unverified"
    BASIC_CHECKED = "basic_checked"
    """基础核验：营业信息、平台资质。"""

    TRANSACTED = "transacted"
    """有过实际交易。最高可信级别——真实交易是唯一可靠的验证。"""


@dataclass
class Supplier:
    """供应商。"""

    supplier_id: SupplierId
    tenant_id: TenantId
    name: str
    created_at: datetime
    region: str | None = None
    platform_refs: list[str] = field(default_factory=list)
    capability_tags: list[str] = field(default_factory=list)
    verification: SupplierVerification = SupplierVerification.UNVERIFIED
    notes: str | None = None


@dataclass(frozen=True)
class SupplierPriceRecord:
    """供应商价格记录。

    字段：
        supplier_id, product_desc
        quantity_tier, price
        basis:        indicative | quoted —— 升级为 quoted 必须有
                      供应商报价证据（evidence_ref）
        currency 由 Money 携带
        observed_at, evidence_ref
        valid_until:  quoted 价格的有效期
    """

    supplier_id: SupplierId
    tenant_id: TenantId
    product_desc: str
    quantity_tier: int
    price: Money
    basis: str
    observed_at: datetime
    evidence_ref: ArtifactId
    valid_until: datetime | None = None
