"""Artifact metadata 的 repository 与事务边界契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from artifact_store.store import (
    GeneratedArtifactMeta,
    RawArtifactKind,
    RawArtifactMeta,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, IdempotencyKey, TenantId


@dataclass(frozen=True)
class RawArtifactRecord:
    """内部持久化记录；object key 不进入 repr 或公共 Meta。"""

    meta: RawArtifactMeta
    object_key: str = field(repr=False)

    def __post_init__(self) -> None:
        expected = f"raw/{self.meta.tenant_id}/{self.meta.artifact_id}"
        if self.object_key != expected:
            raise ValidationError("Artifact metadata record 无效")


@dataclass(frozen=True)
class GeneratedArtifactRecord:
    """内部派生产物记录；object key 不进入 repr 或公共 Meta。"""

    meta: GeneratedArtifactMeta
    object_key: str = field(repr=False)

    def __post_init__(self) -> None:
        expected = f"generated/{self.meta.tenant_id}/{self.meta.artifact_id}"
        if self.object_key != expected:
            raise ValidationError("Artifact metadata record 无效")


class ArtifactInsertStatus(str, Enum):
    """数据库唯一约束决定的 typed insert 结果。"""

    CREATED = "created"
    EXISTING = "existing"


@dataclass(frozen=True)
class RawArtifactInsertResult:
    status: ArtifactInsertStatus
    winner: RawArtifactRecord


@dataclass(frozen=True)
class GeneratedArtifactInsertResult:
    status: ArtifactInsertStatus
    winner: GeneratedArtifactRecord


@runtime_checkable
class RawArtifactRepository(Protocol):
    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactRecord | None: ...

    async def get_by_hash(
        self,
        tenant_id: TenantId,
        kind: RawArtifactKind,
        content_hash: str,
    ) -> RawArtifactRecord | None: ...

    async def insert_if_absent(
        self, record: RawArtifactRecord
    ) -> RawArtifactInsertResult: ...


@runtime_checkable
class GeneratedArtifactRepository(Protocol):
    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactRecord | None: ...

    async def get_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> GeneratedArtifactRecord | None: ...

    async def insert_if_absent(
        self, record: GeneratedArtifactRecord
    ) -> GeneratedArtifactInsertResult: ...


@runtime_checkable
class ArtifactUnitOfWork(Protocol):
    raw: RawArtifactRepository
    generated: GeneratedArtifactRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class ArtifactUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> ArtifactUnitOfWork: ...
