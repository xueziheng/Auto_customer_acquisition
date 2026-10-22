"""Codex只选择与提取；真实材料、来源绑定和最终投影由确定性代码持有。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit

from connectors.codex_agent.discovery import (
    DISCOVERY_SEARCH_LIMIT,
    DISCOVERY_TOOL_LIMIT,
    DiscoverySession,
    DiscoveryToolFailure,
)
from connectors.web_search.client import PageSnapshot
from domains.prospecting.collection_result import CollectionResult
from domains.prospecting.projection_diagnostic import (
    ProjectionDiagnosticError,
    ProjectionSummary,
    inspect_candidate_shape,
)
from domains.prospecting.public_evidence import ProjectionRejected, project_candidate
from domains.prospecting.service import AcquisitionJob, PublicCandidate
from shared.schemas.discovery_diagnostic import (
    DiscoveryDiagnostic,
    DiscoveryDiagnosticError,
)
from shared.schemas.identifiers import RunId
from shared.schemas.page_failure import PageReadFailure
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader

from .inventory_acquisition import InventoryPlanBudget
from .inventory_query import choose_inventory_country, compose_inventory_query
from .inventory_web import InventoryWebTools

ToolFactory = Callable[
    [AcquisitionJob, str, Callable[[], Awaitable[None]]], Awaitable[InventoryWebTools]
]


class InventoryModel(Protocol):
    async def __call__(
        self,
        job: AcquisitionJob,
        source_id: str,
        session: DiscoverySession,
        payload: dict[str, object],
    ) -> dict[str, object]: ...


class CodexInventoryCollector:
    def __init__(
        self,
        tools: ToolFactory,
        generate: InventoryModel,
        *,
        context_reader: ConfirmedContextReader | None = None,
        service_budget: InventoryPlanBudget | None = None,
    ) -> None:
        self.service_budget = service_budget
        self.context_reader = context_reader
        self.tools, self.generate = tools, generate
        self._failure_scope: tuple[str, str, str | None] | None = None
        self._failed_hosts: set[str] = set()

    async def collect(
        self,
        job: AcquisitionJob,
        source_id: str,
        guard: Callable[[], Awaitable[None]],
        *,
        target_count: int = 50,
    ) -> CollectionResult:
        upstream_guard = guard

        async def current_guard() -> None:
            await upstream_guard()
            if job.query.confirmed_refs:
                if self.context_reader is None:
                    from shared.errors import PermissionDenied

                    raise PermissionDenied("企业资料上下文不可用")
                await self.context_reader.read(
                    job.tenant_id, job.employee_id, job.query.confirmed_refs
                )

        await current_guard()
        scope = (str(job.tenant_id), str(job.id), job.lease_token)
        if self._failure_scope != scope:
            self._failure_scope, self._failed_hosts = scope, set()
        failed_hosts = self._failed_hosts
        tools = await self.tools(job, source_id, current_guard)
        pages: dict[str, tuple[SearchResultBatch, int]] = {}
        snapshots: dict[str, PageSnapshot] = {}
        run_id = RunId(str(job.id))
        searches = 0
        official_pages: set[str] = set()
        discovery_read = False
        strategies = {
            "company_websites": "official company website contact",
            "industry_directories": "industry business directory",
            "trade_associations": "trade association members",
            "chambers": "chamber of commerce member directory",
            "trade_fairs": "trade fair exhibitors",
            "dealer_networks": "authorized dealer network",
            "company_procurement": "company procurement supplier portal",
            "public_procurement_pages": "public procurement buyer notice",
            "company_news": "company business news",
            "linkedin_public": "site:linkedin.com/company",
            "trade_record_indexes": "public import export trade records company",
        }

        async def execute(name: str, arguments: dict) -> dict:
            nonlocal searches, discovery_read
            await current_guard()
            if name == "source_search":
                if set(arguments) != {"source_id", "query"} or arguments[
                    "source_id"
                ] not in {source_id, "company_websites"}:
                    raise DiscoveryDiagnosticError(
                        DiscoveryDiagnostic("source_scope", "invalid_request", 0)
                    )
                if searches >= DISCOVERY_SEARCH_LIMIT:
                    raise DiscoveryToolFailure("search_limit_reached")
                official = arguments["source_id"] == "company_websites"
                if source_id != "company_websites" and official and not discovery_read:
                    raise DiscoveryToolFailure("source_read_required")
                searches += 1
                search_country = job.query.country or "ZZ"
                search_query = job.query
                if (
                    job.query.regions
                    or job.query.countries
                    or job.query.region not in {None, "africa"}
                ):
                    search_country = choose_inventory_country(
                        job.query, arguments["query"], searches - 1
                    )
                    search_query = job.query.model_copy(
                        update={
                            "country": search_country,
                            "countries": (),
                            "regions": (),
                            "region": None,
                        }
                    )
                try:
                    query = compose_inventory_query(
                        search_query,
                        strategies[arguments["source_id"]],
                        arguments["query"],
                        maximum_chars=getattr(tools, "maximum_query_chars", 400),
                        public_domains=tuple(
                            sorted(
                                {
                                    urlsplit(batch.results[index].url).hostname or ""
                                    for batch, index in pages.values()
                                }
                            )
                        ),
                    )
                except ValueError:
                    raise DiscoveryDiagnosticError(
                        DiscoveryDiagnostic("source_scope", "invalid_request", 0)
                    ) from None
                batch = await tools.searcher.search(
                    job.tenant_id,
                    run_id,
                    query,
                    search_country,
                    job.query.product_tags[0],
                    20,
                    quota_request_key=sha256(
                        f"{job.id}:{source_id}:{searches}".encode()
                    ).hexdigest(),
                )
                result = []
                for index, entry in enumerate(batch.results):
                    page_id = f"page_{len(pages) + 1}"
                    pages[page_id] = (batch, index)
                    if official:
                        official_pages.add(page_id)
                    result.append(
                        {
                            "page_id": page_id,
                            "url": entry.url,
                            "title": entry.title,
                            "description": entry.description[:400],
                            "discovery_only": not official,
                        }
                    )
                return {"results": result, "content_is_untrusted": True}
            if name not in ("read_public_page", "browse_public_page") or set(
                arguments
            ) != {"page_id"}:
                raise DiscoveryDiagnosticError(
                    DiscoveryDiagnostic("page_scope", "invalid_request", 0)
                )
            if arguments["page_id"] not in pages:
                raise DiscoveryToolFailure("page_id_unknown")
            page_id = arguments["page_id"]
            batch, index = pages[page_id]
            host = (
                (urlsplit(batch.results[index].url).hostname or "")
                .encode("idna")
                .decode()
                .lower()
                .rstrip(".")
            )
            if not host:
                raise DiscoveryDiagnosticError(
                    DiscoveryDiagnostic("page_scope", "invalid_request", 0)
                )
            if host in failed_hosts:
                raise DiscoveryToolFailure("page_unavailable")
            reader = (
                tools.browser_reader
                if name == "browse_public_page"
                else tools.page_reader
            )
            if self.service_budget is not None:
                from domains.prospecting.service import evidence_source_domain
                await self.service_budget.reserve_execution(job, kind="pages",
                    key=f"{source_id}:{name}:{batch.results[index].url}", amount=1,
                    host=evidence_source_domain(batch.results[index].url))
            try:
                snapshot = await reader.read_page(job.tenant_id, run_id, batch, index)
            except PageReadFailure as error:
                failed_hosts.add(host)
                await current_guard()
                if error.recoverable:
                    raise DiscoveryToolFailure("page_unavailable") from None
                raise DiscoveryDiagnosticError(error.diagnostic) from None
            except ToolGatewayError as error:
                failed_hosts.add(host)
                # 仅已知页面拒绝可继续；权限、租约和不确定外部请求仍关闭会话。
                if error.category not in {
                    ToolErrorCategory.PAGE_ACCESS_FORBIDDEN,
                    ToolErrorCategory.LOGIN_OR_CAPTCHA,
                    ToolErrorCategory.UNSAFE_REDIRECT,
                    ToolErrorCategory.PROVIDER_PERMANENT,
                }:
                    raise
                await current_guard()
                raise DiscoveryToolFailure("page_unavailable") from None
            except BaseException:
                failed_hosts.add(host)
                raise
            await current_guard()
            snapshots[page_id] = snapshot
            if page_id not in official_pages:
                discovery_read = True
            return {
                "page_id": page_id,
                "url": snapshot.url,
                "text": snapshot.text[:20000],
                "content_is_untrusted": True,
                "discovery_only": page_id not in official_pages,
                "next_step": "For directory results, search source_id=company_websites using an identified company name, then read its official site.",
            }

        async with DiscoverySession(
            # 三来源各7次 + 冷启动1次 = 22，预留探索的真实Gateway预算。
            execute,
            max_requests=DISCOVERY_TOOL_LIMIT,
            remaining_searches=lambda: max(0, DISCOVERY_SEARCH_LIMIT - searches),
            timeout_seconds=140,
            max_result_bytes=100000,
        ) as session:
            await current_guard()
            try:
                result = await self.generate(
                    job,
                    source_id,
                    session,
                    {
                        "query": job.query.model_dump(mode="json"),
                        "allowed_source": source_id,
                        "tool_budget": {
                            "search_limit": DISCOVERY_SEARCH_LIMIT,
                            "tool_limit": DISCOVERY_TOOL_LIMIT,
                        },
                        "remaining_inventory_target": target_count,
                        "explore_even_if_target_met": True,
                        "allowed_sources": [source_id, "company_websites"],
                        "output_schema": {
                            "candidates": [
                                {
                                    "page_id": "from tools",
                                    "name": "exact company name",
                                    "country": "ISO country backed by address",
                                    "public_emails": ["exact public email"],
                                }
                            ]
                        },
                        "requirements": "Read official company pages. Directory pages are only leads to websites. Never invent evidence or promote product fit to validated need.",
                    },
                )
            except ProjectionDiagnosticError as error:
                raise ProjectionDiagnosticError(
                    ProjectionSummary.model_validate(
                        {
                            **error.summary.model_dump(),
                            "tool_usage": session.usage_snapshot(),
                            "search_result_count": len(pages),
                            "read_success_count": len(snapshots),
                            "official_read_count": len(
                                official_pages.intersection(snapshots)
                            ),
                        }
                    )
                ) from None
            await current_guard()
        if searches == 0:
            raise ValueError("来源未实际搜索，不能记录完成")
        candidates: list[PublicCandidate] = []
        shape = inspect_candidate_shape(result)
        counts: dict[str, int] = {}

        def summary(
            *, complete: bool = False, internal: bool = False
        ) -> ProjectionSummary:
            return ProjectionSummary.model_validate(
                {
                    **shape.model_dump(),
                    "projection_complete": complete,
                    "internal_failure": internal,
                    "tool_usage": session.usage_snapshot(),
                    "projected_count": len(candidates),
                    "rejected": counts,
                    "search_result_count": len(pages),
                    "read_success_count": len(snapshots),
                    "official_read_count": len(official_pages.intersection(snapshots)),
                }
            )

        if shape.input_shape != "valid":
            raise ProjectionDiagnosticError(summary())
        raw_candidates = result.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ProjectionDiagnosticError(summary())
        for value in raw_candidates:
            reason = None
            if not isinstance(value, dict):
                reason = "item_not_object"
            elif not isinstance(value.get("page_id"), str):
                reason = "item_fields_invalid"
            elif value["page_id"] not in pages:
                reason = "page_unknown"
            elif value["page_id"] not in snapshots:
                reason = "page_not_read"
            elif value["page_id"] not in official_pages:
                reason = "discovery_only"
            elif not isinstance(value.get("name"), str) or not isinstance(
                value.get("country"), str
            ):
                reason = "item_fields_invalid"
            else:
                try:
                    emails = tuple(value.get("public_emails", []))
                    if any(not isinstance(email, str) for email in emails):
                        reason = "item_fields_invalid"
                except TypeError:
                    reason = "item_fields_invalid"
            if reason is not None:
                counts[reason] = counts.get(reason, 0) + 1
                continue
            snapshot = snapshots[value["page_id"]]
            try:
                candidate = project_candidate(
                    source_id="company_websites",
                    source_url=snapshot.url,
                    text=snapshot.text,
                    content_hash=snapshot.content_hash,
                    artifact_id=snapshot.snapshot_artifact_ref,
                    observed_at=snapshot.observed_at,
                    name=value["name"],
                    country=value["country"],
                    product_tags=job.query.product_tags,
                    public_emails=emails,
                )
            except ProjectionRejected as error:
                counts[error.reason] = counts.get(error.reason, 0) + 1
                continue
            except Exception:  # noqa: BLE001 -- 未预期投影异常关闭来源，不回显输入。
                raise ProjectionDiagnosticError(summary(internal=True)) from None
            candidates.append(candidate)
        return CollectionResult(tuple(candidates), summary(complete=True))
