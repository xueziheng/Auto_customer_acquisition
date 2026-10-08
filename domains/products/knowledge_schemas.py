"""企业资料的公共契约：原件、模型草稿和人工确认保持分离。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId
from shared.schemas.provenance import ProvenanceSummary

KnowledgeSourceKind = Literal["document_text", "vision_transcription"]
KnowledgeStatus = Literal["queued", "processing", "awaiting_confirmation", "confirmed", "failed", "unknown"]
KnowledgeExportState = Literal["pending", "synced", "failed"]
KnowledgeFailureReason = Literal["processing_failed", "model_result_unknown", "lease_expired", "authorization_revoked", "invalid_analysis", "source_unsupported", "source_limit_exceeded", "source_integrity_failed", "provider_unavailable"]
_SAFE_FAILURES = frozenset({"processing_failed", "model_result_unknown", "lease_expired", "authorization_revoked", "invalid_analysis", "source_unsupported", "source_limit_exceeded", "source_integrity_failed", "provider_unavailable"})
_SECRET = re.compile(r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:sk-|sk_)[a-z0-9_-]{20,}|(?:postgres(?:ql)?|mysql)://[^\s]+:[^\s]+@|(?:authorization|api[_ -]?key|access[_ -]?token|password)\s*[:=]\s*\S{6,})")


def require_safe_text(value: str) -> str:
    """拒绝常见凭证标记；错误只返回固定说明，不回显原文。"""
    if _SECRET.search(value) or "\x00" in value:
        raise ValueError("资料包含不允许的敏感内容")
    return value


def require_parse_warnings(values: tuple[str, ...]) -> tuple[str, ...]:
    """解析提醒仅供单独展示，不参与可引用正文；固定说明也执行敏感信息检查。"""
    if type(values) is not tuple or len(values) > 4:
        raise ValueError("资料解析提醒无效")
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            raise ValueError("资料解析提醒无效")
        require_safe_text(value)
    return values


class KnowledgeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", hide_input_in_errors=True)


class KnowledgeActor(KnowledgeModel):
    tenant_id: TenantId
    employee_id: EmployeeId


class KnowledgeSource(KnowledgeModel):
    filename: str = Field(min_length=1, max_length=240)
    mime_type: str = Field(min_length=1, max_length=120)
    size_bytes: int = Field(strict=True, ge=1, le=20_000_000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: ArtifactId

    @field_validator("filename")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if value != value.strip() or any(c in value for c in ("/", "\\", "\x00")) or value.startswith(".") or value.casefold() == "agents.md":
            raise ValueError("资料文件名无效")
        return require_safe_text(value)


class KnowledgeFact(KnowledgeModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=2000)
    source_quote: str = Field(min_length=1, max_length=4000)


class KnowledgeInference(KnowledgeModel):
    text: str = Field(min_length=1, max_length=3000)
    evidence_quotes: tuple[str, ...] = Field(default=(), max_length=20)
    image_pages: tuple[int, ...] = Field(default=(), max_length=10)

    @field_validator("image_pages", mode="before")
    @classmethod
    def strict_image_pages(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)) or any(type(page) is not int or not 1 <= page <= 10 for page in value):
            raise ValueError("视觉来源页号无效")
        if len(set(value)) != len(value):
            raise ValueError("视觉来源页号不得重复")
        return value


class KnowledgeAnalysis(KnowledgeModel):
    title: str = Field(min_length=1, max_length=240)
    facts: tuple[KnowledgeFact, ...] = Field(max_length=100)
    inferences: tuple[KnowledgeInference, ...] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_text(self) -> Self:
        require_safe_text(self.model_dump_json())
        if not self.facts and not self.inferences:
            raise ValueError("资料分析不能为空")
        for value in (self.title, *(x.label for x in self.facts), *(x.value for x in self.facts), *(x.source_quote for x in self.facts), *(x.text for x in self.inferences)):
            if not value.strip():
                raise ValueError("资料分析字段不能为空")
        return self

    def validate_evidence(self, source_text: str, image_count: int = 0) -> None:
        """事实值须逐字出现在来源引文，推断只能引用已核实引文。"""
        require_safe_text(source_text)
        if type(image_count) is not int or not 0 <= image_count <= 10:
            raise ValueError("可用图像页数无效")
        quotes = {item.source_quote for item in self.facts}
        for fact in self.facts:
            if fact.source_quote not in source_text or fact.value not in fact.source_quote:
                raise ValueError("资料分析缺少可核实的原文证据")
        for inference in self.inferences:
            if not inference.evidence_quotes and not inference.image_pages:
                raise ValueError("资料推断缺少来源")
            if any(page > image_count for page in inference.image_pages):
                raise ValueError("视觉来源页号超出本次原件")
            if any(not quote.strip() or quote not in quotes for quote in inference.evidence_quotes):
                raise ValueError("资料推断缺少已核实证据")


class KnowledgeDocumentView(KnowledgeModel):
    document_id: str
    tenant_id: TenantId
    source: KnowledgeSource
    uploader_id: EmployeeId
    status: KnowledgeStatus
    version: int
    job_id: str
    current_revision_id: str | None = None
    export_state: KnowledgeExportState = "pending"
    failure_reason: KnowledgeFailureReason | None = None
    can_retry: bool = False
    can_confirm: bool = False
    can_sync: bool = False
    created_at: datetime
    updated_at: datetime


class KnowledgeRevisionView(KnowledgeModel):
    revision_id: str
    document_id: str
    job_id: str
    source_text: str = Field(min_length=1, max_length=200_000, repr=False)
    analysis: KnowledgeAnalysis
    model: str = Field(min_length=1, max_length=128)
    extracted_by: str = Field(min_length=1, max_length=128)
    extracted_at: datetime
    fact_provenance: tuple[ProvenanceSummary, ...]
    source_kind: KnowledgeSourceKind = "document_text"
    image_count: int = Field(default=0, strict=True, ge=0, le=10)
    image_source_pages: tuple[int, ...] = ()
    parse_warnings: tuple[Annotated[str, Field(min_length=1, max_length=512)], ...] = Field(default=(), max_length=4)
    confirmed_by: EmployeeId | None = None
    confirmed_at: datetime | None = None


    @field_validator("parse_warnings")
    @classmethod
    def safe_parse_warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_parse_warnings(value)

    @field_validator("image_source_pages", mode="before")
    @classmethod
    def strict_source_pages(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)) or any(type(page) is not int or page < 1 for page in value):
            raise ValueError("原文件图像页号无效")
        if tuple(sorted(set(value))) != tuple(value):
            raise ValueError("原文件图像页号须唯一升序")
        return value

    @model_validator(mode="after")
    def source_layout(self) -> Self:
        if len(self.fact_provenance) != len(self.analysis.facts):
            raise ValueError("资料事实来源链不完整")
        if (self.confirmed_by is None) != (self.confirmed_at is None):
            raise ValueError("资料确认信息不完整")
        if any(item.confirmed_by != self.confirmed_by or item.confirmed_at != self.confirmed_at for item in self.fact_provenance):
            raise ValueError("资料确认来源链不一致")
        if len(self.image_source_pages) != self.image_count:
            raise ValueError("视觉来源映射不完整")
        if (self.source_kind == "document_text" and self.image_count != 0) or (self.source_kind == "vision_transcription" and self.image_count < 1):
            raise ValueError("资料来源形态无效")
        return self


class KnowledgeDocumentDetail(KnowledgeModel):
    document: KnowledgeDocumentView
    revision: KnowledgeRevisionView | None


class KnowledgePage(KnowledgeModel):
    items: tuple[KnowledgeDocumentView, ...]
    total: int
    limit: int
    offset: int


class KnowledgeClaim(KnowledgeModel):
    tenant_id: TenantId
    job_id: str
    document_id: str
    actor_id: EmployeeId
    user_id: str
    lease_token: str = Field(repr=False)
    lease_until: datetime
    attempt: int
    source: KnowledgeSource


class KnowledgeJob(KnowledgeModel):
    """受信处理账本，不作为HTTP响应。"""
    tenant_id: TenantId
    job_id: str
    document_id: str
    actor_id: EmployeeId
    user_id: str
    state: Literal["queued", "processing", "complete", "failed", "unknown"]
    attempt: int
    lease_token: str | None = Field(default=None, repr=False)
    lease_until: datetime | None = None
    created_at: datetime

class KnowledgeConfirmCommand(KnowledgeModel):
    revision_id: str = Field(min_length=1, max_length=40)
    expected_version: int = Field(strict=True, ge=1)


class KnowledgeRetryCommand(KnowledgeModel):
    expected_version: int = Field(strict=True, ge=1)
    acknowledge_unknown: bool = Field(default=False, strict=True)


class KnowledgeSyncCommand(KnowledgeConfirmCommand):
    pass
