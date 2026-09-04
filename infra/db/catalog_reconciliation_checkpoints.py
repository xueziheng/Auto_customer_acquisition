"""Catalog scheduler 三流 checkpoint 的 tenant-bound PostgreSQL store。"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import CatalogReconciliationCheckpointRow
from shared.errors import TenantIsolationViolation, TransientError, ValidationError
from shared.schemas.catalog_reconciliation import (
    CatalogReconciliationCheckpoint,
    CatalogReconciliationCheckpointStream,
)
from shared.schemas.identifiers import TenantId


class PostgresCatalogReconciliationCheckpointStore:
    """每次 load/CAS 使用独立短事务；它不是 singleton 锁或 leader election。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
    ) -> None:
        if not callable(factory) or not self._valid_tenant(tenant_id):
            raise ValidationError("Catalog checkpoint store 依赖无效")
        self._factory = factory
        self._tenant_id = tenant_id

    @staticmethod
    def _valid_tenant(tenant_id: object) -> bool:
        return (
            isinstance(tenant_id, str)
            and tenant_id.startswith("tn_")
            and tenant_id == tenant_id.strip()
            and 0 < len(tenant_id) <= 40
        )

    def _require_scope(
        self,
        tenant_id: TenantId,
        stream: CatalogReconciliationCheckpointStream,
    ) -> None:
        if tenant_id != self._tenant_id:
            raise TenantIsolationViolation("Catalog checkpoint 不可跨租户访问")
        if stream not in {
            "pending_policies",
            "awaiting_proposals",
            "catalog_clusters",
        }:
            raise ValidationError("Catalog checkpoint stream 无效")

    @staticmethod
    def _from_row(
        row: CatalogReconciliationCheckpointRow,
    ) -> CatalogReconciliationCheckpoint:
        try:
            return CatalogReconciliationCheckpoint(
                tenant_id=TenantId(row.tenant_id),
                stream=cast(CatalogReconciliationCheckpointStream, row.stream),
                position_at=row.position_at,
                entity_id=row.entity_id,
                version=row.version,
            )
        except (PydanticValidationError, TypeError, ValueError):
            raise TransientError("Catalog checkpoint 存储事实无效") from None

    async def load(
        self,
        tenant_id: TenantId,
        stream: CatalogReconciliationCheckpointStream,
    ) -> CatalogReconciliationCheckpoint:
        self._require_scope(tenant_id, stream)
        try:
            async with self._factory.begin() as session:
                row = (
                    await session.execute(
                        select(CatalogReconciliationCheckpointRow).where(
                            CatalogReconciliationCheckpointRow.tenant_id
                            == str(self._tenant_id),
                            CatalogReconciliationCheckpointRow.stream == stream,
                        )
                    )
                ).scalar_one_or_none()
        except Exception as error:
            if isinstance(error, (TenantIsolationViolation, ValidationError)):
                raise
            raise TransientError("Catalog checkpoint 暂不可用") from None
        if row is None:
            return CatalogReconciliationCheckpoint(
                tenant_id=self._tenant_id,
                stream=stream,
                position_at=None,
                entity_id=None,
                version=0,
            )
        return self._from_row(row)

    async def compare_and_set(
        self,
        current: CatalogReconciliationCheckpoint,
        *,
        next_position_at: datetime | None,
        next_entity_id: str | None,
    ) -> CatalogReconciliationCheckpoint:
        if not isinstance(current, CatalogReconciliationCheckpoint):
            raise ValidationError("Catalog checkpoint 当前版本无效")
        self._require_scope(current.tenant_id, current.stream)
        if current.version == 0 and (
            current.position_at is not None or current.entity_id is not None
        ):
            raise ValidationError("Catalog checkpoint 初始版本无效")
        try:
            desired = CatalogReconciliationCheckpoint(
                tenant_id=self._tenant_id,
                stream=current.stream,
                position_at=next_position_at,
                entity_id=next_entity_id,
                version=current.version + 1,
            )
        except (PydanticValidationError, TypeError, ValueError):
            raise ValidationError("Catalog checkpoint 下一位置无效") from None

        try:
            async with self._factory.begin() as session:
                if current.version == 0:
                    result = await session.execute(
                        insert(CatalogReconciliationCheckpointRow)
                        .values(
                            tenant_id=str(self._tenant_id),
                            stream=current.stream,
                            position_at=desired.position_at,
                            entity_id=desired.entity_id,
                            version=desired.version,
                        )
                        .on_conflict_do_nothing(
                            constraint="pk_catalog_reconciliation_checkpoints"
                        )
                    )
                else:
                    predicates = [
                        CatalogReconciliationCheckpointRow.tenant_id
                        == str(self._tenant_id),
                        CatalogReconciliationCheckpointRow.stream == current.stream,
                        CatalogReconciliationCheckpointRow.version == current.version,
                        CatalogReconciliationCheckpointRow.position_at.is_(None)
                        if current.position_at is None
                        else CatalogReconciliationCheckpointRow.position_at
                        == current.position_at,
                        CatalogReconciliationCheckpointRow.entity_id.is_(None)
                        if current.entity_id is None
                        else CatalogReconciliationCheckpointRow.entity_id
                        == current.entity_id,
                    ]
                    result = await session.execute(
                        update(CatalogReconciliationCheckpointRow)
                        .where(*predicates)
                        .values(
                            position_at=desired.position_at,
                            entity_id=desired.entity_id,
                            version=desired.version,
                        )
                    )
                if cast(CursorResult[object], result).rowcount != 1:
                    raise TransientError("Catalog checkpoint 已变化")
        except Exception as error:
            if isinstance(
                error,
                (TenantIsolationViolation, ValidationError, TransientError),
            ):
                raise
            raise TransientError("Catalog checkpoint 暂不可用") from None
        return desired


__all__ = ("PostgresCatalogReconciliationCheckpointStore",)
