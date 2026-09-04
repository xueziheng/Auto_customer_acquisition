"""NeedCluster 的 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.models import NeedCluster
from domains.demand.repository import (
    NeedClusterCatalogSnapshot,
    NeedClusterRepository,
)
from domains.demand.schemas import CatalogClusterCursor, CatalogClusterIdPage
from infra.db.repositories.need_hypotheses import _row_to_need
from infra.db.tables import NeedClusterMemberRow, NeedClusterRow, ValidatedNeedRow
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    NeedClusterId,
    ProspectAccountId,
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

    async def get_catalog_snapshot(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
    ) -> NeedClusterCatalogSnapshot | None:
        """用一次联表读取同时取得 membership 正向集合和 Need 反向集合。"""
        self._require_tenant(tenant_id, "need_cluster_get_catalog_snapshot")
        member_ids = (
            select(func.array_agg(NeedClusterMemberRow.need_id))
            .where(
                NeedClusterMemberRow.tenant_id == str(self._tenant_id),
                NeedClusterMemberRow.cluster_id == str(cluster_id),
            )
            .scalar_subquery()
        )
        rows = (
            await self._session.execute(
                select(
                    NeedClusterRow,
                    ValidatedNeedRow,
                    member_ids.label("member_need_ids"),
                )
                .outerjoin(
                    ValidatedNeedRow,
                    and_(
                        ValidatedNeedRow.tenant_id == NeedClusterRow.tenant_id,
                        ValidatedNeedRow.cluster_id == NeedClusterRow.cluster_id,
                    ),
                )
                .where(
                    NeedClusterRow.tenant_id == str(self._tenant_id),
                    NeedClusterRow.cluster_id == str(cluster_id),
                )
                .order_by(ValidatedNeedRow.need_id)
            )
        ).all()
        if not rows:
            return None
        cluster_row = rows[0][0]
        raw_member_ids = rows[0][2] or []
        cluster = NeedCluster(
            cluster_id=NeedClusterId(cluster_row.cluster_id),
            tenant_id=TenantId(cluster_row.tenant_id),
            category=cluster_row.category,
            member_need_ids=[
                ValidatedNeedId(value) for value in raw_member_ids
            ],
            keywords=list(cast(list[str], cluster_row.keywords)),
            countries=list(cast(list[str], cluster_row.countries)),
            total_potential_quantity=cluster_row.total_potential_quantity,
            recurring_demand=cluster_row.recurring_demand,
            created_at=cluster_row.created_at,
            updated_at=cluster_row.updated_at,
        )
        needs = tuple(_row_to_need(row[1]) for row in rows if row[1] is not None)
        return NeedClusterCatalogSnapshot(cluster=cluster, needs=needs)

    async def list_catalog_cluster_ids(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
    ) -> tuple[NeedClusterId, ...]:
        self._require_tenant(tenant_id, "need_cluster_list_catalog_ids")
        values = (
            await self._session.execute(
                select(NeedClusterRow.cluster_id)
                .where(NeedClusterRow.tenant_id == str(self._tenant_id))
                .order_by(NeedClusterRow.updated_at.desc(), NeedClusterRow.cluster_id)
                .limit(limit)
            )
        ).scalars().all()
        return tuple(NeedClusterId(value) for value in values)

    async def list_catalog_cluster_id_page(
        self,
        tenant_id: TenantId,
        *,
        limit: int,
        cursor: CatalogClusterCursor | None = None,
    ) -> CatalogClusterIdPage:
        self._require_tenant(tenant_id, "need_cluster_list_catalog_id_page")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValidationError("目录需求簇页面 limit 必须为 1..200")
        if cursor is not None and (
            not isinstance(cursor, CatalogClusterCursor)
            or cursor.tenant_id != self._tenant_id
        ):
            raise TenantIsolationViolation("目录需求簇游标不可跨租户使用")
        statement = select(
            NeedClusterRow.cluster_id, NeedClusterRow.created_at
        ).where(NeedClusterRow.tenant_id == str(self._tenant_id))
        if cursor is not None:
            statement = statement.where(
                or_(
                    NeedClusterRow.created_at > cursor.created_at,
                    and_(
                        NeedClusterRow.created_at == cursor.created_at,
                        NeedClusterRow.cluster_id > str(cursor.cluster_id),
                    ),
                )
            )
        rows = list(
            (
                await self._session.execute(
                    statement.order_by(
                        NeedClusterRow.created_at.asc(),
                        NeedClusterRow.cluster_id.asc(),
                    ).limit(limit + 1)
                )
            ).all()
        )
        selected = rows[:limit]
        cluster_ids = tuple(NeedClusterId(row.cluster_id) for row in selected)
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            next_cursor = CatalogClusterCursor(
                tenant_id=self._tenant_id,
                created_at=last.created_at,
                cluster_id=NeedClusterId(last.cluster_id),
            )
        return CatalogClusterIdPage(
            tenant_id=self._tenant_id,
            cluster_ids=cluster_ids,
            next_cursor=next_cursor,
        )

    async def list_catalog_cluster_ids_for_account(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        limit: int,
    ) -> tuple[NeedClusterId, ...]:
        self._require_tenant(tenant_id, "need_cluster_list_catalog_ids_for_account")
        values = (
            await self._session.execute(
                select(NeedClusterRow.cluster_id)
                .join(
                    ValidatedNeedRow,
                    and_(
                        ValidatedNeedRow.tenant_id == NeedClusterRow.tenant_id,
                        ValidatedNeedRow.cluster_id == NeedClusterRow.cluster_id,
                    ),
                )
                .where(
                    NeedClusterRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.tenant_id == str(self._tenant_id),
                    ValidatedNeedRow.account_id == str(account_id),
                )
                .group_by(NeedClusterRow.cluster_id, NeedClusterRow.updated_at)
                .order_by(NeedClusterRow.updated_at.desc(), NeedClusterRow.cluster_id)
                .limit(limit)
            )
        ).scalars().all()
        return tuple(NeedClusterId(value) for value in values)

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
