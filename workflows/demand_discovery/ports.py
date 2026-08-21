"""需求探索的确认提案、Web 工具与账户发现窄端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_runtime.base import AgentTask, ChangeSet
from connectors.web_search.client import PageSnapshot
from shared.schemas.identifiers import (
    NeedHypothesisId,
    RunId,
    TenantId,
    UserId,
)
from tool_gateway.handlers.web_slots import SearchResultBatch


@dataclass(frozen=True)
class DiscoverySearchQuery:
    query: str
    country: str
    category: str
    limit: int


@dataclass(frozen=True)
class DemandDiscoveryPlan:
    """老板确认后的不可变探索事实；没有默认预算或默认市场。"""

    objective: str
    queries: tuple[DiscoverySearchQuery, ...]
    target_countries: tuple[str, ...]
    target_categories: tuple[str, ...]
    excluded_countries: tuple[str, ...]
    excluded_categories: tuple[str, ...]
    max_search_queries: int
    max_pages_read: int
    max_signals: int
    max_hypotheses: int
    minimum_confidence_tier: str
    strategy_group: str
    campaign_id: str
    role_hints: tuple[str, ...]
    assessment_ref: str


@runtime_checkable
class DemandDiscoveryTaskReader(Protocol):
    async def load_confirmed(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        acting_user: UserId,
    ) -> DemandDiscoveryPlan: ...


@runtime_checkable
class DemandIntelligenceCapability(Protocol):
    async def run(self, task: AgentTask, context: object) -> ChangeSet: ...


@runtime_checkable
class WebDiscoverySearcher(Protocol):
    async def search(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        query: str,
        country: str,
        category: str,
        limit: int,
    ) -> SearchResultBatch: ...

    def release(self, batch: SearchResultBatch) -> None: ...

    def discard_all(self) -> None: ...


@runtime_checkable
class WebDiscoveryPageReader(Protocol):
    async def read_page(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        batch: SearchResultBatch,
        result_index: int,
    ) -> PageSnapshot: ...


@runtime_checkable
class AccountDiscoveryQueue(Protocol):
    async def start(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        *,
        campaign_id: str,
        acting_user: UserId,
        role_hints: tuple[str, ...],
        assessment_ref: str,
    ) -> RunId: ...


__all__ = (
    "AccountDiscoveryQueue",
    "DemandDiscoveryPlan",
    "DemandDiscoveryTaskReader",
    "DemandIntelligenceCapability",
    "DiscoverySearchQuery",
    "WebDiscoveryPageReader",
    "WebDiscoverySearcher",
)
