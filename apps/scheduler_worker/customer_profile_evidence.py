"""本次Gateway快照与本企业不可变原件的双重核验，不读取公共缓存原租户。"""
import hashlib

from artifact_store.store import BoundedRawArtifactStore, RawArtifactKind
from connectors.web_search.client import PageSnapshot, _VisibleTextParser
from domains.prospecting.agent_profile import AgentProfileSnapshot
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import ArtifactId, TenantId


class ArchivedCustomerProfileEvidence:
    """只绑定当前任务实际取得的一份页面，不接受模型给出的租户或正文。"""

    def __init__(self, tenant_id: TenantId, snapshot: PageSnapshot, artifacts: BoundedRawArtifactStore) -> None:
        self._tenant, self._snapshot, self._artifacts = tenant_id, snapshot, artifacts

    async def read(self, tenant_id: TenantId, artifact_id: ArtifactId) -> AgentProfileSnapshot:
        """有界读取原件并重新提取正文，校验hash和Gateway返回的文本一致。"""
        snapshot = self._snapshot
        if tenant_id != self._tenant or artifact_id != snapshot.snapshot_artifact_ref:
            raise PermissionDenied("准备任务来源不属于当前企业")
        meta, content = await self._artifacts.get_bounded(tenant_id, artifact_id, maximum_bytes=2_097_152)
        if (meta.tenant_id != tenant_id or meta.artifact_id != artifact_id
            or meta.kind is not RawArtifactKind.WEB_SNAPSHOT
            or meta.content_hash != snapshot.content_hash
            or hashlib.sha256(content).hexdigest() != snapshot.content_hash):
            raise ValidationError("准备任务来源原件不一致")
        parser = _VisibleTextParser()
        try:
            parser.feed(content.decode("utf-8", errors="replace"))
            parser.close()
            visible = parser.text()
        except Exception:  # noqa: BLE001 - 原始页面异常不得泄露
            raise ValidationError("准备任务来源无法核验") from None
        if visible != snapshot.text:
            raise ValidationError("准备任务来源正文不一致")
        return AgentProfileSnapshot(tenant_id=tenant_id, artifact_id=artifact_id,
            source_url=snapshot.url, page_hash=snapshot.content_hash,
            observed_at=snapshot.observed_at, text=visible)
