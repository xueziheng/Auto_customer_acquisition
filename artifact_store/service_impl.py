"""不可变 Raw / Generated Artifact Store 的确定性编排。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from datetime import datetime

from artifact_store.errors import (
    ArtifactBoundedReadUnavailable,
    ArtifactCommitUnknownError,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadLimitExceeded,
    ArtifactUnavailableError,
)
from artifact_store.repository import (
    ArtifactInsertStatus,
    ArtifactUnitOfWorkFactory,
    GeneratedArtifactRecord,
    RawArtifactRecord,
)
from artifact_store.store import (
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    RawArtifactKind,
    RawArtifactMeta,
    validate_generated_key,
)
from artifact_store.transport import (
    BlobObjectNotFoundError,
    BlobReadLimitExceeded,
    BoundedObjectBlobTransport,
    ObjectBlobTransport,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
)

_integrity_logger = logging.getLogger("security.artifact_integrity")
_cleanup_logger = logging.getLogger("artifact_store.cleanup")


def _require_content(content: object, maximum_bytes: int) -> bytes:
    if not isinstance(content, bytes) or not content or len(content) > maximum_bytes:
        raise ValidationError("artifact metadata 无效")
    return content


def _require_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError("artifact metadata 无效")
    return value


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_id(id_generator: Callable[[str], str]) -> ArtifactId:
    return ArtifactId(id_generator("art"))


async def _cleanup_object(
    transport: ObjectBlobTransport,
    object_key: str,
    *,
    primary: BaseException | None,
) -> None:
    try:
        await transport.delete(object_key)
    except asyncio.CancelledError:
        if primary is None:
            raise
        _cleanup_logger.error("Artifact 补偿清理失败")
    except BaseException:  # noqa: BLE001 - cleanup 不得覆盖 primary
        if primary is not None:
            _cleanup_logger.error("Artifact 补偿清理失败")
            return
        raise TransientError("Artifact 对象存储暂不可用") from None


def _integrity_failure(
    tenant_id: TenantId,
    artifact_id: ArtifactId,
    kind: RawArtifactKind | GeneratedArtifactKind,
) -> ArtifactIntegrityError:
    _integrity_logger.critical(
        "检测到 Artifact 完整性违规",
        extra={
            "tenant_id": str(tenant_id),
            "artifact_id": str(artifact_id),
            "kind": kind.value,
            "rule": "content_hash_and_size",
        },
    )
    return ArtifactIntegrityError()


def _generated_matches(
    winner: GeneratedArtifactMeta, candidate: GeneratedArtifactMeta
) -> bool:
    return (
        winner.kind,
        winner.content_hash,
        winner.size_bytes,
        winner.mime_type,
        winner.workflow_run_id,
        winner.subject_ref,
        winner.sequence_number,
        winner.idempotency_key,
        winner.generated_by,
    ) == (
        candidate.kind,
        candidate.content_hash,
        candidate.size_bytes,
        candidate.mime_type,
        candidate.workflow_run_id,
        candidate.subject_ref,
        candidate.sequence_number,
        candidate.idempotency_key,
        candidate.generated_by,
    )


class RawArtifactStoreImpl:
    """Raw metadata 与 bytes 的不可变、tenant-bound 编排。"""

    def __init__(
        self,
        uow_factory: ArtifactUnitOfWorkFactory,
        transport: ObjectBlobTransport,
        maximum_bytes: int,
        now: Callable[[], datetime],
        id_generator: Callable[[str], str],
        *,
        bounded_transport: BoundedObjectBlobTransport | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._transport = transport
        self._maximum_bytes = _require_limit(maximum_bytes)
        self._now = now
        self._id_generator = id_generator
        self._bounded_transport = bounded_transport

    def __repr__(self) -> str:
        return "RawArtifactStoreImpl()"

    async def put(
        self,
        tenant_id: TenantId,
        kind: RawArtifactKind,
        content: bytes,
        mime_type: str,
        uploaded_by: UserId | None = None,
    ) -> RawArtifactMeta:
        value = _require_content(content, self._maximum_bytes)
        meta = RawArtifactMeta(
            tenant_id,
            _artifact_id(self._id_generator),
            kind,
            _content_hash(value),
            len(value),
            mime_type,
            uploaded_by,
            self._now(),
        )
        candidate = RawArtifactRecord(
            meta,
            f"raw/{meta.tenant_id}/{meta.artifact_id}",
        )
        put_attempted = False
        result = None
        try:
            async with self._uow_factory(candidate.meta.tenant_id) as uow:
                existing = await uow.raw.get_by_hash(
                    candidate.meta.tenant_id,
                    candidate.meta.kind,
                    candidate.meta.content_hash,
                )
                if existing is not None:
                    return existing.meta
                put_attempted = True
                await self._transport.put(candidate.object_key, value)
                result = await uow.raw.insert_if_absent(candidate)
        except BaseException as primary:
            if put_attempted:
                await _cleanup_object(
                    self._transport, candidate.object_key, primary=primary
                )
            raise
        assert result is not None
        if result.status is ArtifactInsertStatus.EXISTING:
            await _cleanup_object(self._transport, candidate.object_key, primary=None)
        return result.winner.meta

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[RawArtifactMeta, bytes]:
        record = await self._get_record(tenant_id, artifact_id)
        try:
            content = await self._transport.get(record.object_key)
        except BlobObjectNotFoundError:
            raise ArtifactNotFoundError() from None
        if (
            len(content) != record.meta.size_bytes
            or _content_hash(content) != record.meta.content_hash
        ):
            raise _integrity_failure(tenant_id, artifact_id, record.meta.kind)
        return record.meta, content

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactMeta:
        return (await self._get_record(tenant_id, artifact_id)).meta

    async def get_bounded(
        self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int
    ) -> tuple[RawArtifactMeta, bytes]:
        """有界读取在metadata事务退出后执行，绝不调用旧get。"""
        limit = min(_require_limit(maximum_bytes), self._maximum_bytes)
        if self._bounded_transport is None:
            raise ArtifactBoundedReadUnavailable()
        record = await self._get_record(tenant_id, artifact_id)
        if record.meta.size_bytes > limit:
            raise ArtifactReadLimitExceeded()
        try:
            content = await self._bounded_transport.get_bounded(
                record.object_key, maximum_bytes=min(limit, record.meta.size_bytes)
            )
        except BlobReadLimitExceeded:
            raise ArtifactReadLimitExceeded() from None
        except BlobObjectNotFoundError:
            raise ArtifactNotFoundError() from None
        if (
            not isinstance(content, bytes)
            or len(content) != record.meta.size_bytes
            or _content_hash(content) != record.meta.content_hash
        ):
            raise _integrity_failure(tenant_id, artifact_id, record.meta.kind)
        return record.meta, content

    async def _get_record(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> RawArtifactRecord:
        async with self._uow_factory(tenant_id) as uow:
            record = await uow.raw.get_by_id(tenant_id, artifact_id)
        if record is None:
            raise ArtifactNotFoundError()
        return record


class GeneratedArtifactStoreImpl:
    """Generated metadata 与 bytes 的不可变、幂等编排。"""

    def __init__(
        self,
        uow_factory: ArtifactUnitOfWorkFactory,
        transport: ObjectBlobTransport,
        maximum_bytes: int,
        now: Callable[[], datetime],
        id_generator: Callable[[str], str],
        *, bounded_transport: BoundedObjectBlobTransport | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._transport = transport
        self._maximum_bytes = _require_limit(maximum_bytes)
        self._now = now
        self._id_generator = id_generator
        self._bounded_transport = bounded_transport

    def __repr__(self) -> str:
        return "GeneratedArtifactStoreImpl()"

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
    ) -> GeneratedArtifactMeta:
        value = _require_content(content, self._maximum_bytes)
        meta = GeneratedArtifactMeta(
            tenant_id,
            _artifact_id(self._id_generator),
            kind,
            _content_hash(value),
            len(value),
            mime_type,
            workflow_run_id,
            subject_ref,
            sequence_number,
            idempotency_key,
            generated_by,
            self._now(),
        )
        candidate = GeneratedArtifactRecord(
            meta, f"generated/{meta.tenant_id}/{meta.artifact_id}"
        )
        put_attempted = False
        result = None
        try:
            async with self._uow_factory(meta.tenant_id) as uow:
                existing = await uow.generated.get_by_idempotency_key(
                    meta.tenant_id, meta.idempotency_key
                )
                if existing is not None:
                    if not _generated_matches(existing.meta, meta):
                        raise ArtifactConflictError()
                    return existing.meta
                put_attempted = True
                await self._transport.put(candidate.object_key, value)
                result = await uow.generated.insert_if_absent(candidate)
        except BaseException as primary:
            if kind is GeneratedArtifactKind.QUOTE_PDF:
                if isinstance(primary, (asyncio.CancelledError, ArtifactConflictError)):
                    raise
                if put_attempted:
                    raise ArtifactCommitUnknownError() from None
                raise ArtifactUnavailableError() from None
            if put_attempted:
                await _cleanup_object(
                    self._transport, candidate.object_key, primary=primary
                )
            raise
        assert result is not None
        if result.status is ArtifactInsertStatus.EXISTING:
            try:
                await _cleanup_object(
                    self._transport, candidate.object_key, primary=None
                )
            except Exception:
                if kind is GeneratedArtifactKind.QUOTE_PDF:
                    raise ArtifactUnavailableError() from None
                raise
            if not _generated_matches(result.winner.meta, meta):
                raise ArtifactConflictError()
        return result.winner.meta

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[GeneratedArtifactMeta, bytes]:
        record = await self._get_record(tenant_id, artifact_id)
        try:
            content = await self._transport.get(record.object_key)
        except BlobObjectNotFoundError:
            raise ArtifactNotFoundError() from None
        if (
            len(content) != record.meta.size_bytes
            or _content_hash(content) != record.meta.content_hash
        ):
            raise _integrity_failure(tenant_id, artifact_id, record.meta.kind)
        return record.meta, content

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactMeta:
        return (await self._get_record(tenant_id, artifact_id)).meta

    async def get_bounded(self, tenant_id: TenantId, artifact_id: ArtifactId,
        *, maximum_bytes: int) -> tuple[GeneratedArtifactMeta, bytes]:
        """仅QUOTE_PDF且metadata事务已释放，实际读取最多metadata长度加一哨兵。"""
        limit = min(_require_limit(maximum_bytes), self._maximum_bytes)
        if self._bounded_transport is None:
            raise ArtifactBoundedReadUnavailable()
        record = await self._get_record(tenant_id, artifact_id)
        if record.meta.kind is not GeneratedArtifactKind.QUOTE_PDF or record.meta.mime_type != "application/pdf":
            raise ValidationError("派生文件绑定无效")
        if record.meta.size_bytes > limit:
            raise ArtifactReadLimitExceeded()
        try:
            content = await self._bounded_transport.get_bounded(record.object_key,
                maximum_bytes=min(limit, record.meta.size_bytes))
        except BlobReadLimitExceeded:
            raise ArtifactReadLimitExceeded() from None
        except BlobObjectNotFoundError:
            raise ArtifactNotFoundError() from None
        if (type(content) is not bytes or len(content) != record.meta.size_bytes
            or _content_hash(content) != record.meta.content_hash):
            raise _integrity_failure(tenant_id, artifact_id, record.meta.kind)
        return record.meta, content

    async def get_meta_by_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> GeneratedArtifactMeta | None:
        """仅读安全metadata，失败不重试、不触碰对象、不把暂未找到当未提交。"""
        validate_generated_key(tenant_id, idempotency_key)
        try:
            async with self._uow_factory(tenant_id) as uow:
                record = await uow.generated.get_by_idempotency_key(
                    tenant_id, idempotency_key
                )
            return None if record is None else record.meta
        except Exception:  # noqa: BLE001 -- 恢复读取失败必须固定脱敏
            raise ArtifactUnavailableError() from None

    async def _get_record(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> GeneratedArtifactRecord:
        async with self._uow_factory(tenant_id) as uow:
            record = await uow.generated.get_by_id(tenant_id, artifact_id)
        if record is None:
            raise ArtifactNotFoundError()
        return record
