"""产品域对外 DTO。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import (
    ArtifactId,
    ProductId,
    SourcingCaseId,
    SupplierCandidateId,
)
from shared.schemas.money import WireDecimal


def _bounded_text(value: str, field_name: str, maximum: int) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field_name} 必须非空、无首尾空白且不超过 {maximum} 字符")
    return value


def _require_numeric_28_12(value: Decimal) -> None:
    """拒绝数据库会舍入或溢出的值；保留调用方原始 Decimal。"""

    exponent = cast(int, value.as_tuple().exponent)
    scale = max(-exponent, 0)
    integer_digits = max(value.adjusted() + 1, 0)
    if scale > 12 or integer_digits > 16:
        raise ValueError("unit_amount 必须可精确表示为 NUMERIC(28,12)")


class CandidateIndicativePriceRef(BaseModel):
    """候选产品的公开参考价；Artifact 是原页证据而不是搜索摘要。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    minimum_quantity: int = Field(ge=1)
    unit_amount: WireDecimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit: str = Field(min_length=1, max_length=50)
    evidence_ref: ArtifactId

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        """金额、单位和证据定位必须同时可用。"""

        if not self.unit_amount.is_finite() or self.unit_amount <= Decimal(0):
            raise ValueError("unit_amount 必须是有限正 Decimal")
        _require_numeric_28_12(self.unit_amount)
        _bounded_text(self.unit, "unit", 50)
        _bounded_text(str(self.evidence_ref), "evidence_ref", 200)
        return self


class CandidateProductCreate(BaseModel):
    """由合格 Sourcing Candidate 生成 source_only 产品卡的强类型命令。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    sourcing_case_id: SourcingCaseId
    supplier_candidate_id: SupplierCandidateId
    name_zh: str = Field(min_length=1, max_length=300)
    name_en: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    spec_summary: str = Field(min_length=1, max_length=4_000)
    moq: int = Field(ge=1)
    evidence_refs: tuple[ArtifactId, ...] = Field(min_length=1)
    indicative_prices: tuple[CandidateIndicativePriceRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_evidence_and_tiers(self) -> Self:
        """命令必须可回溯，且每个参考价都绑定已声明的原页 Artifact。"""

        for field_name, value, maximum in (
            ("sourcing_case_id", str(self.sourcing_case_id), 200),
            ("supplier_candidate_id", str(self.supplier_candidate_id), 200),
            ("name_zh", self.name_zh, 300),
            ("name_en", self.name_en, 300),
            ("category", self.category, 100),
            ("spec_summary", self.spec_summary, 4_000),
        ):
            _bounded_text(value, field_name, maximum)
        evidence = tuple(str(item) for item in self.evidence_refs)
        if any(not item or item != item.strip() for item in evidence):
            raise ValueError("evidence_refs 不得包含空引用")
        if len(set(evidence)) != len(evidence):
            raise ValueError("evidence_refs 不得重复")
        evidence_set = set(self.evidence_refs)
        if any(
            item.evidence_ref not in evidence_set for item in self.indicative_prices
        ):
            raise ValueError("每个参考价 Evidence 必须包含在 evidence_refs 中")
        minimums = [item.minimum_quantity for item in self.indicative_prices]
        if len(set(minimums)) != len(minimums):
            raise ValueError("indicative_prices 不得重复数量档")
        return self


class ProductSupplySourceView(BaseModel):
    """source_only 产品卡的寻源来源链；没有供应商联系人、成本或客户报价。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    sourcing_case_id: SourcingCaseId
    supplier_candidate_id: SupplierCandidateId
    evidence_refs: tuple[ArtifactId, ...] = Field(min_length=1)
    indicative_prices: tuple[CandidateIndicativePriceRef, ...] = Field(min_length=1)
    price_basis: Literal["indicative"] = "indicative"


class ProductSupplyCardView(BaseModel):
    """供应中心安全卡片；没有 supplier、成本、客户报价或联系入口。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    product_id: ProductId
    pool: str = Field(min_length=1, max_length=40)
    candidate_status: str | None = Field(default=None, max_length=40)
    name_zh: str = Field(min_length=1, max_length=300)
    name_en: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    spec_summary: str | None = Field(default=None, max_length=4_000)
    moq: int | None = Field(default=None, ge=1)
    lead_time_display: str | None = Field(default=None, max_length=50)
    source_only: bool
    quote_warning: Literal["不可用于客户报价"] | None = None
    source: ProductSupplySourceView | None = None

    @model_validator(mode="after")
    def validate_source_only(self) -> Self:
        """source_only 必须有完整来源链并永远携带不可报价提示。"""

        if self.source_only != (self.candidate_status == "source_only"):
            raise ValueError("source_only 与 candidate_status 不一致")
        if self.source_only and (
            self.source is None or self.quote_warning != "不可用于客户报价"
        ):
            raise ValueError("source_only 产品卡必须有来源链和不可报价提示")
        if not self.source_only and (self.source is not None or self.quote_warning):
            raise ValueError("非 source_only 产品卡不得伪装为寻源候选")
        return self


__all__ = (
    "CandidateIndicativePriceRef",
    "CandidateProductCreate",
    "ProductSupplyCardView",
    "ProductSupplySourceView",
)
