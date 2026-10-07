"""将Store安全metadata逐字段映射成本地Fact；无bytes、SDK或商业判断。"""

from artifact_store.errors import ArtifactNotFoundError
from artifact_store.store import GeneratedArtifactStore
from domains.quotations.errors import QuoteFileUnavailableError
from domains.quotations.schemas import QuoteGeneratedArtifactFact
from shared.schemas.identifiers import ArtifactId, TenantId


class GeneratedStoreQuoteArtifactReader:
    """只使用公共get_meta，不读取artifact repository或对象内容。"""

    def __init__(self, store: GeneratedArtifactStore) -> None:
        """依赖必须显式装配，缺失在调用时失败关闭。"""
        self._store = store

    async def read(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> QuoteGeneratedArtifactFact | None:
        """保留真实kind/MIME；缺记录与跨tenant同None，其余故障固定映射。"""
        try:
            meta = await self._store.get_meta(tenant_id, artifact_id)
            return QuoteGeneratedArtifactFact(
                tenant_id=meta.tenant_id,
                artifact_id=meta.artifact_id,
                kind=meta.kind.value,
                artifact_hash=meta.content_hash,
                size_bytes=meta.size_bytes,
                mime_type=meta.mime_type,
                workflow_run_id=meta.workflow_run_id,
                subject_ref=meta.subject_ref,
                sequence_number=meta.sequence_number,
                idempotency_key=meta.idempotency_key,
                generated_by=meta.generated_by,
                generated_at=meta.generated_at,
            )
        except ArtifactNotFoundError:
            return None
        except Exception:  # noqa: BLE001 -- 基础设施异常不透出SQL/路径，取消原样传播
            raise QuoteFileUnavailableError("dependency_unavailable") from None
