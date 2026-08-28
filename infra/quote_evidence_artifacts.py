"""真实Raw Store到中立来源事实的映射；不含ACL、SDK或定位。"""

from typing import Literal, cast

from artifact_store.errors import (
    ArtifactBoundedReadUnavailable,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadLimitExceeded,
)
from artifact_store.store import (
    BoundedRawArtifactStore,
    RawArtifactKind,
    RawArtifactMeta,
)
from shared.schemas.evidence_read import (
    EvidenceRawContent,
    EvidenceRawMeta,
    QuoteEvidenceError,
)
from shared.schemas.identifiers import ArtifactId, TenantId


def _meta(meta: RawArtifactMeta) -> EvidenceRawMeta:
    if meta.kind not in (RawArtifactKind.PDF, RawArtifactKind.EMAIL_RAW):
        raise QuoteEvidenceError("source_unsupported")
    return EvidenceRawMeta(
        tenant_id=meta.tenant_id,
        artifact_id=meta.artifact_id,
        kind=meta.kind.value,
        mime_type=cast(Literal["application/pdf", "message/rfc822"], meta.mime_type),
        content_hash=meta.content_hash,
        size_bytes=meta.size_bytes,
        observed_at=meta.uploaded_at,
    )


def _failure(error: Exception) -> QuoteEvidenceError:
    if isinstance(error, QuoteEvidenceError):
        return QuoteEvidenceError(error.code)
    if isinstance(error, ArtifactNotFoundError):
        return QuoteEvidenceError("permission_denied")
    if isinstance(error, ArtifactReadLimitExceeded):
        return QuoteEvidenceError("source_limit_exceeded")
    if isinstance(error, ArtifactIntegrityError):
        return QuoteEvidenceError("source_integrity_failed")
    if isinstance(error, ArtifactBoundedReadUnavailable):
        return QuoteEvidenceError("source_unavailable")
    return QuoteEvidenceError("source_unavailable")


class RawQuoteEvidenceAdapter:
    """不把generated转换成raw，也不把取得时间当供应商报价时间。"""

    def __init__(self, store: BoundedRawArtifactStore) -> None:
        self._store = store

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> EvidenceRawMeta:
        """只映射取得原件时已存储的metadata。"""
        try:
            return _meta(await self._store.get_meta(tenant_id, artifact_id))
        except Exception as error:  # noqa: BLE001 - 基础设施异常不得泄露原件或连接
            raise _failure(error) from None

    async def read(
        self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int
    ) -> EvidenceRawContent:
        """只调用专用bounded能力，不回退旧get。"""
        try:
            meta, content = await self._store.get_bounded(
                tenant_id, artifact_id, maximum_bytes=maximum_bytes
            )
            return EvidenceRawContent(meta=_meta(meta), content=content)
        except Exception as error:  # noqa: BLE001 - 基础设施异常不得泄露原件或连接
            raise _failure(error) from None
