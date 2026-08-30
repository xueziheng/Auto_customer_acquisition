"""Sourcing Case V2 的跨域只读窄端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_runtime.sourcing_agent import SourcingPageCandidateDraft
from connectors.web_search.client import PageSnapshot
from domains.sourcing.service import PublicSourcingPlan, SourcingNeedSnapshot
from shared.schemas.identifiers import (
    OpportunityId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
    ValidatedNeedId,
)
from tool_gateway.handlers.web_slots import SearchResultBatch


@runtime_checkable
class SourcingNeedReader(Protocol):
    """读取已验证需求的 tenant-bound、带 Provenance 冻结快照。"""

    async def read(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingNeedSnapshot:
        """不存在、跨租户或关键来源不可核实时必须失败关闭。"""
        ...


@runtime_checkable
class OpportunityLinkReader(Protocol):
    """按已验证需求读取真实、同租户的机会引用。"""

    async def find_for_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> OpportunityId | None:
        """没有真实机会时返回 ``None``，不得构造占位 ID。"""
        ...


class AuthorizedPublicSourcingPlanReader(Protocol):
    async def load_authorized(
        self,
        *,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        plan_id: SourcingPlanId,
        plan_hash: str,
    ) -> PublicSourcingPlan: ...


class PersistedSearchReceiptPort(Protocol):
    async def restore(
        self, *, tenant_id: TenantId, run_id: RunId, plan_hash: str, query_index: int
    ) -> SearchResultBatch | None: ...

    async def commit_locator_receipt(self, **values: object) -> None: ...

    async def record_uncertain(self, **values: object) -> None: ...


class PublicSourcingSearcher(Protocol):
    async def search(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        query: str,
        country: str,
        category: str,
        limit: int,
        *,
        quota_request_key: str,
    ) -> SearchResultBatch: ...

    def release(self, batch: SearchResultBatch) -> None: ...

    def discard_all(self) -> None: ...


class PublicPageReader(Protocol):
    async def read_page(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        batch: SearchResultBatch,
        result_index: int,
    ) -> PageSnapshot: ...


class PublicCandidateExtractor(Protocol):
    async def extract(
        self, need: SourcingNeedSnapshot, page: PageSnapshot
    ) -> SourcingPageCandidateDraft: ...


class PublicCandidateDraftWriter(Protocol):
    async def save(self, **values: object) -> str: ...


__all__ = (
    "AuthorizedPublicSourcingPlanReader",
    "OpportunityLinkReader",
    "PersistedSearchReceiptPort",
    "PublicCandidateDraftWriter",
    "PublicCandidateExtractor",
    "PublicPageReader",
    "PublicSourcingSearcher",
    "SourcingNeedReader",
)
