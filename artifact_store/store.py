"""原始资料存取接口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from shared.schemas.identifiers import ArtifactId, TenantId


@dataclass(frozen=True)
class ArtifactMeta:
    """资料元数据。

    字段：
        artifact_id, tenant_id
        kind:          email_raw / screenshot / pdf / excel /
                       web_snapshot / image / audio
        content_hash:  sha256。「当时看到的就是这个」的唯一证明
        size_bytes, mime_type
        uploaded_by, uploaded_at
        source_note:   来源说明
    """

    artifact_id: ArtifactId
    tenant_id: TenantId
    kind: str
    content_hash: str
    size_bytes: int
    mime_type: str
    uploaded_at: datetime
    uploaded_by: str | None = None
    source_note: str | None = None


@runtime_checkable
class ArtifactStore(Protocol):
    """存取接口。

    **没有 update / delete 方法**——不可变不是约定，是接口上
    做不到可变。（合规删除请求经专用管理路径处理并留审计。）
    """

    async def put(
        self,
        tenant_id: TenantId,
        kind: str,
        content: bytes,
        mime_type: str,
        uploaded_by: str | None = None,
    ) -> ArtifactMeta:
        """写入。算哈希；同租户同哈希已存在则返回既有记录（去重）。"""
        ...

    async def get(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> tuple[ArtifactMeta, bytes]:
        """读取并校验哈希。校验失败抛错并告警——对象存储损坏或
        被篡改都是要立刻知道的事。"""
        ...

    async def get_meta(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> ArtifactMeta: ...
