"""报价文件安全metadata与本域不可变持久DTO，不含bytes或存储地址。"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    QuoteFileId,
    QuoteId,
    RunId,
    TenantId,
)

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(gt=0, le=2**63 - 1)]


def _utc(value: datetime) -> datetime:
    """拒绝无时区时间并规范为UTC，不以本机时区补齐。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("文件时间必须带时区")
    return value.astimezone(UTC)


Timestamp = Annotated[datetime, AfterValidator(_utc)]


class QuoteFileView(BaseModel):
    """三个hash各自来自原报价、客户投影及产物bytes，不可互换。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    file_id: QuoteFileId = Field(pattern=rf"^qfl_{_ULID}$")
    quote_id: QuoteId = Field(pattern=rf"^quo_{_ULID}$")
    quote_version: Positive
    artifact_id: ArtifactId = Field(pattern=rf"^art_{_ULID}$")
    content_hash: Hash
    quote_content_hash: Hash
    customer_content_hash: Hash
    template_version: str
    size_bytes: Positive
    generated_at: Timestamp


class QuoteFileApprovalFact(BaseModel):
    """历史成功批准归属，不代表当前正式文件使用许可。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: TenantId = Field(pattern=rf"^tn_{_ULID}$")
    quote_id: QuoteId = Field(pattern=rf"^quo_{_ULID}$")
    quote_version: Positive
    quote_content_hash: Hash
    approval_run_id: RunId = Field(pattern=rf"^run_{_ULID}$")
    approval_facts_hash: Hash
    applied_at: Timestamp


class QuoteGeneratedArtifactFact(BaseModel):
    """受信reader逐字段映射的真实metadata，不预先强转kind或MIME。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: TenantId = Field(pattern=rf"^tn_{_ULID}$")
    artifact_id: ArtifactId = Field(pattern=rf"^art_{_ULID}$")
    kind: str
    artifact_hash: Hash
    size_bytes: Positive
    mime_type: str
    workflow_run_id: RunId = Field(pattern=rf"^run_{_ULID}$")
    subject_ref: str
    sequence_number: Positive
    idempotency_key: IdempotencyKey
    generated_by: str
    generated_at: Timestamp


class QuoteFileRecord(BaseModel):
    """本域持久记录，不注册HTTP，也不携带客户原文。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: TenantId = Field(pattern=rf"^tn_{_ULID}$")
    view: QuoteFileView
    approval_run_id: RunId = Field(pattern=rf"^run_{_ULID}$")
