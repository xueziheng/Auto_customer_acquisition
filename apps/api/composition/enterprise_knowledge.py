"""企业资料 HTTP 编排：原件授权上传、有界读取与共享资料域接线。"""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Protocol

from artifact_store.store import (
    BoundedRawArtifactStore,
    RawArtifactKind,
    RawArtifactMeta,
    RawArtifactStore,
)
from domains.products.schemas import (
    KnowledgeActor,
    KnowledgeDocumentView,
    KnowledgeSource,
)
from domains.products.service import EnterpriseKnowledgeService
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, UserId

MAXIMUM_KNOWLEDGE_UPLOAD_BYTES = 10 * 1024 * 1024
_MIME_BY_EXTENSION = {
    "txt": ("text/plain", "text"),
    "md": ("text/markdown", "text"),
    "csv": ("text/csv", "excel"),
    "pdf": ("application/pdf", "pdf"),
    "png": ("image/png", "image"),
    "jpg": ("image/jpeg", "image"),
    "jpeg": ("image/jpeg", "image"),
    "webp": ("image/webp", "image"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "word"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "excel"),
}

KNOWLEDGE_UPLOAD_MIME_TYPES = frozenset(mime for mime, _kind in _MIME_BY_EXTENSION.values())


class KnowledgeArtifactStore(RawArtifactStore, BoundedRawArtifactStore, Protocol):
    """组合已有原件写入与有界读取端口，不提供目录或跨企业查询。"""


def validate_knowledge_file(filename: str, mime_type: str) -> RawArtifactKind:
    """仅校验HTTP文件名称与格式契约，内容提取由后台受限解析器完成。"""
    if (
        not filename or len(filename) > 240 or filename != filename.strip()
        or filename.startswith(".") or filename.casefold() == "agents.md"
        or any(c in filename for c in ("/", "\\"))
        or any(unicodedata.category(c).startswith("C") for c in filename)
    ):
        raise ValidationError("资料文件名无效")
    expected = _MIME_BY_EXTENSION.get(filename.rsplit(".", 1)[-1].lower())
    if expected is None or mime_type != expected[0]:
        raise ValidationError("资料类型不支持或与文件名不一致")
    return RawArtifactKind(expected[1])


class EnterpriseKnowledgeApplication:
    """HTTP不执行模型、解析器或文件投影；只保存原件并操作持久任务。"""

    def __init__(self, service: EnterpriseKnowledgeService, artifacts: KnowledgeArtifactStore, *, maximum_upload_bytes: int) -> None:
        if type(maximum_upload_bytes) is not int or not 0 < maximum_upload_bytes <= MAXIMUM_KNOWLEDGE_UPLOAD_BYTES:
            raise ValueError("企业资料上传上限无效")
        self.service = service
        self._artifacts = artifacts
        self.maximum_upload_bytes = maximum_upload_bytes

    async def upload(self, tenant_id: TenantId, actor: KnowledgeActor, *, uploaded_by: UserId | None, filename: str, mime_type: str, content: bytes, idempotency_key: str) -> KnowledgeDocumentView:
        """当前员工先授权，保存不可变原件，再由域重新授权并登记幂等任务。"""
        await self.service.authorize_upload(tenant_id, actor)
        kind = validate_knowledge_file(filename, mime_type)
        if not content or len(content) > self.maximum_upload_bytes:
            raise ValidationError("企业资料大小无效")
        if mime_type in {"text/plain", "text/markdown", "text/csv"}:
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValidationError("文本资料必须使用UTF-8编码") from None
        elif kind is RawArtifactKind.PDF and not content.startswith(b"%PDF-"):
            raise ValidationError("PDF资料格式无效")
        elif kind in {RawArtifactKind.WORD, RawArtifactKind.EXCEL} and mime_type != "text/csv" and not content.startswith(b"PK"):
            raise ValidationError("Office资料格式无效")
        elif kind is RawArtifactKind.IMAGE:
            matches = (
                (mime_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime_type == "image/jpeg" and content.startswith(b"\xff\xd8\xff"))
                or (mime_type == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
            )
            if not matches:
                raise ValidationError("图片资料格式无效")
        metadata = await self._artifacts.put(tenant_id, kind, content, mime_type, uploaded_by)
        source = KnowledgeSource(
            filename=filename, mime_type=metadata.mime_type, size_bytes=metadata.size_bytes,
            sha256=metadata.content_hash, artifact_id=metadata.artifact_id,
        )
        return await self.service.register_upload(tenant_id, actor, source, idempotency_key=idempotency_key)

    async def source(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str) -> tuple[str, RawArtifactMeta, bytes]:
        """先按共享资料权限查记录，再有界读原件并复核来源和当前授权。"""
        detail = await self.service.get_detail(tenant_id, actor, document_id)
        source = detail.document.source
        meta, content = await self._artifacts.get_bounded(
            tenant_id, source.artifact_id, maximum_bytes=self.maximum_upload_bytes,
        )
        if (
            meta.tenant_id != tenant_id or meta.artifact_id != source.artifact_id
            or meta.content_hash != source.sha256 or meta.size_bytes != source.size_bytes
            or meta.mime_type != source.mime_type
            or len(content) != source.size_bytes
            or hashlib.sha256(content).hexdigest() != source.sha256
        ):
            raise ValidationError("资料原件完整性校验失败")
        await self.service.get_detail(tenant_id, actor, document_id)
        return source.filename, meta, content
