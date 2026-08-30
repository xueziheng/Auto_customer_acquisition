"""产品域实体。（浅域：三池与三视图结构写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    ProductId,
    ProductVariantId,
    SourcingCaseId,
    SupplierCandidateId,
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


class ProductSpecMatchLevel(str, Enum):
    """内部产品事实与 Need 规格的确定性逐项比较结果。"""

    EXACT = "exact"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductSpecFact:
    """产品侧结构化规格事实；缺 Evidence 的旧数据只能产生 unknown finding。"""

    value: str
    evidence_ref: ArtifactId | None


@dataclass(frozen=True)
class ProductSpecRequirement:
    """工作流传入的单项规范化 Need 规格。"""

    spec_name: str
    required: str


@dataclass(frozen=True)
class ProductSpecComparison:
    """产品服务返回的逐项、证据绑定比较，不包含概率或综合分数。"""

    spec_name: str
    required: str
    offered: str | None
    level: ProductSpecMatchLevel
    evidence_ref: ArtifactId | None


@dataclass(frozen=True)
class CandidateIndicativePriceRef:
    """候选产品的单个参考价数量档；金额与 Evidence 引用不可分离。"""

    minimum_quantity: int
    unit_amount: Decimal
    currency: str
    unit: str
    evidence_ref: ArtifactId

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_quantity, bool)
            or not isinstance(self.minimum_quantity, int)
            or self.minimum_quantity < 1
        ):
            raise ValidationError("候选参考价 minimum_quantity 必须为正整数")
        if not isinstance(self.unit_amount, Decimal) or (
            not self.unit_amount.is_finite() or self.unit_amount <= Decimal(0)
        ):
            raise ValidationError("候选参考价 unit_amount 必须为有限正 Decimal")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValidationError("候选参考价 currency 必须为三位大写币种")
        if not self.unit or self.unit != self.unit.strip() or len(self.unit) > 50:
            raise ValidationError("候选参考价 unit 必须非空且不超过 50 字符")
        if not str(self.evidence_ref).strip():
            raise ValidationError("候选参考价必须有 Evidence 引用")


@dataclass(frozen=True)
class ProductCandidateSource:
    """候选产品卡的域内来源聚合；不依赖 sourcing 域内部类型。"""

    tenant_id: TenantId
    product_id: ProductId
    sourcing_case_id: SourcingCaseId
    supplier_candidate_id: SupplierCandidateId
    created_at: datetime
    indicative_prices: tuple[CandidateIndicativePriceRef, ...]

    def __post_init__(self) -> None:
        if not self.indicative_prices:
            raise ValidationError("候选产品来源至少需要一个参考价档")
        minimums = [item.minimum_quantity for item in self.indicative_prices]
        if len(set(minimums)) != len(minimums):
            raise ValidationError("候选产品来源不得重复数量档")


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
    internal_cost_basis: str | None = None
    internal_cost_unit: str | None = None
    internal_cost_source_ref: ArtifactId | None = None
    allowed_price_min: Money | None = None
    allowed_price_max: Money | None = None
    sellable_markets: list[str] = field(default_factory=list)
    customizable: bool = False
    selling_points: list[str] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    source_sourcing_case: str | None = None
    match_specs: dict[str, ProductSpecFact] = field(default_factory=dict)

    def __post_init__(self) -> None:
        cost_parts = (
            self.internal_cost,
            self.internal_cost_basis,
            self.internal_cost_unit,
            self.internal_cost_source_ref,
        )
        if any(item is not None for item in cost_parts) and not all(
            item is not None for item in cost_parts
        ):
            raise ValidationError("内部成本金额、口径、单位和来源必须全有或全无")
        if self.internal_cost_basis is not None and (
            not self.internal_cost_basis.strip()
            or self.internal_cost_basis != self.internal_cost_basis.strip()
            or len(self.internal_cost_basis) > 2_000
        ):
            raise ValidationError("内部成本口径必须非空且不超过 2000 字符")
        if self.internal_cost_unit is not None and (
            not self.internal_cost_unit.strip()
            or self.internal_cost_unit != self.internal_cost_unit.strip()
            or len(self.internal_cost_unit) > 50
        ):
            raise ValidationError("内部成本单位必须非空且不超过 50 字符")


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
    supplier_id: SupplierId | None
    internal_cost: Money | None
    internal_cost_basis: str | None
    internal_cost_unit: str | None
    internal_cost_source_ref: ArtifactId | None
    margin_note: str | None
    known_issues: list[str]
    moq: int | None
    lead_time_display: str | None
    candidate_status: str | None = None
    candidate_source: ProductCandidateSource | None = None


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


@dataclass(frozen=True)
class ProductMatchFinding:
    """现有产品不能自动交接的结构化原因；未知成本绝不补零。"""

    product_id: ProductId
    code: str
    missing_fields: tuple[str, ...]
    spec_comparisons: tuple[ProductSpecComparison, ...] = ()


@dataclass(frozen=True)
class QualifiedProductMatch:
    """满足成本与全部逐项规格证据门禁的内部产品命中。"""

    product: Product
    spec_comparisons: tuple[ProductSpecComparison, ...]


@dataclass(frozen=True)
class ProductMatchResult:
    """内部匹配的确定性结果与被排除项。"""

    qualified_matches: tuple[QualifiedProductMatch, ...]
    findings: tuple[ProductMatchFinding, ...]
