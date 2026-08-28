"""公开Generated Store到中立文档端口的技术适配，无SQL/SDK或业务判权。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import ValidationError as SchemaError

from artifact_store.errors import (
    ArtifactBoundedReadUnavailable,
    ArtifactCommitUnknownError,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadLimitExceeded,
    ArtifactUnavailableError,
)
from artifact_store.store import (
    BoundedGeneratedArtifactStore,
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    GeneratedArtifactStore,
)
from shared.errors import ValidationError
from shared.schemas.generated_documents import (
    GeneratedDocumentError,
    GeneratedDocumentMeta,
    GeneratedDocumentPayload,
)
from shared.schemas.identifiers import ArtifactId, IdempotencyKey, RunId, TenantId


@asynccontextmanager
async def _errors(*, writing: bool = False) -> AsyncIterator[None]:
    """取消透传；仅确定类型错误保留，未知写尝试保守待核对。"""
    try:
        yield
    except GeneratedDocumentError:
        raise
    except ArtifactNotFoundError:
        raise GeneratedDocumentError("not_found") from None
    except (ValidationError, SchemaError):
        raise GeneratedDocumentError("invalid_binding") from None
    except ArtifactConflictError:
        raise GeneratedDocumentError("conflict") from None
    except ArtifactCommitUnknownError:
        raise GeneratedDocumentError("commit_unknown") from None
    except ArtifactIntegrityError:
        raise GeneratedDocumentError("corrupt") from None
    except ArtifactReadLimitExceeded:
        raise GeneratedDocumentError("read_limit") from None
    except ArtifactBoundedReadUnavailable:
        raise GeneratedDocumentError("bounded_unavailable") from None
    except ArtifactUnavailableError:
        raise GeneratedDocumentError("unavailable") from None
    except Exception:  # noqa: BLE001 -- 不泄露SQL/endpoint/对象key，未知写禁止宣称未提交
        raise GeneratedDocumentError(
            "commit_unknown" if writing else "unavailable"
        ) from None


def _meta(
    value: GeneratedArtifactMeta,
    tenant_id: TenantId,
    *,
    key: IdempotencyKey | None = None,
    artifact_id: ArtifactId | None = None,
) -> GeneratedDocumentMeta:
    """逐字段投影，拒绝email_draft或跨租户错绑定。"""
    if (
        value.kind is not GeneratedArtifactKind.QUOTE_PDF
        or value.mime_type != "application/pdf"
        or value.tenant_id != tenant_id
        or key is not None
        and value.idempotency_key != key
        or artifact_id is not None
        and value.artifact_id != artifact_id
    ):
        raise GeneratedDocumentError("invalid_binding")
    return GeneratedDocumentMeta(
        tenant_id=value.tenant_id,
        artifact_id=value.artifact_id,
        kind="quote_pdf",
        artifact_hash=value.content_hash,
        size_bytes=value.size_bytes,
        mime_type="application/pdf",
        workflow_run_id=value.workflow_run_id,
        subject_ref=value.subject_ref,
        sequence_number=value.sequence_number,
        idempotency_key=value.idempotency_key,
        generated_by=value.generated_by,
        generated_at=value.generated_at,
    )


class GeneratedStoreDocumentMetadataReader:
    """独立对象仅暴露metadata读取，显式恢复不得获得对象操作方法。"""

    def __init__(self, store: GeneratedArtifactStore) -> None:
        self._store = store

    async def get_meta_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> GeneratedDocumentMeta | None:
        """读取公开tenant/key端口，绝不访问对象bytes。"""
        async with _errors():
            value = await self._store.get_meta_by_key(tenant_id, key)
            return None if value is None else _meta(value, tenant_id, key=key)


class GeneratedStoreDocumentAdapter:
    """正式文件路径公开适配；与只读metadata对象分开装配。"""

    def __init__(
        self, store: GeneratedArtifactStore, bounded: BoundedGeneratedArtifactStore
    ) -> None:
        self._store, self._bounded = store, bounded

    async def get_meta_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> GeneratedDocumentMeta | None:
        """同原键恢复metadata，不新建不同存储契约。"""
        return await GeneratedStoreDocumentMetadataReader(self._store).get_meta_by_key(
            tenant_id, key
        )

    async def get_bounded(
        self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int
    ) -> GeneratedDocumentPayload:
        """只消费独立有界端口，不fallback旧get。"""
        async with _errors():
            value, content = await self._bounded.get_bounded(
                tenant_id, artifact_id, maximum_bytes=maximum_bytes
            )
            return GeneratedDocumentPayload(
                meta=_meta(value, tenant_id, artifact_id=artifact_id), content=content
            )

    async def put_pdf(
        self,
        tenant_id: TenantId,
        content: bytes,
        *,
        workflow_run_id: RunId,
        subject_ref: str,
        sequence_number: int,
        idempotency_key: IdempotencyKey,
        generated_by: str,
    ) -> GeneratedDocumentMeta:
        """固定QUOTE_PDF，沿T6保守提交语义，不清理candidate。"""
        async with _errors(writing=True):
            value = await self._store.put(
                tenant_id,
                GeneratedArtifactKind.QUOTE_PDF,
                content,
                "application/pdf",
                workflow_run_id=workflow_run_id,
                subject_ref=subject_ref,
                sequence_number=sequence_number,
                idempotency_key=idempotency_key,
                generated_by=generated_by,
            )
            return _meta(value, tenant_id, key=idempotency_key)
