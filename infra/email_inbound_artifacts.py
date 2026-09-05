"""Gateway注入的中立Raw归档适配，不执行域规则或后台IO。"""

from __future__ import annotations

import hashlib

from artifact_store.store import (
    BoundedRawArtifactStore,
    RawArtifactKind,
    RawArtifactStore,
)
from shared.schemas.email_inbound import MIME_BYTES, ArchivedInboundRaw, InboundError
from shared.schemas.identifiers import TenantId


class InboundRawArtifactArchiver:
    """put后必须有界读取实际bytes；去重metadata不证明原件存在。"""

    def __init__(
        self, store: RawArtifactStore, bounded: BoundedRawArtifactStore
    ) -> None:
        self._store = store
        self._bounded = bounded

    async def archive(
        self, tenant_id: TenantId, raw_mime: bytes, *, maximum_bytes: int
    ) -> ArchivedInboundRaw:
        try:
            if (
                type(maximum_bytes) is not int
                or not 1 <= maximum_bytes <= MIME_BYTES
                or not 0 < len(raw_mime) <= maximum_bytes
            ):
                raise InboundError()
            meta = await self._store.put(
                tenant_id,
                RawArtifactKind.EMAIL_RAW,
                raw_mime,
                "message/rfc822",
                uploaded_by=None,
            )
            if (
                meta.tenant_id != tenant_id
                or meta.kind is not RawArtifactKind.EMAIL_RAW
                or meta.mime_type != "message/rfc822"
                or meta.size_bytes != len(raw_mime)
                or meta.content_hash != hashlib.sha256(raw_mime).hexdigest()
            ):
                raise InboundError()
            actual, content = await self._bounded.get_bounded(
                tenant_id, meta.artifact_id, maximum_bytes=maximum_bytes
            )
            if (
                actual != meta
                or content != raw_mime
                or len(content) != meta.size_bytes
                or hashlib.sha256(content).hexdigest() != meta.content_hash
            ):
                raise InboundError()
            return ArchivedInboundRaw(
                tenant_id=tenant_id,
                artifact_id=meta.artifact_id,
                content_hash=meta.content_hash,
                size_bytes=meta.size_bytes,
            )
        except Exception:  # noqa: BLE001 存储异常不包含连接、对象键或原文
            raise InboundError() from None
