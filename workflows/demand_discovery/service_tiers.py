"""把服务额度作为既有研究流程的附加边界，不扩张确认范围或供应商权限。"""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from connectors.web_search.client import PageSnapshot
from domains.prospecting.service import (
    ProspectingServicePlanStore,
    ServicePlanReservation,
    ServicePlanState,
    ServiceTierPolicy,
    evidence_source_domain,
    tier_policy,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.demand_discovery.ports import (
    DemandDiscoveryPlan,
    DemandDiscoveryTaskReader,
    WebDiscoveryPageReader,
    WebDiscoverySearcher,
)
from workflows.demand_discovery.steps import _base_context
from workflows.engine.runner import StepHandler, WorkflowRun


def bounded_plan(plan: DemandDiscoveryPlan, policy: ServiceTierPolicy,
                 *, offset: int = 0) -> DemandDiscoveryPlan:
    """只裁剪已确认查询；小预算仍保留全部研究线路，绝不自行扩大市场。"""
    budget = min(plan.max_search_queries, policy.per_run_search_limit)
    queries = plan.queries
    if queries:
        offset %= len(queries)
        rotated = queries[offset:] + queries[:offset]
    else:
        rotated = queries
    if len(rotated) > budget:
        selected = []
        lanes = set()
        for query in rotated:
            if query.discovery_lane not in lanes:
                selected.append(query)
                lanes.add(query.discovery_lane)
        if len(selected) > budget:
            raise ValidationError("当前档位不足以覆盖已确认研究线路")
        selected += [q for q in rotated if q not in selected][:budget - len(selected)]
        queries = tuple(selected)
    elif offset:
        queries = rotated
    return replace(plan, queries=queries, max_search_queries=budget,
        max_pages_read=min(plan.max_pages_read, policy.per_run_page_limit),
        max_signals=min(plan.max_signals, policy.per_run_signal_limit),
        max_hypotheses=min(plan.max_hypotheses, policy.per_run_hypothesis_limit))


async def current_plan(store: ProspectingServicePlanStore, tenant_id: TenantId,
                       now: datetime) -> ServicePlanState | None:
    """无配置保留历史流程；已配置但到期/损坏则不得降级成无限制历史行为。"""
    state = await store.get(tenant_id)
    if state is not None:
        if state.tenant_id != tenant_id:
            raise TenantIsolationViolation("服务计划租户不匹配")
        if not state.period_started_at <= now < state.period_ends_at:
            raise ValidationError("获客服务期已结束或尚未开始")
    return state


class TieredDiscoveryTaskReader:
    def __init__(self, reader: DemandDiscoveryTaskReader, store: ProspectingServicePlanStore,
                 *, now: Callable[[], datetime]) -> None:
        self._reader, self._store, self._now = reader, store, now

    async def load_confirmed(self, tenant_id: TenantId, proposal_id: str,
                             acting_user: UserId) -> DemandDiscoveryPlan:
        plan = await self._reader.load_confirmed(tenant_id, proposal_id, acting_user)
        state = await current_plan(self._store, tenant_id, self._now())
        if state is None:
            return plan
        policy = tier_policy(state.tier)
        usage = _usage.get()
        if usage is not None:
            if usage.tenant_id != tenant_id:
                raise TenantIsolationViolation("研究批次政策租户不匹配")
            original = bounded_plan(plan, usage.original_policy, offset=usage.query_offset)
            # 升档不扩大旧批次；降档只能从原查询集合裁剪。
            bounded = bounded_plan(original, policy)
            if usage.receipt is not None:
                bounded = replace(bounded,
                    queries=bounded.queries[:usage.receipt.searches],
                    max_search_queries=min(bounded.max_search_queries, usage.receipt.searches),
                    max_signals=min(bounded.max_signals, usage.receipt.candidates))
            return bounded
        # 有界批次按服务期与频率轮转；重启无需保存内存游标。
        elapsed = (self._now() - state.period_started_at).total_seconds()
        offset = int(elapsed // (policy.cadence_hours * 3600)) * policy.per_run_search_limit
        return bounded_plan(plan, policy, offset=offset)


@dataclass
class _ResearchUsage:
    tenant_id: TenantId
    run_id: RunId
    original_policy: ServiceTierPolicy
    query_offset: int
    receipt: ServicePlanReservation | None = None
    searches: int = 0
    pages: int = 0
    reserved_searches: int = 0
    sites: dict[str, int] = field(default_factory=dict)


_usage: ContextVar[_ResearchUsage | None] = ContextVar("prospecting_research_usage", default=None)


class TieredResearchStep:
    """单轮预算先独立持久预留，未知结果保留预留，不靠重试隐式退款。"""

    def __init__(self, delegate: StepHandler, reader: DemandDiscoveryTaskReader,
                 store: ProspectingServicePlanStore, *, now: Callable[[], datetime]) -> None:
        self._delegate, self._reader, self._store, self._now = delegate, reader, store, now

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        state = await current_plan(self._store, run.tenant_id, self._now())
        if state is None:
            return await self._delegate.execute(run)
        proposal_id, actor = _base_context(run)
        key = f"{run.run_id}:research"
        receipt = await self._store.reservation(run.tenant_id, key=key, now=self._now())
        policy = tier_policy(receipt.tier if receipt is not None else state.tier)
        created_at = receipt.created_at if receipt is not None else self._now()
        offset = int((created_at - state.period_started_at).total_seconds()
                     // (policy.cadence_hours * 3600)) * policy.per_run_search_limit
        usage = _ResearchUsage(run.tenant_id, run.run_id, policy, offset, receipt)
        token = _usage.set(usage)
        try:
            if receipt is None:
                plan = await self._reader.load_confirmed(run.tenant_id, proposal_id, actor)
                await self._store.reserve(run.tenant_id, key=key,
                    candidates=plan.max_signals, searches=min(len(plan.queries), plan.max_search_queries),
                    contacts=0, now=self._now())
                receipt = await self._store.reservation(run.tenant_id, key=key, now=self._now())
            if (receipt is None or receipt.period_started_at != state.period_started_at
                    or receipt.key != key or receipt.searches < 1 or receipt.candidates < 1
                    or receipt.contacts != 0):
                raise ValidationError("研究任务缺少有效的原批次预留")
            usage.receipt = receipt
            usage.original_policy = tier_policy(receipt.tier)
            usage.query_offset = int((receipt.created_at - receipt.period_started_at).total_seconds()
                // (usage.original_policy.cadence_hours * 3600)) * usage.original_policy.per_run_search_limit
            status, next_step, output = await self._delegate.execute(run)
            return status, next_step, {**output, "service_tier": state.tier,
                "service_policy_version": tier_policy(state.tier).policy_version}
        finally:
            _usage.reset(token)


class _TieredIO:
    def __init__(self, store: ProspectingServicePlanStore,
                 now: Callable[[], datetime]) -> None:
        self._store, self._now = store, now

    async def scope(self, tenant_id: TenantId, run_id: RunId) -> tuple[ServiceTierPolicy, _ResearchUsage] | None:
        state = await current_plan(self._store, tenant_id, self._now())
        if state is None:
            return None
        usage = _usage.get()
        if usage is None or (usage.tenant_id, usage.run_id) != (tenant_id, run_id):
            raise ValidationError("研究任务缺少受信服务额度上下文")
        receipt = await self._store.reservation(tenant_id, key=f"{run_id}:research", now=self._now())
        if receipt is None or receipt.period_started_at != state.period_started_at:
            raise ValidationError("研究任务缺少当前服务期的持久预留")
        usage.reserved_searches = receipt.searches
        policy = tier_policy(state.tier)
        return policy.model_copy(update={
            name: min(getattr(policy, name), getattr(usage.original_policy, name))
            for name in ("per_run_search_limit", "per_run_page_limit", "max_pages_per_company")
        }), usage


class TieredResearchSearcher(_TieredIO):
    def __init__(self, delegate: WebDiscoverySearcher, store: ProspectingServicePlanStore,
                 *, now: Callable[[], datetime]) -> None:
        super().__init__(store, now)
        self._delegate = delegate

    async def search(self, tenant_id: TenantId, run_id: RunId, query: str,
                     country: str, category: str, limit: int) -> SearchResultBatch:
        scope = await self.scope(tenant_id, run_id)
        if scope:
            policy, usage = scope
            if usage.searches >= min(policy.per_run_search_limit, usage.reserved_searches):
                raise ValidationError("研究搜索达到当前档位单轮上限")
            usage.searches += 1
        return await self._delegate.search(tenant_id, run_id, query, country, category, limit)

    def release(self, batch: SearchResultBatch) -> None:
        self._delegate.release(batch)

    def discard_all(self) -> None:
        self._delegate.discard_all()


class TieredResearchPageReader(_TieredIO):
    def __init__(self, delegate: WebDiscoveryPageReader, store: ProspectingServicePlanStore,
                 *, now: Callable[[], datetime]) -> None:
        super().__init__(store, now)
        self._delegate = delegate

    async def read_page(self, tenant_id: TenantId, run_id: RunId,
                        batch: SearchResultBatch, result_index: int) -> PageSnapshot:
        scope = await self.scope(tenant_id, run_id)
        if scope:
            policy, usage = scope
            url = batch.results[result_index].url
            host = evidence_source_domain(url) or (urlsplit(url).hostname or "")
            if usage.pages >= policy.per_run_page_limit:
                raise ValidationError("研究页面达到当前档位单轮上限")
            if usage.sites.get(host, 0) >= policy.max_pages_per_company:
                # 仅跳过已用完本轮页面份额的网站；原Gateway检查仍由被调用者执行。
                raise ToolGatewayError(ToolErrorCategory.VALIDATION)
            usage.pages += 1
            usage.sites[host] = usage.sites.get(host, 0) + 1
        return await self._delegate.read_page(tenant_id, run_id, batch, result_index)
