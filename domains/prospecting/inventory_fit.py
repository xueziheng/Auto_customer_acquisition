"""企业任务私有适配推断；公开证据版本绑定，不授予客户需求或触达许可。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .inventory import InventoryQuery, PublicCandidate

MAX_FIT_BATCHES = 10
MAX_FIT_CANDIDATES = 10
MAX_FIT_EVIDENCE_BYTES = 24000


def candidate_evidence_version(candidate: PublicCandidate) -> str:
    """绑定整个公开候选快照；任何证据、时间、来源或身份变化均须重评。"""
    return hashlib.sha256(
        json.dumps(
            candidate.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def fit_query_fingerprint(query: InventoryQuery) -> str:
    """固定企业任务的私有目标与精确资料引用，不让修改后的目标沿用旧推断。"""
    return hashlib.sha256(
        json.dumps(
            query.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def fit_evidence_inputs(
    candidates: tuple[PublicCandidate, ...],
) -> list[dict[str, str]]:
    """显式公开证据片段，模型不得声称片段之外的能力；总字节由调用方限制。"""
    return [
        {
            "candidate_id": str(c.id),
            "name": c.name,
            "country": c.country,
            "source_url": c.source_url,
            "evidence_text": c.evidence_text[:4000],
        }
        for c in candidates
    ]


class FitCandidateVersion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=40)
    evidence_version: str = Field(pattern=r"^[a-f0-9]{64}$")


class InventoryFitAssessment(FitCandidateVersion):
    """模型适配推断及其公开引文；不能冒充客户明确表达的需求。"""

    matched: bool = Field(strict=True)
    evidence_quote: str = Field(min_length=1, max_length=4000)
    inference: str = Field(min_length=1, max_length=2000)
    source_type: Literal["agent_inference"] = "agent_inference"
    extracted_by: str = Field(min_length=1, max_length=200)
    extracted_at: AwareDatetime


class InventoryFitBatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    query_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    sequence: int = Field(ge=0, lt=MAX_FIT_BATCHES)
    state: Literal["started", "complete"]
    candidates: tuple[FitCandidateVersion, ...] = Field(
        min_length=1, max_length=MAX_FIT_CANDIDATES
    )
    assessments: tuple[InventoryFitAssessment, ...] = ()


def selected_fit_candidates(
    candidates: tuple[PublicCandidate, ...],
    batches: tuple[InventoryFitBatch, ...],
    query: InventoryQuery,
) -> tuple[PublicCandidate, ...]:
    """仅本任务完成的推断可交付，且公共证据须仍为评估时的完整版本。"""
    selected = {
        (a.candidate_id, a.evidence_version)
        for b in batches
        if b.state == "complete" and b.query_fingerprint == fit_query_fingerprint(query)
        for a in b.assessments
        if a.matched
    }
    return tuple(
        c for c in candidates if (str(c.id), candidate_evidence_version(c)) in selected
    )
