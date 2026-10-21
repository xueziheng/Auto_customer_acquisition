"""有界企业资料上下文：只经 Products 公共服务读取当前确认版本。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Protocol

from domains.products.schemas import KnowledgeActor
from domains.products.service import EnterpriseKnowledgeService
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId


class ConfirmedReference(Protocol):
    """读取调用方已验证的精确版本引用，避免依赖其他域的内部类型。"""

    @property
    def document_id(self) -> str: ...

    @property
    def revision_id(self) -> str: ...


class ConfirmedContextReader:
    """不扫描 vault、不缓存跨任务原文；调用方在每次模型调用前后重新读取。"""

    def __init__(self, service: EnterpriseKnowledgeService) -> None:
        self.service = service

    async def read(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        refs: Iterable[ConfirmedReference | Mapping[str, str]],
    ) -> list[dict[str, object]]:
        """完整保留有限事实、推断和来源；超预算拒绝，不静默截断商业字段。"""
        actor = KnowledgeActor(tenant_id=tenant_id, employee_id=employee_id)
        result: list[dict[str, object]] = []
        values = tuple(refs)
        if len(values) > 5:
            raise PermissionDenied("企业资料上下文无效")
        for ref in values:
            document_id = (
                ref["document_id"] if isinstance(ref, Mapping) else ref.document_id
            )
            revision_id = (
                ref["revision_id"] if isinstance(ref, Mapping) else ref.revision_id
            )
            detail = await self.service.get_detail(tenant_id, actor, document_id)
            doc, rev = detail.document, detail.revision
            if (
                doc.tenant_id != tenant_id
                or doc.document_id != document_id
                or doc.status != "confirmed"
                or rev is None
                or doc.current_revision_id != revision_id
                or rev.revision_id != revision_id
                or rev.document_id != document_id
                or rev.confirmed_by is None
                or rev.confirmed_at is None
            ):
                raise PermissionDenied("企业资料上下文已失效")
            result.append(
                {
                    "document_id": document_id,
                    "revision_id": revision_id,
                    "artifact_id": str(doc.source.artifact_id),
                    "sha256": doc.source.sha256,
                    "facts": [
                        item.model_dump(mode="json") for item in rev.analysis.facts
                    ],
                    "inferences": [
                        item.model_dump(mode="json") for item in rev.analysis.inferences
                    ],
                    "fact_provenance": [
                        item.model_dump(mode="json") for item in rev.fact_provenance
                    ],
                }
            )
            if len(json.dumps(result, ensure_ascii=False)) > 60000:
                raise PermissionDenied("企业资料上下文超过限制，请缩小选择范围")
        return result
