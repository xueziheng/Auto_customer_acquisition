"""寻源域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shared.schemas.money import Money


@dataclass(frozen=True)
class SpecComparisonView:
    spec_name: str
    required: str
    offered: str | None
    level: str
    substitutable: bool | None = None
    substitution_impact: str | None = None
    needs_customer_confirmation: bool = False


@dataclass(frozen=True)
class CandidateSubmission:
    """提交候选的入参。

    ``evidence_url`` / ``evidence_hash`` / ``evidence_artifact_ref``
    三项必填——证据缺失的候选直接拒绝。
    """

    supplier_name: str
    product_title: str
    source_platform: str | None
    specs: list[SpecComparisonView]
    quoted_prices: dict[int, Money]
    moq: int | None
    price_unit: str | None
    currency: str | None
    evidence_url: str
    evidence_hash: str
    evidence_artifact_ref: str
    verified_by: str | None = None


@dataclass(frozen=True)
class CandidateView:
    candidate_id: str
    supplier_name: str
    product_title: str
    source_platform: str | None
    specs: list[SpecComparisonView]
    quoted_prices: dict[int, Money]
    match_summary: str | None
    moq: int | None = None
    price_unit: str | None = None
    currency: str | None = None
    rejected: bool = False
    rejection_reasons: list[str] = field(default_factory=list)
    evidence_url: str | None = None
    price_basis: str = "indicative"
    """恒为 indicative——提醒所有消费方这些价格不能进报价。"""


@dataclass(frozen=True)
class CaseView:
    case_id: str
    need_id: str
    state: str
    opened_at: datetime
    ladder_checked_to: int | None = None
    candidates: list[CandidateView] = field(default_factory=list)
    qualified_count: int = 0
    assigned_to_name: str | None = None
    failed_reason: str | None = None
    completed_at: datetime | None = None
