"""跨域 Catalog 调度恢复只读 checkpoint 契约。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import TenantId

CatalogReconciliationCheckpointStream = Literal[
    "pending_policies",
    "awaiting_proposals",
    "catalog_clusters",
]

_ENTITY_PREFIX = {
    "pending_policies": "cpv_",
    "awaiting_proposals": "cpr_",
    "catalog_clusters": "ncl_",
}


class CatalogReconciliationCheckpoint(BaseModel):
    """仅保存稳定扫描位置和 CAS 版本，不包含任何业务正文。"""

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        revalidate_instances="always",
    )

    tenant_id: TenantId
    stream: CatalogReconciliationCheckpointStream
    position_at: datetime | None
    entity_id: str | None = Field(default=None, max_length=40)
    version: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_checkpoint(self) -> Self:
        tenant = str(self.tenant_id)
        if (
            not tenant.startswith("tn_")
            or tenant != tenant.strip()
            or not tenant
            or len(tenant) > 40
            or isinstance(self.version, bool)
            or (self.position_at is None) != (self.entity_id is None)
            or (self.version == 0 and self.position_at is not None)
        ):
            raise ValueError("Catalog 恢复 checkpoint 无效")
        if self.position_at is None:
            return self
        if (
            self.position_at.tzinfo is None
            or self.position_at.utcoffset() != UTC.utcoffset(self.position_at)
            or self.entity_id is None
            or not self.entity_id.startswith(_ENTITY_PREFIX[self.stream])
            or self.entity_id != self.entity_id.strip()
            or not self.entity_id
        ):
            raise ValueError("Catalog 恢复 checkpoint 无效")
        return self


__all__ = (
    "CatalogReconciliationCheckpoint",
    "CatalogReconciliationCheckpointStream",
)
