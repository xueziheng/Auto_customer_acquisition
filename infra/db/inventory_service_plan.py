"""库存请求与来源共享服务计划预留；预算恢复不依赖进程内计数。"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.service import (
    AcquisitionJob,
    PublicCandidate,
    ServicePlanReservation,
    ServiceTier,
    ServiceTierPolicy,
    tier_policy,
)
from infra.db.prospect_inventory_tables import AcquisitionJobRow, InventoryRequestRow
from infra.db.prospecting_service_plans import (
    PostgresProspectingServicePlanStore,
    _stored_time,
)
from infra.db.prospecting_service_tables import ProspectingServiceReservationRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId


class InventoryServicePlanBudget:
    """运营额度与供应商实际账本各有量纲；前者只收紧后者。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId,
                 *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.factory, self.tenant_id, self.now = factory, tenant_id, now
        self.store = PostgresProspectingServicePlanStore(factory)

    async def reserve_request(self, key_hash: str, count: int) -> None:
        state = await self.store.get(self.tenant_id)
        if state is None:
            return
        policy = tier_policy(state.tier)
        key = "inventory:" + key_hash
        receipt = await self.store.reservation(self.tenant_id, key=key, now=self.now())
        # 重放原预留数量保持请求身份；降档只收紧后续执行，不改历史已记用量。
        if receipt is None and count > policy.per_run_signal_limit:
            raise PermissionDenied("本档单次候选处理上限不足，请减少请求数量")
        await self.store.reserve(self.tenant_id, key=key, candidates=count,
            searches=receipt.searches if receipt else policy.per_run_search_limit,
            contacts=0, now=self.now())

    async def current(self, job: AcquisitionJob) -> tuple[ServicePlanReservation, ServiceTierPolicy] | None:
        if job.tenant_id != self.tenant_id:
            raise PermissionDenied("服务计划租户不匹配")
        state = await self.store.get(self.tenant_id)
        if state is None:
            return None
        async with self.factory() as session:
            row = await session.scalar(select(AcquisitionJobRow).where(
                AcquisitionJobRow.tenant_id == self.tenant_id, AcquisitionJobRow.job_id == job.id))
            if row is None:
                raise PermissionDenied("服务计划任务不存在")
            key = "inventory:" + row.key_hash
        receipt = await self.store.reservation(self.tenant_id, key=key, now=self.now())
        if receipt is None:
            # 服务计划在旧任务启动后才赋档时，不能把旧任务当作无限预算。
            raise PermissionDenied("服务计划任务缺少当前服务期预留")
        return receipt, tier_policy(state.tier)

    async def limits(self, job: AcquisitionJob) -> dict[str, int] | None:
        current = await self.current(job)
        if current is None:
            return None
        receipt, current_policy = current
        original = tier_policy(receipt.tier)
        return {"candidates": min(receipt.candidates, current_policy.per_run_signal_limit),
            "searches": min(receipt.searches, current_policy.per_run_search_limit),
            "pages": min(original.per_run_page_limit, current_policy.per_run_page_limit),
            "pages_per_company": min(original.max_pages_per_company, current_policy.max_pages_per_company)}

    async def reserve_execution(self, job: AcquisitionJob, *, kind: str, key: str,
                                amount: int, host: str | None = None) -> None:
        """独立提交未知开销；同动作不得重派，来源间与重启共享同一上限。"""
        limits = await self.limits(job)
        if limits is None:
            return
        current = await self.current(job)
        assert current is not None
        receipt, _ = current
        if kind not in {"pages", "candidates"} or amount < 1:
            raise ValueError("执行预算类型无效")
        async with self.store._transaction() as session:
            state = await self.store._plan(session, self.tenant_id)
            if state is None or not cast(datetime, _stored_time(state.period_started_at)) <= self.now() < cast(datetime, _stored_time(state.period_ends_at)):
                raise PermissionDenied("服务期已失效")
            row = await session.scalar(select(ProspectingServiceReservationRow).where(
                ProspectingServiceReservationRow.tenant_id == self.tenant_id,
                ProspectingServiceReservationRow.reservation_key == receipt.key).with_for_update())
            if row is None or row.period_started_at != state.period_started_at:
                raise PermissionDenied("服务期预留已失效")
            usage = dict(row.execution_usage or {})
            digest = sha256(f"{kind}:{key}".encode()).hexdigest()
            if digest in usage:
                raise PermissionDenied("未知执行消耗不得重复派发")
            spent = sum(v['amount'] for v in usage.values() if v['kind'] == kind)
            maximum = min(limits[kind], getattr(tier_policy(cast(ServiceTier, state.tier)),
                'per_run_page_limit' if kind == 'pages' else 'per_run_signal_limit'))
            if spent + amount > maximum:
                raise PermissionDenied("本轮累计执行额度不足")
            if host and sum(v['amount'] for v in usage.values() if v.get('host') == host) + amount > limits['pages_per_company']:
                raise PermissionDenied("本企业页面上限已用尽")
            usage[digest] = {"kind": kind, "amount": amount, "host": host}
            row.execution_usage = usage

    async def request_reservation(self, request_key: str) -> ServicePlanReservation | None:
        """仅返回当前有效服务期内独立提交的原请求预留。"""
        digest = sha256(request_key.encode()).hexdigest()
        return await self.store.reservation(self.tenant_id, key="inventory:" + digest, now=self.now())

    async def find_request(self, request_key: str) -> str | None:
        """即时命中无 job 仍有持久请求；恢复不得重新提交。"""
        digest = sha256(request_key.encode()).hexdigest()
        async with self.factory() as session:
            return await session.scalar(select(InventoryRequestRow.key_hash).where(
                InventoryRequestRow.tenant_id == self.tenant_id, InventoryRequestRow.key_hash == digest))

    async def remaining_candidates(self, job: AcquisitionJob) -> int | None:
        current = await self.current(job)
        if current is None:
            return None
        receipt, policy = current
        async with self.factory() as session:
            row = await session.get(ProspectingServiceReservationRow, (str(self.tenant_id), receipt.key))
            if row is None:
                raise PermissionDenied("服务期预留缺失")
            used = sum(v['amount'] for v in (row.execution_usage or {}).values() if v['kind'] == 'candidates')
        return max(0, min(receipt.candidates, policy.per_run_signal_limit) - used)

    async def admit_source_candidates(self, job: AcquisitionJob, source: str, candidates: tuple[PublicCandidate, ...]) -> None:
        current = await self.current(job)
        if current is None:
            return
        receipt, _ = current
        digest = sha256(f'candidates:{source}'.encode()).hexdigest()
        async with self.store._transaction() as session:
            await self.store._plan(session, self.tenant_id)
            row = await session.get(ProspectingServiceReservationRow, (str(self.tenant_id), receipt.key))
            if row is None:
                raise PermissionDenied("服务期预留缺失")
            usage = dict(row.execution_usage or {})
            entry = dict(usage.get(digest) or {})
            domains = [c.canonical_domain for c in candidates]
            if not entry or len(domains) > entry['amount'] or ('domains' in entry and entry['domains'] != domains):
                raise PermissionDenied('来源候选与预留不一致')
            entry['domains'] = domains
            usage[digest] = entry
            row.execution_usage = usage

    async def fit_candidates(self, job: AcquisitionJob, candidates: tuple[PublicCandidate, ...]) -> tuple[PublicCandidate, ...]:
        """已有库存和新来源共用处理名额；并发新增公共候选不扩张本任务。"""
        current = await self.current(job)
        if current is None:
            return candidates
        receipt, policy = current
        async with self.store._transaction() as session:
            state = await self.store._plan(session, self.tenant_id)
            row = await session.get(ProspectingServiceReservationRow, (str(self.tenant_id), receipt.key))
            if row is None:
                raise PermissionDenied("服务期预留缺失")
            if (state is None or row.period_started_at != state.period_started_at
                or not cast(datetime, _stored_time(state.period_started_at)) <= self.now() < cast(datetime, _stored_time(state.period_ends_at))):
                raise PermissionDenied('候选处理服务期已失效')
            usage = dict(row.execution_usage or {})
            allowed = {d for v in usage.values() for d in v.get('domains', [])}
            used = sum(v['amount'] for v in usage.values() if v['kind'] == 'candidates')
            cap = min(receipt.candidates, policy.per_run_signal_limit, tier_policy(cast(ServiceTier,state.tier)).per_run_signal_limit)
            for candidate in candidates:
                if candidate.canonical_domain in allowed or used >= cap:
                    continue
                digest = sha256(f'fit:{candidate.canonical_domain}'.encode()).hexdigest()
                usage[digest] = {'kind':'candidates','amount':1,'domains':[candidate.canonical_domain]}
                allowed.add(candidate.canonical_domain)
                used += 1
            row.execution_usage = usage
            return tuple(c for c in candidates if c.canonical_domain in allowed)[:cap]
