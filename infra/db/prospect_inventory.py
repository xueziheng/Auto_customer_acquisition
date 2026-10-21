"""真实Postgres行锁仓储；公共控制面与公司会话分别绑定，不切换租户角色。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.inventory import (
    DEFAULT_DAILY_LIMIT,
    AcquisitionJob,
    CandidateAllocation,
    InventoryQuery,
    InventoryResult,
    InventoryStatus,
    InventoryTransferRequest,
    PublicCandidate,
    PublicCatalog,
    SourceHistory,
    matches_query,
    plan_allocations,
    validate_assignment_ids,
    validate_request_count,
)
from domains.prospecting.inventory_fit import (
    MAX_FIT_BATCHES,
    MAX_FIT_CANDIDATES,
    MAX_FIT_EVIDENCE_BYTES,
    FitCandidateVersion,
    InventoryFitAssessment,
    InventoryFitBatch,
    candidate_evidence_version,
    fit_evidence_inputs,
    fit_query_fingerprint,
    selected_fit_candidates,
)
from domains.prospecting.projection_diagnostic import SaveSummary, SourceDiagnostic
from infra.db.base import TenantScopedRepository
from infra.db.prospect_inventory_tables import (
    AcquisitionAttemptRow,
    AcquisitionJobRow,
    AllocationEmailRow,
    CandidateAllocationRow,
    InventoryQuotaRow,
    InventoryRequestRow,
    PublicCandidateRow,
)
from infra.db.tables import EmployeeRow, OwnershipLockRow, ProspectAccountRow
from shared.errors import IdempotencyConflict, PermissionDenied
from shared.schemas.discovery_diagnostic import is_discovery_failure
from shared.schemas.discovery_sources import get_discovery_source
from shared.schemas.identifiers import (
    AcquisitionJobId,
    CandidateAllocationId,
    EmployeeId,
    ProspectAccountId,
    TenantId,
    new_id,
)


def _digest(value: str) -> str:
    """幂等键不保留潜在敏感原始输入。"""
    return hashlib.sha256(value.encode()).hexdigest()


class InventoryAssignmentBridge(Protocol):
    """应用装配同事务潜客与员工公共服务，不持有第二个独立事务。"""

    async def bind(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        allocation: CandidateAllocation,
        employee_id: EmployeeId,
        acting_employee_id: EmployeeId,
    ) -> ProspectAccountId:
        """在已有事务创建/复用私有客户及归属锁；失败让整批分配回滚。"""
        ...

    async def transfer(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        allocation: CandidateAllocation,
        target: EmployeeId,
        acting: EmployeeId,
        reason: str,
    ) -> None:
        """已有账户在调用方事务内通过员工域转交并留痕。"""
        ...


class PostgresPublicCatalog(TenantScopedRepository):
    """只能由受信采集器装配的控制面仓储；业务HTTP不暴露写端口。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        control_tenant: TenantId,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(control_tenant)
        self.factory, self.now = factory, now

    async def fresh(
        self, query: InventoryQuery | None, now: datetime
    ) -> tuple[PublicCandidate, ...]:
        """控制租户查询只返回重新校验的新鲜公共投影。"""
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    self.scoped_query(PublicCandidateRow)
                    .where(PublicCandidateRow.expires_at > now)
                    .order_by(PublicCandidateRow.candidate_id)
                )
            ).all()
            values = tuple(PublicCandidate.model_validate(row.payload) for row in rows)
        return tuple(
            v
            for v in values
            if v.observed_at <= now and (query is None or matches_query(v, query))
        )

    async def save_public(self, candidates: tuple[PublicCandidate, ...]) -> int:
        """返回本次原子新增的新鲜域名数；刷新与过期资料不计新增。"""
        return (await self._save_public(candidates, None))["inserted_fresh"]

    async def save_public_report(
        self, candidates: tuple[PublicCandidate, ...], query: InventoryQuery
    ) -> SaveSummary:
        """同一保存分支计算有界统计；不改变新增或刷新谓词。"""
        if len(candidates) > 100:
            raise ValueError("采集候选超限")
        return SaveSummary.model_validate(await self._save_public(candidates, query))

    async def _save_public(
        self, candidates: tuple[PublicCandidate, ...], query: InventoryQuery | None
    ) -> dict[str, int]:
        """兼容既有批量写入；统计仅用于受信诊断端口。"""
        counts = {name: 0 for name in SaveSummary.model_fields}
        counts["total_count"] = len(candidates)
        async with self.factory() as session, session.begin():
            for candidate in candidates:
                v = PublicCandidate.model_validate(candidate.model_dump())
                stmt = insert(PublicCandidateRow).values(
                    tenant_id=self._tenant_id,
                    candidate_id=v.id,
                    canonical_domain=v.canonical_domain,
                    payload=v.model_dump(mode="json"),
                    expires_at=v.expires_at,
                )
                inserted = await session.scalar(
                    stmt.on_conflict_do_nothing(
                        constraint="uq_public_candidate_domain"
                    ).returning(PublicCandidateRow.candidate_id)
                )
                now = self.now()
                fresh = v.observed_at <= now < v.expires_at
                counts[
                    "fresh"
                    if fresh
                    else "future_observed"
                    if v.observed_at > now
                    else "expired"
                ] += 1
                counts[
                    "query_match"
                    if query is None or matches_query(v, query)
                    else "query_mismatch"
                ] += 1
                if inserted is not None:
                    counts["inserted_fresh" if fresh else "inserted_not_fresh"] += 1
                    continue
                updated = await session.scalar(
                    stmt.on_conflict_do_update(
                        constraint="uq_public_candidate_domain",
                        set_={
                            "payload": stmt.excluded.payload.op("||")(
                                func.jsonb_build_object(
                                    "id", PublicCandidateRow.candidate_id
                                )
                            ),
                            "expires_at": stmt.excluded.expires_at,
                        },
                        where=(PublicCandidateRow.tenant_id == self._tenant_id)
                        & (
                            PublicCandidateRow.payload["observed_at"].astext.cast(
                                DateTime(timezone=True)
                            )
                            <= stmt.excluded.payload["observed_at"].astext.cast(
                                DateTime(timezone=True)
                            )
                        ),
                    ).returning(PublicCandidateRow.candidate_id)
                )
                counts[
                    "existing_refreshed"
                    if updated is not None
                    else "existing_unchanged"
                ] += 1
        return counts


class PostgresInventoryService(TenantScopedRepository):
    """公司事务内统一锁额度行，实现跨员工去重、配额和幂等。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        business_tenant: TenantId,
        catalog: PublicCatalog,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        timezone: str = "Asia/Shanghai",
        assignment_bridge: InventoryAssignmentBridge | None = None,
    ) -> None:
        super().__init__(business_tenant)
        self.factory, self.catalog, self.now = factory, catalog, now
        self.zone = ZoneInfo(timezone)
        self.tenant_id = business_tenant
        self.assignment_bridge = assignment_bridge

    def _check(self, tenant_id: TenantId) -> None:
        """显式拒绝调用方与绑定租户不一致。"""
        if tenant_id != self._tenant_id:
            raise ValueError("企业隔离拒绝")

    async def _quota(self, session: AsyncSession, now: datetime) -> InventoryQuotaRow:
        """先锁公司额度；任何请求或worker都使用同一锁顺序。"""
        day = now.astimezone(self.zone).date()
        await session.execute(
            insert(InventoryQuotaRow)
            .values(
                tenant_id=self._tenant_id,
                daily_limit=DEFAULT_DAILY_LIMIT,
                quota_day=day,
                used_today=0,
            )
            .on_conflict_do_nothing()
        )
        row = (
            await session.scalars(
                self.scoped_query(InventoryQuotaRow).with_for_update()
            )
        ).one()
        day = self.now().astimezone(self.zone).date()
        if row.quota_day != day:
            row.quota_day, row.used_today = day, 0
        return row

    async def status(self, tenant_id: TenantId) -> InventoryStatus:
        """返回按公司时区重置的配额和新鲜公共库存数。"""
        self._check(tenant_id)
        now = self.now()
        fresh = await self.catalog.fresh(None, now)
        async with self.factory() as session, session.begin():
            quota = await self._quota(session, now)
            reset = datetime.combine(
                quota.quota_day + timedelta(days=1), time(), self.zone
            ).astimezone(UTC)
            return InventoryStatus(
                tenant_id=tenant_id,
                daily_limit=quota.daily_limit,
                used_today=quota.used_today,
                remaining_today=max(0, quota.daily_limit - quota.used_today),
                reset_at=reset,
                fresh_candidates=len(fresh),
            )

    async def _allocate(
        self,
        session: AsyncSession,
        quota: InventoryQuotaRow,
        employee_id: EmployeeId,
        key: str,
        candidates: tuple[PublicCandidate, ...],
        count: int,
        now: datetime,
    ) -> int:
        """公司锁下取得待分配客户；搜索者不是负责人，邮箱冲突不会部分写入。"""
        domains = set(
            (
                await session.scalars(
                    select(CandidateAllocationRow.canonical_domain).where(
                        CandidateAllocationRow.tenant_id == self._tenant_id
                    )
                )
            ).all()
        )
        emails = set(
            (
                await session.scalars(
                    select(AllocationEmailRow.email).where(
                        AllocationEmailRow.tenant_id == self._tenant_id
                    )
                )
            ).all()
        )
        delivered = 0
        now = self.now()
        # 同一个交付时间样本确定额度日与allocated_at，持锁期间也可能跨午夜。
        day = now.astimezone(self.zone).date()
        if quota.quota_day != day:
            quota.quota_day, quota.used_today = day, 0
        for candidate in plan_allocations(
            candidates, domains, emails, count, quota.daily_limit, quota.used_today, now
        ):
            allocation = CandidateAllocation(
                tenant_id=self._tenant_id,
                id=CandidateAllocationId(new_id("cal")),
                employee_id=None,
                candidate=candidate,
                allocated_at=now,
                request_key=key,
            )
            session.add(
                CandidateAllocationRow(
                    tenant_id=self._tenant_id,
                    allocation_id=allocation.id,
                    canonical_domain=candidate.canonical_domain,
                    employee_id=None,
                    key_hash=key,
                    allocated_at=now,
                    payload=allocation.model_dump(mode="json"),
                )
            )
            await session.flush()
            for email in candidate.public_emails:
                session.add(
                    AllocationEmailRow(
                        tenant_id=self._tenant_id,
                        email=email,
                        allocation_id=allocation.id,
                    )
                )
            domains.add(candidate.canonical_domain)
            emails.update(candidate.public_emails)
            delivered += 1
        quota.used_today += delivered
        await session.flush()
        return delivered

    async def _result(
        self, session: AsyncSession, key: str, job: AcquisitionJobRow | None
    ) -> InventoryResult:
        """幂等重放读取持久事实，包含后来worker交付的候选。"""
        rows = (
            await session.scalars(
                self.scoped_query(CandidateAllocationRow)
                .where(CandidateAllocationRow.key_hash == key)
                .order_by(CandidateAllocationRow.allocation_id)
            )
        ).all()
        return InventoryResult(
            allocations=tuple(self._allocation(r) for r in rows),
            job=self._job(job) if job else None,
        )

    @staticmethod
    def _allocation(row: CandidateAllocationRow) -> CandidateAllocation:
        """归属与关联字段由持久列投影，旧JSON缺少新字段仍保留历史负责人。"""
        return CandidateAllocation.model_validate(
            {
                **row.payload,
                "employee_id": row.employee_id,
                "assigned_at": row.assigned_at,
                "assigned_by": row.assigned_by,
                "account_id": row.account_id,
            }
        )

    @staticmethod
    def _job(row: AcquisitionJobRow) -> AcquisitionJob:
        """明确投影任务字段，不暴露仓储对象。"""
        return AcquisitionJob.model_validate(
            {
                "tenant_id": row.tenant_id,
                "id": row.job_id,
                "employee_id": row.employee_id,
                "query": row.query,
                "fit_batches": row.fit_batches or [],
                "source_plan": row.source_plan or [],
                "collection_deadline": row.collection_deadline,
                "collection_reserved_seconds": row.collection_reserved_seconds,
                "requested_count": row.requested_count,
                "remaining_count": row.remaining_count,
                "status": row.status,
                "attempts": row.attempts,
                "lease_until": row.lease_until,
                "lease_token": row.lease_token,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "failure_reason": row.failure_reason,
            }
        )

    async def request(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        query: InventoryQuery,
        count: int,
        request_key: str,
    ) -> InventoryResult:
        """公共池优先，公司原子分配与不足排队；参数冲突不可静默重放。"""
        self._check(tenant_id)
        validate_request_count(count)
        if not employee_id.strip() or not request_key.strip() or len(request_key) > 200:
            raise ValueError("员工与请求键必须有效")
        query = InventoryQuery.model_validate(query.model_dump())
        key = _digest(request_key)
        fingerprint = _digest(
            json.dumps(
                [
                    employee_id,
                    {
                        key: value
                        for key, value in query.model_dump(mode="json").items()
                        if key
                        not in {"regions", "countries", "intent", "confirmed_refs"}
                        or value
                    },
                    count,
                ],
                sort_keys=True,
            )
        )
        now = self.now()
        fresh = (
            ()
            if query.requires_private_fit
            else await self.catalog.fresh(query.public_projection(), now)
        )
        async with self.factory() as session, session.begin():
            quota = await self._quota(session, now)
            existing = (
                await session.scalars(
                    self.scoped_query(InventoryRequestRow).where(
                        InventoryRequestRow.key_hash == key
                    )
                )
            ).one_or_none()
            if existing:
                if existing.request_hash != fingerprint:
                    raise IdempotencyConflict("幂等请求参数冲突")
                job = (
                    (
                        await session.scalars(
                            self.scoped_query(AcquisitionJobRow).where(
                                AcquisitionJobRow.job_id == existing.job_id
                            )
                        )
                    ).one_or_none()
                    if existing.job_id
                    else None
                )
                return await self._result(session, key, job)
            req = InventoryRequestRow(
                tenant_id=tenant_id, key_hash=key, request_hash=fingerprint
            )
            session.add(req)
            await session.flush()
            delivered = await self._allocate(
                session, quota, employee_id, key, fresh, count, now
            )
            job = None
            if delivered < count:
                job = AcquisitionJobRow(
                    tenant_id=tenant_id,
                    job_id=new_id("acj"),
                    key_hash=key,
                    employee_id=employee_id,
                    query=query.model_dump(mode="json"),
                    requested_count=count,
                    remaining_count=count - delivered,
                    status="queued",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
                req.job_id = job.job_id
                session.add(job)
                await session.flush()
            return await self._result(session, key, job)

    async def list_allocations(
        self,
        tenant_id: TenantId,
        allowed_owner_ids: tuple[EmployeeId, ...] | None,
        limit: int,
        cursor: str | None = None,
        *,
        unassigned_only: bool = False,
    ) -> tuple[CandidateAllocation, ...]:
        """按时间降序及唯一ID升序分页；游标锚点仍须属于当前租户和归属范围。"""
        self._check(tenant_id)
        if not 1 <= limit <= 1000:
            raise ValueError("分页数量必须在1至1000")
        query = self.scoped_query(CandidateAllocationRow)
        if allowed_owner_ids is not None:
            query = query.where(
                CandidateAllocationRow.employee_id.in_(allowed_owner_ids)
            )
        if unassigned_only:
            query = query.where(CandidateAllocationRow.employee_id.is_(None))
        async with self.factory() as session:
            if cursor is not None:
                anchor = (
                    await session.scalars(
                        query.where(CandidateAllocationRow.allocation_id == cursor)
                    )
                ).one_or_none()
                if anchor is None:
                    raise ValueError("无效的历史游标")
                query = query.where(
                    or_(
                        CandidateAllocationRow.allocated_at < anchor.allocated_at,
                        (CandidateAllocationRow.allocated_at == anchor.allocated_at)
                        & (CandidateAllocationRow.allocation_id > anchor.allocation_id),
                    )
                )
            rows = (
                await session.scalars(
                    query.order_by(
                        CandidateAllocationRow.allocated_at.desc(),
                        CandidateAllocationRow.allocation_id,
                    ).limit(limit)
                )
            ).all()
            return tuple(self._allocation(r) for r in rows)

    async def get_allocation(
        self, tenant_id: TenantId, allocation_id: CandidateAllocationId
    ) -> CandidateAllocation | None:
        """租户绑定精确读取，不依赖列表页大小或游标位置。"""
        self._check(tenant_id)
        async with self.factory() as session:
            row = (
                await session.scalars(
                    self.scoped_query(CandidateAllocationRow).where(
                        CandidateAllocationRow.allocation_id == allocation_id
                    )
                )
            ).one_or_none()
            return self._allocation(row) if row else None

    async def assign(
        self,
        tenant_id: TenantId,
        allocation_ids: tuple[CandidateAllocationId, ...],
        employee_id: EmployeeId,
        *,
        acting_employee_id: EmployeeId,
    ) -> tuple[CandidateAllocation, ...]:
        """额度→员工→取得记录顺序锁定；整批校验后分配，冲突禁止覆盖。"""
        self._check(tenant_id)
        validate_assignment_ids(allocation_ids)
        async with self.factory() as session, session.begin():
            await self._quota(session, self.now())
            employees = (
                await session.scalars(
                    self.scoped_query(EmployeeRow)
                    .where(
                        EmployeeRow.employee_id.in_({employee_id, acting_employee_id})
                    )
                    .order_by(EmployeeRow.employee_id)
                    # NO KEY UPDATE仍阻止角色/在职更改，兼容归属外键的KEY SHARE。
                    .with_for_update(key_share=True)
                )
            ).all()
            current = {row.employee_id: row for row in employees}
            actor, target = current.get(acting_employee_id), current.get(employee_id)
            if (
                actor is None
                or not actor.is_active
                or actor.role != "boss"
                or target is None
                or not target.is_active
                or target.role not in {"boss", "manager", "sales"}
            ):
                raise PermissionDenied("分配必须由当前管理员面向同公司在职员工")
            rows = (
                await session.scalars(
                    self.scoped_query(CandidateAllocationRow)
                    .where(CandidateAllocationRow.allocation_id.in_(allocation_ids))
                    .order_by(CandidateAllocationRow.allocation_id)
                    .with_for_update()
                )
            ).all()
            if len(rows) != len(allocation_ids):
                raise PermissionDenied("分配记录不存在或不属于当前公司")
            if any(row.employee_id not in (None, employee_id) for row in rows):
                raise IdempotencyConflict("客户已有其他负责人")
            # 私有客户已存在时还必须锁其归属，不能让库存产生第二位负责人。
            accounts = (
                await session.scalars(
                    self.scoped_query(ProspectAccountRow)
                    .where(
                        ProspectAccountRow.website_domain.in_(
                            r.canonical_domain for r in rows
                        )
                    )
                    .order_by(ProspectAccountRow.account_id)
                    .with_for_update()
                )
            ).all()
            locks = (
                await session.scalars(
                    self.scoped_query(OwnershipLockRow)
                    .where(
                        OwnershipLockRow.account_id.in_(a.account_id for a in accounts)
                    )
                    .order_by(OwnershipLockRow.account_id)
                    .with_for_update()
                )
            ).all()
            if any(lock.owner != employee_id for lock in locks):
                raise IdempotencyConflict("客户已有其他负责人")
            now = self.now()
            for row in rows:
                value = self._allocation(row)
                if row.account_id is None and self.assignment_bridge is not None:
                    row.account_id = await self.assignment_bridge.bind(
                        session, tenant_id, value, employee_id, acting_employee_id
                    )
                if row.employee_id is None:
                    row.employee_id = employee_id
                    row.assigned_at, row.assigned_by = now, acting_employee_id
                row.payload = self._allocation(row).model_dump(mode="json")
            await session.flush()
            result = {row.allocation_id: self._allocation(row) for row in rows}
            return tuple(result[value] for value in allocation_ids)

    async def transfer(
        self,
        tenant_id: TenantId,
        allocation_id: CandidateAllocationId,
        command: InventoryTransferRequest,
        *,
        acting_employee_id: EmployeeId,
    ) -> CandidateAllocation:
        """复用分配的锁顺序，比较旧归属后调用同事务转交桥接。"""
        self._check(tenant_id)
        if self.assignment_bridge is None:
            raise PermissionDenied("客户转交尚未配置")
        async with self.factory() as session, session.begin():
            await self._quota(session, self.now())
            employees = (
                await session.scalars(
                    self.scoped_query(EmployeeRow)
                    .where(
                        EmployeeRow.employee_id.in_(
                            {
                                acting_employee_id,
                                command.employee_id,
                                command.expected_employee_id,
                            }
                        )
                    )
                    .order_by(EmployeeRow.employee_id)
                    .with_for_update(key_share=True)
                )
            ).all()
            current = {row.employee_id: row for row in employees}
            actor, target = (
                current.get(acting_employee_id),
                current.get(command.employee_id),
            )
            if (
                actor is None
                or not actor.is_active
                or actor.role != "boss"
                or target is None
                or not target.is_active
                or target.role not in {"boss", "manager", "sales"}
            ):
                raise PermissionDenied("仅管理员可转交给同公司在职员工")
            row = (
                await session.scalars(
                    self.scoped_query(CandidateAllocationRow)
                    .where(CandidateAllocationRow.allocation_id == allocation_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise PermissionDenied("客户不存在或不属于当前公司")
            if (
                row.employee_id != command.expected_employee_id
                or row.assigned_at != command.expected_assigned_at
            ):
                raise IdempotencyConflict("客户归属已改变，请刷新后核对")
            if row.account_id is None:
                raise IdempotencyConflict("请先关联客户档案再转交")
            await self.assignment_bridge.transfer(
                session,
                tenant_id,
                self._allocation(row),
                command.employee_id,
                acting_employee_id,
                command.reason,
            )
            row.employee_id = command.employee_id
            # 即使测试时钟固定，分配版本也须递增，避免A→B→A后的旧命令再次生效。
            row.assigned_at = (
                max(self.now(), row.assigned_at + timedelta(microseconds=1))
                if row.assigned_at
                else self.now()
            )
            row.assigned_by = acting_employee_id
            row.payload = self._allocation(row).model_dump(mode="json")
            await session.flush()
            return self._allocation(row)

    async def get_job(
        self, tenant_id: TenantId, job_id: AcquisitionJobId
    ) -> AcquisitionJob | None:
        """绑定公司读取单个任务。"""
        self._check(tenant_id)
        async with self.factory() as session:
            row = (
                await session.scalars(
                    self.scoped_query(AcquisitionJobRow).where(
                        AcquisitionJobRow.job_id == job_id
                    )
                )
            ).one_or_none()
            return self._job(row) if row else None

    async def set_daily_limit(self, tenant_id: TenantId, daily_limit: int) -> None:
        """平台授权后修改目标公司额度；允许低于当天已用量，剩余额度归零。"""
        self._check(tenant_id)
        if (
            isinstance(daily_limit, bool)
            or not isinstance(daily_limit, int)
            or daily_limit < 0
        ):
            raise ValueError("日额度必须为非负整数")
        async with self.factory() as session, session.begin():
            row = await self._quota(session, self.now())
            row.daily_limit = daily_limit

    async def claim_job(
        self, tenant_id: TenantId, lease_duration: timedelta
    ) -> AcquisitionJob | None:
        """领取排队或过期任务；SKIP LOCKED与每次新令牌防重启重复执行。"""
        self._check(tenant_id)
        if lease_duration <= timedelta(0):
            raise ValueError("租约必须为正")
        now = self.now()
        async with self.factory() as session, session.begin():
            row = (
                await session.scalars(
                    self.scoped_query(AcquisitionJobRow)
                    .where(
                        or_(
                            AcquisitionJobRow.status == "queued",
                            (AcquisitionJobRow.status == "processing")
                            & (AcquisitionJobRow.lease_until <= now),
                        )
                    )
                    .order_by(AcquisitionJobRow.created_at, AcquisitionJobRow.job_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).one_or_none()
            if row is None:
                return None
            row.status, row.lease_until, row.lease_token = (
                "processing",
                now + lease_duration,
                new_id("acl"),
            )
            row.updated_at = now
            row.attempts += 1
            return self._job(row)

    async def _leased(
        self, session: AsyncSession, job_id: AcquisitionJobId, token: str, now: datetime
    ) -> AcquisitionJobRow:
        """持锁核验fencing，过期worker不得记录或交付。"""
        row = (
            await session.scalars(
                self.scoped_query(AcquisitionJobRow)
                .where(AcquisitionJobRow.job_id == job_id)
                .with_for_update()
            )
        ).one_or_none()
        if (
            row is None
            or row.status != "processing"
            or row.lease_token != token
            or row.lease_until is None
            or row.lease_until <= self.now()
        ):
            raise ValueError("任务租约无效或已过期")
        return row

    async def save_collected(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        candidates: tuple[PublicCandidate, ...],
    ) -> SaveSummary:
        """持公司job行锁核验fence后写公共池；两库事务独立，崩溃不重派外部调用。"""
        self._check(tenant_id)
        async with self.factory() as session, session.begin():
            job = await self._leased(session, job_id, lease_token, self.now())
            return await self.catalog.save_public_report(
                candidates, InventoryQuery.model_validate(job.query).public_projection()
            )

    async def initialize_source_plan(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        plan: tuple[str, ...],
    ) -> AcquisitionJob:
        """首次派发前固定计划与恢复期限；旧无计划重领任务保守拒绝。"""
        self._check(tenant_id)
        if (
            not 1 <= len(plan) <= 3
            or len(set(plan)) != len(plan)
            or any(get_discovery_source(source) is None for source in plan)
        ):
            raise ValueError("来源计划无效")
        async with self.factory() as session, session.begin():
            row = await self._leased(session, job_id, lease_token, self.now())
            if not row.source_plan:
                if row.attempts > 1:
                    raise ValueError("旧任务无法证明尚未派发")
                row.source_plan = list(plan)
                row.collection_deadline = self.now() + timedelta(hours=24)
            return self._job(row)

    async def source_progress(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
    ) -> dict[str, str]:
        """读取同一任务现有尝试账本，started 永远视为未知已消耗。"""
        self._check(tenant_id)
        async with self.factory() as session, session.begin():
            await self._leased(session, job_id, lease_token, self.now())
            rows = (
                await session.scalars(
                    self.scoped_query(AcquisitionAttemptRow).where(
                        AcquisitionAttemptRow.job_id == job_id
                    )
                )
            ).all()
            return {row.source_id: row.outcome for row in rows}

    async def reserve_source(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        source_id: str,
    ) -> int:
        """先持锁扣留时间和started，再允许外部IO；取消或未知结果不退款。"""
        self._check(tenant_id)
        async with self.factory() as session, session.begin():
            row = await self._leased(session, job_id, lease_token, self.now())
            if (
                source_id not in row.source_plan
                or row.collection_deadline is None
                or row.collection_deadline <= self.now()
            ):
                raise ValueError("来源计划已失效")
            prior = (
                await session.scalars(
                    self.scoped_query(AcquisitionAttemptRow).where(
                        AcquisitionAttemptRow.job_id == job_id,
                        AcquisitionAttemptRow.source_id == source_id,
                    )
                )
            ).first()
            seconds = 480 // len(row.source_plan)
            if prior is not None or row.collection_reserved_seconds + seconds > 480:
                raise ValueError("来源已派发或预算耗尽")
            row.collection_reserved_seconds += seconds
            session.add(
                AcquisitionAttemptRow(
                    tenant_id=tenant_id,
                    attempt_id=new_id("aca"),
                    job_id=job_id,
                    source_id=source_id,
                    outcome="started",
                    valid_new_count=0,
                    diagnostic=None,
                    attempted_at=self.now(),
                )
            )
            return seconds

    async def record_source_attempt(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        source_id: str,
        outcome: str,
        valid_new_count: int,
        *,
        diagnostic: SourceDiagnostic | None = None,
    ) -> None:
        """保存实际来源结果，selected不代表可用；不记录凭证或原始异常。"""
        self._check(tenant_id)
        if (
            get_discovery_source(source_id) is None
            or (
                outcome not in ("complete", "unavailable", "failed", "quota_exhausted")
                and not is_discovery_failure(outcome)
            )
            or valid_new_count < 0
        ):
            raise ValueError("来源尝试记录无效")
        safe = None
        if diagnostic is not None:
            if not isinstance(diagnostic, SourceDiagnostic):
                raise ValueError("来源诊断类型无效")
            checked = SourceDiagnostic.model_validate(diagnostic.model_dump())
            if (outcome == "complete") != (checked.save is not None):
                raise ValueError("来源诊断阶段不一致")
            if (
                checked.save is not None
                and checked.save.inserted_fresh != valid_new_count
            ):
                raise ValueError("来源新增计数不一致")
            safe = checked.model_dump(mode="json")
        now = self.now()
        async with self.factory() as session, session.begin():
            job = await self._leased(session, job_id, lease_token, now)
            if job.source_plan:
                attempt = (
                    await session.scalars(
                        self.scoped_query(AcquisitionAttemptRow).where(
                            AcquisitionAttemptRow.job_id == job_id,
                            AcquisitionAttemptRow.source_id == source_id,
                        )
                    )
                ).one_or_none()
                if attempt is None or attempt.outcome != "started":
                    raise ValueError("来源未预留或已结束")
                attempt.outcome = outcome
                attempt.valid_new_count = valid_new_count
                attempt.diagnostic = safe
                return
            session.add(
                AcquisitionAttemptRow(
                    tenant_id=tenant_id,
                    attempt_id=new_id("aca"),
                    job_id=job_id,
                    source_id=source_id,
                    outcome=outcome,
                    valid_new_count=valid_new_count,
                    diagnostic=safe,
                    attempted_at=now,
                )
            )

    async def fit_pool(
        self, tenant_id: TenantId, job_id: AcquisitionJobId, lease_token: str
    ) -> tuple[PublicCandidate, ...]:
        """复用公共缓存，但只评估本企业尚未取得的候选，不把旧客户计入交付缺口。"""
        self._check(tenant_id)
        job = await self.get_job(tenant_id, job_id)
        if job is None or not job.query.requires_private_fit:
            raise ValueError("企业适配任务无效")
        fresh = await self.catalog.fresh(job.query.public_projection(), self.now())
        async with self.factory() as session, session.begin():
            await self._leased(session, job_id, lease_token, self.now())
            domains = set(
                (
                    await session.scalars(
                        self.scoped_query(CandidateAllocationRow).with_only_columns(
                            CandidateAllocationRow.canonical_domain
                        )
                    )
                ).all()
            )
            emails = set(
                (
                    await session.scalars(
                        self.scoped_query(AllocationEmailRow).with_only_columns(
                            AllocationEmailRow.email
                        )
                    )
                ).all()
            )
            return tuple(
                c
                for c in fresh
                if c.canonical_domain not in domains
                and not emails.intersection(c.public_emails)
            )

    async def reserve_fit_batch(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        candidates: tuple[PublicCandidate, ...],
    ) -> InventoryFitBatch:
        """模型派发前持久化批次；未知结果保留started，稳定序号不得重派。"""
        self._check(tenant_id)
        if (
            not 1 <= len(candidates) <= MAX_FIT_CANDIDATES
            or len(
                json.dumps(fit_evidence_inputs(candidates), ensure_ascii=False).encode()
            )
            > MAX_FIT_EVIDENCE_BYTES
        ):
            raise ValueError("适配批次大小无效")
        async with self.factory() as session, session.begin():
            row = await self._leased(session, job_id, lease_token, self.now())
            if (
                not InventoryQuery.model_validate(row.query).requires_private_fit
                or len(row.fit_batches) >= MAX_FIT_BATCHES
            ):
                raise ValueError("适配预算已用完")
            versions = tuple(
                FitCandidateVersion(
                    candidate_id=str(c.id),
                    evidence_version=candidate_evidence_version(c),
                )
                for c in candidates
            )
            seen = {
                (c["candidate_id"], c["evidence_version"])
                for b in row.fit_batches
                for c in b["candidates"]
            }
            if len({v.candidate_id for v in versions}) != len(versions) or any(
                (v.candidate_id, v.evidence_version) in seen for v in versions
            ):
                raise ValueError("适配批次不得重派")
            batch = InventoryFitBatch(
                sequence=len(row.fit_batches),
                state="started",
                candidates=versions,
                query_fingerprint=fit_query_fingerprint(
                    InventoryQuery.model_validate(row.query)
                ),
            )
            row.fit_batches = [*row.fit_batches, batch.model_dump(mode="json")]
            return batch

    async def complete_fit_batch(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        sequence: int,
        assessments: tuple[InventoryFitAssessment, ...],
    ) -> None:
        """只提交本任务当前租约完整批次；模型结果不能选择别的任务/证据版本。"""
        self._check(tenant_id)
        async with self.factory() as session, session.begin():
            row = await self._leased(session, job_id, lease_token, self.now())
            batches = [InventoryFitBatch.model_validate(b) for b in row.fit_batches]
            if not 0 <= sequence < len(batches) or batches[sequence].state != "started":
                raise ValueError("适配批次状态无效")
            batch = batches[sequence]
            if batch.query_fingerprint != fit_query_fingerprint(
                InventoryQuery.model_validate(row.query)
            ):
                raise ValueError("适配目标已变化")
            expected = {(c.candidate_id, c.evidence_version) for c in batch.candidates}
            if (
                len(assessments) != len(expected)
                or {(a.candidate_id, a.evidence_version) for a in assessments}
                != expected
            ):
                raise ValueError("适配结果证据绑定无效")
            public = {
                (str(c.id), candidate_evidence_version(c)): c
                for c in await self.catalog.fresh(
                    InventoryQuery.model_validate(row.query).public_projection(),
                    self.now(),
                )
            }
            if any(
                (a.candidate_id, a.evidence_version) not in public
                or a.evidence_quote
                not in public[(a.candidate_id, a.evidence_version)].evidence_text[:4000]
                for a in assessments
            ):
                raise ValueError("适配结果公开证据无效")
            batches[sequence] = batch.model_copy(
                update={"state": "complete", "assessments": assessments}
            )
            row.fit_batches = [b.model_dump(mode="json") for b in batches]

    async def deliver_job(
        self, tenant_id: TenantId, job_id: AcquisitionJobId, lease_token: str
    ) -> InventoryResult:
        """从已保存公共结果交付公司待分配池，遵守交付当天额度。"""
        self._check(tenant_id)
        job = await self.get_job(tenant_id, job_id)
        if job is None:
            raise ValueError("任务不存在")
        now = self.now()
        fresh = await self.catalog.fresh(job.query.public_projection(), now)
        async with self.factory() as session, session.begin():
            quota = await self._quota(session, now)
            row = await self._leased(session, job_id, lease_token, now)
            if InventoryQuery.model_validate(row.query) != job.query:
                raise ValueError("采集目标已变化")
            if job.query.requires_private_fit:
                fresh = selected_fit_candidates(
                    fresh,
                    tuple(InventoryFitBatch.model_validate(b) for b in row.fit_batches),
                    job.query,
                )
            count = await self._allocate(
                session,
                quota,
                EmployeeId(row.employee_id),
                row.key_hash,
                fresh,
                row.remaining_count,
                now,
            )
            exhausted = False
            if row.source_plan:
                attempts = (
                    await session.scalars(
                        self.scoped_query(AcquisitionAttemptRow).where(
                            AcquisitionAttemptRow.job_id == job_id,
                        )
                    )
                ).all()
                exhausted = set(row.source_plan) <= {a.source_id for a in attempts}
            row.updated_at = now
            row.remaining_count -= count
            row.status = (
                "complete"
                if row.remaining_count == 0
                else "failed"
                if job.query.requires_private_fit or exhausted
                else "queued"
            )
            if row.status == "failed":
                row.failure_reason = "budget_exhausted"
            row.lease_token = row.lease_until = None
            return await self._result(session, row.key_hash, row)

    async def list_jobs(
        self,
        tenant_id: TenantId,
        allowed_owner_ids: tuple[EmployeeId, ...] | None,
        limit: int,
    ) -> tuple[AcquisitionJob, ...]:
        """查询当前企业授权归属的未完成任务，空范围不返回。"""
        self._check(tenant_id)
        if not 1 <= limit <= 1000:
            raise ValueError("分页数量必须在1至1000")
        query = self.scoped_query(AcquisitionJobRow).where(
            AcquisitionJobRow.status != "complete"
        )
        if allowed_owner_ids is not None:
            query = query.where(AcquisitionJobRow.employee_id.in_(allowed_owner_ids))
        async with self.factory() as session:
            rows = (
                await session.scalars(
                    query.order_by(
                        AcquisitionJobRow.created_at.desc(), AcquisitionJobRow.job_id
                    ).limit(limit)
                )
            ).all()
            return tuple(self._job(row) for row in rows)

    async def fail_job(
        self,
        tenant_id: TenantId,
        job_id: AcquisitionJobId,
        lease_token: str,
        reason: str,
    ) -> None:
        """记录可展示的枚举原因，拒绝任意异常文本。"""
        self._check(tenant_id)
        if reason not in (
            "sources_unavailable",
            "budget_exhausted",
            "collector_failed",
        ):
            raise ValueError("失败原因无效")
        now = self.now()
        async with self.factory() as session, session.begin():
            row = await self._leased(session, job_id, lease_token, now)
            row.status, row.failure_reason, row.updated_at = "failed", reason, now
            row.lease_token = row.lease_until = None

    async def source_history(self, tenant_id: TenantId) -> tuple[SourceHistory, ...]:
        """聚合公司实际来源尝试；运行可用性仍由外部就绪检查给出。"""
        self._check(tenant_id)
        async with self.factory() as session:
            rows = (
                await session.execute(
                    select(
                        AcquisitionAttemptRow.source_id,
                        func.count(AcquisitionAttemptRow.attempt_id),
                        func.sum(AcquisitionAttemptRow.valid_new_count),
                        func.max(AcquisitionAttemptRow.attempted_at),
                    )
                    .where(AcquisitionAttemptRow.tenant_id == self._tenant_id)
                    .group_by(AcquisitionAttemptRow.source_id)
                    .order_by(AcquisitionAttemptRow.source_id)
                )
            ).all()
            return tuple(
                SourceHistory(
                    source_id=r[0],
                    attempts=r[1],
                    valid_new_count=r[2],
                    last_used_at=r[3],
                )
                for r in rows
            )
