"""Artifact metadata 的 tenant-bound PostgreSQL repositories。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from artifact_store.repository import (
    ArtifactInsertStatus,
    GeneratedArtifactInsertResult,
    GeneratedArtifactRecord,
    GeneratedArtifactRepository,
    RawArtifactInsertResult,
    RawArtifactRecord,
    RawArtifactRepository,
)
from artifact_store.store import (
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    RawArtifactKind,
    RawArtifactMeta,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import GeneratedArtifactRow, RawArtifactRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
)

_tenant_logger = logging.getLogger("security.tenant_isolation")


class _ArtifactRepository(TenantScopedRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")


def _raw_record(row: RawArtifactRow) -> RawArtifactRecord:
    return RawArtifactRecord(
        RawArtifactMeta(
            TenantId(row.tenant_id),
            ArtifactId(row.artifact_id),
            RawArtifactKind(row.kind),
            row.content_hash,
            row.size_bytes,
            row.mime_type,
            UserId(row.uploaded_by) if row.uploaded_by is not None else None,
            row.uploaded_at,
        ),
        row.object_key,
    )


def _generated_record(row: GeneratedArtifactRow) -> GeneratedArtifactRecord:
    return GeneratedArtifactRecord(
        GeneratedArtifactMeta(
            tenant_id=TenantId(row.tenant_id),
            artifact_id=ArtifactId(row.artifact_id),
            kind=GeneratedArtifactKind(row.kind),
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            mime_type=row.mime_type,
            workflow_run_id=RunId(row.workflow_run_id),
            subject_ref=row.subject_ref,
            sequence_number=row.sequence_number,
            idempotency_key=IdempotencyKey(row.idempotency_key),
            generated_by=row.generated_by,
            generated_at=row.generated_at,
        ),
        row.object_key,
    )


class RawArtifactRepositoryImpl(_ArtifactRepository, RawArtifactRepository):
    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactRecord | None:
        self._require_tenant(tenant_id, "artifact_raw_get")
        row = (
            await self._session.execute(
                select(RawArtifactRow).where(
                    RawArtifactRow.tenant_id == self._tenant_id,
                    RawArtifactRow.artifact_id == artifact_id,
                )
            )
        ).scalar_one_or_none()
        return _raw_record(row) if row is not None else None

    async def get_by_hash(
        self,
        tenant_id: TenantId,
        kind: RawArtifactKind,
        content_hash: str,
    ) -> RawArtifactRecord | None:
        self._require_tenant(tenant_id, "artifact_raw_hash_get")
        row = (
            await self._session.execute(
                select(RawArtifactRow).where(
                    RawArtifactRow.tenant_id == self._tenant_id,
                    RawArtifactRow.kind == kind.value,
                    RawArtifactRow.content_hash == content_hash,
                )
            )
        ).scalar_one_or_none()
        return _raw_record(row) if row is not None else None

    async def insert_if_absent(
        self, record: RawArtifactRecord
    ) -> RawArtifactInsertResult:
        self._require_tenant(record.meta.tenant_id, "artifact_raw_insert")
        created = (
            await self._session.execute(
                insert(RawArtifactRow)
                .values(
                    tenant_id=str(record.meta.tenant_id),
                    artifact_id=str(record.meta.artifact_id),
                    kind=record.meta.kind.value,
                    content_hash=record.meta.content_hash,
                    size_bytes=record.meta.size_bytes,
                    mime_type=record.meta.mime_type,
                    object_key=record.object_key,
                    uploaded_by=(
                        str(record.meta.uploaded_by)
                        if record.meta.uploaded_by is not None
                        else None
                    ),
                    uploaded_at=record.meta.uploaded_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_raw_artifacts_tenant_kind_hash"
                )
                .returning(RawArtifactRow.artifact_id)
            )
        ).scalar_one_or_none()
        if created is not None:
            return RawArtifactInsertResult(ArtifactInsertStatus.CREATED, record)
        winner = await self.get_by_hash(
            record.meta.tenant_id,
            record.meta.kind,
            record.meta.content_hash,
        )
        if winner is None:
            raise RuntimeError("Artifact raw winner 缺失")
        return RawArtifactInsertResult(ArtifactInsertStatus.EXISTING, winner)


class GeneratedArtifactRepositoryImpl(
    _ArtifactRepository, GeneratedArtifactRepository
):
    async def get_by_id(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactRecord | None:
        self._require_tenant(tenant_id, "artifact_generated_get")
        row = (
            await self._session.execute(
                select(GeneratedArtifactRow).where(
                    GeneratedArtifactRow.tenant_id == self._tenant_id,
                    GeneratedArtifactRow.artifact_id == artifact_id,
                )
            )
        ).scalar_one_or_none()
        return _generated_record(row) if row is not None else None

    async def get_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> GeneratedArtifactRecord | None:
        self._require_tenant(tenant_id, "artifact_generated_key_get")
        row = (
            await self._session.execute(
                select(GeneratedArtifactRow).where(
                    GeneratedArtifactRow.tenant_id == self._tenant_id,
                    GeneratedArtifactRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        return _generated_record(row) if row is not None else None

    async def insert_if_absent(
        self, record: GeneratedArtifactRecord
    ) -> GeneratedArtifactInsertResult:
        self._require_tenant(record.meta.tenant_id, "artifact_generated_insert")
        created = (
            await self._session.execute(
                insert(GeneratedArtifactRow)
                .values(
                    tenant_id=str(record.meta.tenant_id),
                    artifact_id=str(record.meta.artifact_id),
                    kind=record.meta.kind.value,
                    content_hash=record.meta.content_hash,
                    size_bytes=record.meta.size_bytes,
                    mime_type=record.meta.mime_type,
                    object_key=record.object_key,
                    workflow_run_id=str(record.meta.workflow_run_id),
                    subject_ref=record.meta.subject_ref,
                    sequence_number=record.meta.sequence_number,
                    idempotency_key=str(record.meta.idempotency_key),
                    generated_by=record.meta.generated_by,
                    generated_at=record.meta.generated_at,
                )
                .on_conflict_do_nothing(constraint="uq_artifacts_tenant_key")
                .returning(GeneratedArtifactRow.artifact_id)
            )
        ).scalar_one_or_none()
        if created is not None:
            return GeneratedArtifactInsertResult(
                ArtifactInsertStatus.CREATED, record
            )
        winner = await self.get_by_idempotency_key(
            record.meta.tenant_id, record.meta.idempotency_key
        )
        if winner is None:
            raise RuntimeError("Artifact generated winner 缺失")
        return GeneratedArtifactInsertResult(ArtifactInsertStatus.EXISTING, winner)
