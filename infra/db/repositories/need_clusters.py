"""NeedCluster 的 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.models import NeedCluster
from domains.demand.repository import NeedClusterRepository
from infra.db.tables import NeedClusterMemberRow, NeedClusterRow
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    TenantId,
    ValidatedNeedId,
)

_logger = logging.getLogger("infra.db.repositories.need_clusters")


class NeedClusterRepositoryImpl(NeedClusterRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now or (lambda: datetime.now(UTC))

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id != self._tenant_id:
            _logger.critical(
                "检测到跨租户数据隔离违规",
                extra={"action": action, "tenant_id": str(self._tenant_id)},
            )
            raise TenantIsolationViolation("跨租户数据隔离违规")

    async def add(self, cluster: NeedCluster) -> None:
        self._require_tenant(cluster.tenant_id, "need_cluster_add")
        created_at = cluster.created_at or self._now()
        updated_at = cluster.updated_at or created_at
        self._session.add(
            NeedClusterRow(
                tenant_id=str(cluster.tenant_id),
                cluster_id=str(cluster.cluster_id),
                category=cluster.category,
                keywords=list(cluster.keywords),
                countries=list(cluster.countries),
                total_potential_quantity=cluster.total_potential_quantity,
                recurring_demand=cluster.recurring_demand,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        await self._session.flush()
        await self._insert_members(cluster, assigned_at=updated_at)

    async def get(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
    ) -> NeedCluster | None:
        self._require_tenant(tenant_id, "need_cluster_get")
        row = (
            await self._session.execute(
                select(NeedClusterRow).where(
                    NeedClusterRow.tenant_id == str(self._tenant_id),
                    NeedClusterRow.cluster_id == str(cluster_id),
                )
            )
        ).scalar_one_or_none()
        return await self._hydrate(row) if row is not None else None

    async def update(self, cluster: NeedCluster) -> None:
        self._require_tenant(cluster.tenant_id, "need_cluster_update")
        updated_at = cluster.updated_at or self._now()
        result = await self._session.execute(
            sa_update(NeedClusterRow)
            .where(
                NeedClusterRow.tenant_id == str(self._tenant_id),
                NeedClusterRow.cluster_id == str(cluster.cluster_id),
            )
            .values(
                category=cluster.category,
                keywords=list(cluster.keywords),
                countries=list(cluster.countries),
                total_potential_quantity=cluster.total_potential_quantity,
                recurring_demand=cluster.recurring_demand,
                updated_at=updated_at,
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise ValidationError("需求簇更新失败")
        await self._insert_members(cluster, assigned_at=updated_at)

    async def find_candidate_cluster(
        self,
        tenant_id: TenantId,
        category: str,
        keywords: list[str],
    ) -> NeedCluster | None:
        self._require_tenant(tenant_id, "need_cluster_find_candidate")
        rows = (
            await self._session.execute(
                select(NeedClusterRow)
                .where(
                    NeedClusterRow.tenant_id == str(self._tenant_id),
                    NeedClusterRow.category == category,
                )
                .order_by(NeedClusterRow.created_at, NeedClusterRow.cluster_id)
                .with_for_update()
            )
        ).scalars().all()
        incoming = set(keywords)
        for row in rows:
            stored = cast(list[str], row.keywords)
            if incoming and incoming.intersection(stored):
                return await self._hydrate(row)
        return None

    async def list_for_radar(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
    ) -> list[NeedCluster]:
        self._require_tenant(tenant_id, "need_cluster_list_for_radar")
        rows = (
            await self._session.execute(
                select(NeedClusterRow)
                .where(NeedClusterRow.tenant_id == str(self._tenant_id))
                .order_by(NeedClusterRow.updated_at.desc(), NeedClusterRow.cluster_id)
                .limit(limit)
            )
        ).scalars().all()
        return [await self._hydrate(row) for row in rows]

    async def _insert_members(
        self,
        cluster: NeedCluster,
        *,
        assigned_at: datetime,
    ) -> None:
        for need_id in dict.fromkeys(cluster.member_need_ids):
            await self._session.execute(
                pg_insert(NeedClusterMemberRow)
                .values(
                    tenant_id=str(self._tenant_id),
                    cluster_id=str(cluster.cluster_id),
                    need_id=str(need_id),
                    assigned_at=assigned_at,
                )
                .on_conflict_do_nothing(
                    index_elements=(
                        NeedClusterMemberRow.tenant_id,
                        NeedClusterMemberRow.need_id,
                    )
                )
            )

    async def _hydrate(self, row: NeedClusterRow) -> NeedCluster:
        members = (
            await self._session.execute(
                select(NeedClusterMemberRow.need_id)
                .where(
                    NeedClusterMemberRow.tenant_id == str(self._tenant_id),
                    NeedClusterMemberRow.cluster_id == row.cluster_id,
                )
                .order_by(
                    NeedClusterMemberRow.assigned_at,
                    NeedClusterMemberRow.need_id,
                )
            )
        ).scalars().all()
        return NeedCluster(
            cluster_id=NeedClusterId(row.cluster_id),
            tenant_id=TenantId(row.tenant_id),
            category=row.category,
            member_need_ids=[ValidatedNeedId(value) for value in members],
            keywords=list(cast(list[str], row.keywords)),
            countries=list(cast(list[str], row.countries)),
            total_potential_quantity=row.total_potential_quantity,
            recurring_demand=row.recurring_demand,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


__all__ = ("NeedClusterRepositoryImpl",)
