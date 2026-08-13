"""不可变 Artifact 的公共 typed 契约。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
)

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_ID = re.compile(rf"tn_{_ULID}")
_ARTIFACT_ID = re.compile(rf"art_{_ULID}")
_RUN_ID = re.compile(rf"run_{_ULID}")
_USER_ID = re.compile(rf"usr_{_ULID}")
_SUBJECT_REF = re.compile(rf"enr_{_ULID}")
_HASH = re.compile(r"[0-9a-f]{64}")
_GENERATED_BY = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MAX_SIGNED_INT64 = 2**63 - 1
_INVALID_METADATA = "artifact metadata 无效"


class RawArtifactKind(str, Enum):
    """只代表外部取得的原始证据。"""

    EMAIL_RAW = "email_raw"
    CHAT_SCREENSHOT = "chat_screenshot"
    PDF = "pdf"
    WORD = "word"
    EXCEL = "excel"
    WEB_SNAPSHOT = "web_snapshot"
    IMAGE = "image"
    AUDIO = "audio"


class GeneratedArtifactKind(str, Enum):
    """只代表 TradeOS 生成的派生产物。"""

    EMAIL_DRAFT = "email_draft"


RAW_ARTIFACT_MIME_TYPES: Mapping[RawArtifactKind, frozenset[str]] = MappingProxyType({
    RawArtifactKind.EMAIL_RAW: frozenset({"message/rfc822"}),
    RawArtifactKind.CHAT_SCREENSHOT: frozenset(
        {"image/png", "image/jpeg", "image/webp"}
    ),
    RawArtifactKind.PDF: frozenset({"application/pdf"}),
    RawArtifactKind.WORD: frozenset(
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        }
    ),
    RawArtifactKind.EXCEL: frozenset(
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "text/csv",
        }
    ),
    RawArtifactKind.WEB_SNAPSHOT: frozenset({"text/html"}),
    RawArtifactKind.IMAGE: frozenset({"image/png", "image/jpeg", "image/webp"}),
    RawArtifactKind.AUDIO: frozenset({"audio/mpeg", "audio/wav", "audio/mp4"}),
})

GENERATED_ARTIFACT_MIME_TYPES: Mapping[
    GeneratedArtifactKind, frozenset[str]
] = MappingProxyType({
    GeneratedArtifactKind.EMAIL_DRAFT: frozenset(
        {"application/vnd.tradeos.email-draft+json"}
    )
})


def _invalid() -> ValidationError:
    return ValidationError(_INVALID_METADATA)


def _canonical_string(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise _invalid()
    return value


def _safe_text(value: object, pattern: re.Pattern[str]) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(character).startswith("C") for character in value)
        or pattern.fullmatch(value) is None
    ):
        raise _invalid()
    return value


def _positive_int(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= _MAX_SIGNED_INT64
    ):
        raise _invalid()
    return value


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not UTC:
        raise _invalid()
    return value


@dataclass(frozen=True)
class RawArtifactMeta:
    """原始证据的安全元数据；不包含对象键或原始内容。"""

    tenant_id: TenantId
    artifact_id: ArtifactId
    kind: RawArtifactKind
    content_hash: str
    size_bytes: int
    mime_type: str
    uploaded_by: UserId | None
    uploaded_at: datetime

    def __post_init__(self) -> None:
        tenant_id = TenantId(_canonical_string(self.tenant_id, _TENANT_ID))
        artifact_id = ArtifactId(_canonical_string(self.artifact_id, _ARTIFACT_ID))
        if not isinstance(self.kind, RawArtifactKind):
            raise _invalid()
        content_hash = _canonical_string(self.content_hash, _HASH)
        size_bytes = _positive_int(self.size_bytes)
        if (
            not isinstance(self.mime_type, str)
            or self.mime_type not in RAW_ARTIFACT_MIME_TYPES[self.kind]
        ):
            raise _invalid()
        if self.uploaded_by is None:
            uploaded_by = None
        else:
            uploaded_by = UserId(_canonical_string(self.uploaded_by, _USER_ID))
        uploaded_at = _utc_datetime(self.uploaded_at)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "size_bytes", size_bytes)
        object.__setattr__(self, "uploaded_by", uploaded_by)
        object.__setattr__(self, "uploaded_at", uploaded_at)


@dataclass(frozen=True)
class GeneratedArtifactMeta:
    """派生产物的安全元数据；不包含对象键或生成内容。"""

    tenant_id: TenantId
    artifact_id: ArtifactId
    kind: GeneratedArtifactKind
    content_hash: str
    size_bytes: int
    mime_type: str
    workflow_run_id: RunId
    subject_ref: str
    sequence_number: int
    idempotency_key: IdempotencyKey
    generated_by: str
    generated_at: datetime

    def __post_init__(self) -> None:
        tenant_id = TenantId(_canonical_string(self.tenant_id, _TENANT_ID))
        artifact_id = ArtifactId(_canonical_string(self.artifact_id, _ARTIFACT_ID))
        if not isinstance(self.kind, GeneratedArtifactKind):
            raise _invalid()
        content_hash = _canonical_string(self.content_hash, _HASH)
        size_bytes = _positive_int(self.size_bytes)
        if (
            not isinstance(self.mime_type, str)
            or self.mime_type not in GENERATED_ARTIFACT_MIME_TYPES[self.kind]
        ):
            raise _invalid()
        workflow_run_id = RunId(
            _canonical_string(self.workflow_run_id, _RUN_ID)
        )
        subject_ref = _safe_text(self.subject_ref, _SUBJECT_REF)
        sequence_number = _positive_int(self.sequence_number)
        expected_key = f"{subject_ref}:{sequence_number}:draft"
        if (
            not isinstance(self.idempotency_key, str)
            or self.idempotency_key != expected_key
        ):
            raise _invalid()
        idempotency_key = IdempotencyKey(self.idempotency_key)
        generated_by = _safe_text(self.generated_by, _GENERATED_BY)
        generated_at = _utc_datetime(self.generated_at)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "artifact_id", artifact_id)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "size_bytes", size_bytes)
        object.__setattr__(self, "workflow_run_id", workflow_run_id)
        object.__setattr__(self, "subject_ref", subject_ref)
        object.__setattr__(self, "sequence_number", sequence_number)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(self, "generated_by", generated_by)
        object.__setattr__(self, "generated_at", generated_at)


@runtime_checkable
class RawArtifactStore(Protocol):
    """不可变原始证据 Store；刻意不提供 update/delete/list-all。"""

    async def put(
        self,
        tenant_id: TenantId,
        kind: RawArtifactKind,
        content: bytes,
        mime_type: str,
        uploaded_by: UserId | None = None,
    ) -> RawArtifactMeta: ...

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[RawArtifactMeta, bytes]: ...

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactMeta: ...


@runtime_checkable
class GeneratedArtifactStore(Protocol):
    """不可变派生产物 Store；与原始证据的 kind 和幂等语义分离。"""

    async def put(
        self,
        tenant_id: TenantId,
        kind: GeneratedArtifactKind,
        content: bytes,
        mime_type: str,
        *,
        workflow_run_id: RunId,
        subject_ref: str,
        sequence_number: int,
        idempotency_key: IdempotencyKey,
        generated_by: str,
    ) -> GeneratedArtifactMeta: ...

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[GeneratedArtifactMeta, bytes]: ...

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactMeta: ...
