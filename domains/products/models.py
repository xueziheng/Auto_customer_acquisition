"""产品域实体。（浅域：三池与三视图结构写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.schemas.identifiers import (
    ProductId,
    ProductVariantId,
    SupplierId,
    TenantId,
)
from shared.schemas.money import Money


class ProductPool(str, Enum):
    """三个供应池。"""

    FORMAL = "formal"
    """正式产品：全部信息确认。匹配梯子第 1–2 级的查询范围。"""

    CANDIDATE = "candidate"
    """候选产品：来自历史寻源，未完全验证。匹配梯子第 3 级。"""

    CAPABILITY = "capability"
    """供应能力：无固定 SKU 的能力（加工、OEM、整合）。
    贸易公司真正的资产常常是这个，不是某个 SKU。"""


class CandidateStatus(str, Enum):
    SOURCE_ONLY = "source_only"
    PARTIAL = "partial"
    NOT_APPROVED = "not_approved"


@dataclass
class Product:
    """产品（正式或候选，按 pool 区分）。

    注意 ``supplier_id`` 与 ``internal_cost`` 是**内部字段**——
    它们只出现在内部视图，销售视图和客户视图的 DTO 里没有这两个
    字段的位置（结构性防泄漏：字段不存在就不可能被序列化出去）。
    """

    product_id: ProductId
    tenant_id: TenantId
    pool: ProductPool
    name_zh: str
    name_en: str
    category: str
    created_at: datetime
    candidate_status: CandidateStatus | None = None
    spec_summary: str | None = None
    moq: int | None = None
    lead_time_days_min: int | None = None
    lead_time_days_max: int | None = None
    supplier_id: SupplierId | None = None
    internal_cost: Money | None = None
    allowed_price_min: Money | None = None
    allowed_price_max: Money | None = None
    sellable_markets: list[str] = field(default_factory=list)
    customizable: bool = False
    selling_points: list[str] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    source_sourcing_case: str | None = None


@dataclass
class ProductVariant:
    """SKU / 规格变体。"""

    variant_id: ProductVariantId
    tenant_id: TenantId
    product_id: ProductId
    sku: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class SupplyCapability:
    """供应能力（第三池）。

    字段：
        tenant_id, capability_id
        kind:         metal_fabrication / injection_molding / packaging /
                      oem / small_batch_custom / supplier_sourcing / …
        description
        proof_refs:   佐证（做过的案例、供应商网络说明）
    """

    capability_id: str
    tenant_id: TenantId
    kind: str
    description: str
    proof_refs: list[str] = field(default_factory=list)


# --- 三视图 DTO：权限边界的结构化形式 ------------------------------------
# 三个独立类而不是一个类加过滤参数：字段不存在就不可能被序列化出去。
# 内部成本泄漏给客户是永久性商业损伤——以后每次谈判都从你的底价开始。


@dataclass(frozen=True)
class ProductInternalView:
    """内部视图：boss / product / sourcing / finance。"""

    product_id: str
    pool: str
    name_zh: str
    name_en: str
    category: str
    supplier_name: str | None
    internal_cost: Money | None
    margin_note: str | None
    known_issues: list[str]
    moq: int | None
    lead_time_display: str | None
    candidate_status: str | None = None


@dataclass(frozen=True)
class ProductSalesView:
    """销售视图。没有供应商、没有成本字段——不是隐藏，是不存在。"""

    product_id: str
    name_zh: str
    name_en: str
    category: str
    selling_points: list[str]
    allowed_price_min: Money | None
    allowed_price_max: Money | None
    moq: int | None
    lead_time_display: str | None
    faq: list[str] = field(default_factory=list)
    sendable_image_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProductCustomerView:
    """客户视图。更少：无价格范围（价格走报价流程），无 MOQ 细节。"""

    product_id: str
    name_en: str
    category: str
    spec_display: str | None
    variant_options: list[str] = field(default_factory=list)
    description_en: str | None = None
    image_refs: list[str] = field(default_factory=list)
    inquiry_enabled: bool = True
    sample_request_enabled: bool = True
