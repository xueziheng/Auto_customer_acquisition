"""员工工作上传、提取草稿与人工确认的公共契约。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeConfirmationId,
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    WorkExtractionId,
    WorkUploadId,
)


class WorkSourceKind(str, Enum):
    """预处理后交给提取能力的受限来源类型。"""

    CHAT_TRANSCRIPT = "chat_transcript"
    EMAIL_TEXT = "email_text"
    PDF_TEXT = "pdf_text"
    SPREADSHEET_TEXT = "spreadsheet_text"
    AUDIO_TRANSCRIPT = "audio_transcript"
    IMAGE_OCR = "image_ocr"


class WorkUploadStatus(str, Enum):
    """上传批次状态；只有 confirmed 能进入后续业务应用。"""

    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _reject_blank_strings(cls, value: object) -> object:
        if isinstance(value, str) and (not value or value != value.strip()):
            raise ValueError("文本字段不能为空或带首尾空白")
        return value


class ExtractedMoneyValue(_StrictModel):
    """模型识别到的客户原话金额；仍不是系统计算的最终金额。"""

    amount: str
    currency: str


class ExtractedFact(_StrictModel):
    fact_type: Literal[
        "customer_statement",
        "employee_statement",
        "customer_reaction",
        "activity",
    ]
    value: str
    evidence_quote: str


class ExtractedNeedField(_StrictModel):
    field_name: str
    value: str | ExtractedMoneyValue
    evidence_quote: str


class ExtractedCommitment(_StrictModel):
    commitment_type: Literal["employee", "customer"]
    action: str
    due_at: datetime
    due_at_uncertain: bool
    verbatim: str

    @field_validator("due_at")
    @classmethod
    def _absolute_due_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("承诺到期时间必须含时区")
        return value


class ProgressNote(_StrictModel):
    summary: str
    evidence_quotes: tuple[str, ...]

    @model_validator(mode="after")
    def _nonempty_unique_evidence(self) -> ProgressNote:
        if (
            not self.evidence_quotes
            or len(self.evidence_quotes) != len(set(self.evidence_quotes))
        ):
            raise ValueError("进展摘要必须包含不重复的原文证据")
        return self


class ExtractionPayload(_StrictModel):
    """Agent 原始提取或员工修订后的完整、版本化结构。"""

    facts: tuple[ExtractedFact, ...] = ()
    need_field_updates: tuple[ExtractedNeedField, ...] = ()
    commitments: tuple[ExtractedCommitment, ...] = ()
    progress_note: ProgressNote | None = None


class EmployeeConfirmationView(_StrictModel):
    """追加式人工版本；payload 不覆盖 Agent 原始提取。"""

    confirmation_id: EmployeeConfirmationId
    revision: int
    payload: ExtractionPayload
    confirmed_by: EmployeeId
    confirmed_at: datetime

    @field_validator("revision")
    @classmethod
    def _positive_revision(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("人工确认版本必须为正整数")
        return value

    @field_validator("confirmed_at")
    @classmethod
    def _aware_confirmed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("确认时间必须含时区")
        return value


class WorkExtractionView(_StrictModel):
    extraction_id: WorkExtractionId
    upload_id: WorkUploadId
    payload: ExtractionPayload
    extracted_by: str
    created_at: datetime
    confirmation: EmployeeConfirmationView | None

    @property
    def is_confirmed(self) -> bool:
        return self.confirmation is not None


class WorkUploadView(_StrictModel):
    """上传批次的安全读取模型；不暴露 bytes、对象键或凭证。"""

    upload_id: WorkUploadId
    tenant_id: TenantId
    artifact_id: ArtifactId
    employee_id: EmployeeId
    source_kind: WorkSourceKind
    status: WorkUploadStatus
    occurred_at: datetime
    customer_timezone: str
    created_at: datetime
    account_id: ProspectAccountId | None = None
    opportunity_id: OpportunityId | None = None
    need_id: ValidatedNeedId | None = None

    @field_validator("occurred_at", "created_at")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("工作时间必须含时区")
        return value

    @field_validator("customer_timezone")
    @classmethod
    def _iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("客户时区无效") from None
        return value


__all__ = (
    "EmployeeConfirmationView",
    "ExtractedCommitment",
    "ExtractedFact",
    "ExtractedMoneyValue",
    "ExtractedNeedField",
    "ExtractionPayload",
    "ProgressNote",
    "WorkExtractionView",
    "WorkSourceKind",
    "WorkUploadStatus",
    "WorkUploadView",
)
