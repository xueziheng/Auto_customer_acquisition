"""多来源先覆盖后加深；每次页面交付都重新经过 Gateway 检查。"""

from dataclasses import replace

import pytest

from agent_runtime.base import ChangeSet
from connectors.web_search.client import PageSnapshot, WebSearchResult
from domains.demand.schemas import ResearchEvidence
from shared.schemas.identifiers import ChangeSetId, new_id
from tests.unit.workflows.test_demand_discovery import (
    ARTIFACT,
    HASH,
    NOW,
    _Demand,
    _Prospecting,
    _Reader,
    _run,
    _Searcher,
)
from tests.unit.workflows.test_research_discovery import research_plan
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import FreeSearchError, FreeSearchStopReason
from workflows.demand_discovery.research import ResearchExecuteSearchStep


class RecordingSearch(_Searcher):
    def __init__(self, run, *, duplicate_urls=False, empty_first=False, stop_at=None):
        super().__init__(run)
        self.duplicate_urls = duplicate_urls
        self.empty_first = empty_first
        self.stop_at = stop_at
        self.batch_numbers = {}

    async def search(self, *args):
        if self.stop_at == self.search_calls + 1:
            raise FreeSearchError(FreeSearchStopReason.QUOTA_EXHAUSTED)
        batch = await super().search(*args)
        self.batch_numbers[batch.handle] = self.search_calls
        prefix = "shared" if self.duplicate_urls else str(self.search_calls)
        return replace(
            batch,
            results=() if self.empty_first and self.search_calls == 1 else tuple(
                WebSearchResult("公开企业资料", f"https://acme.example/{prefix}/{i}", "")
                for i in range(2)
            ),
        )


class RecordingPages:
    def __init__(self, search, *, reject_first=False, crash=False):
        self.search = search
        self.calls = []
        self.reject_first = reject_first
        self.rejected_indices = set()
        self.crash = crash
        self.reject_category = ToolErrorCategory.PROVIDER_PERMANENT

    async def read_page(self, tenant, run_id, batch, index):
        self.calls.append((self.search.batch_numbers[batch.handle], index))
        if self.crash:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
        if (self.reject_first and len(self.calls) == 1) or index in self.rejected_indices:
            raise ToolGatewayError(self.reject_category)
        return PageSnapshot("公开企业经营相关品类。", batch.results[index].url, NOW, HASH, ARTIFACT)


class CaptureEveryPage:
    async def run(self, task, context):
        changes = []
        for page in task.inputs["pages"]:
            changes.append({"domain": "demand", "risk_level": "low", "operation": "capture_signal", "payload": {
                "signal_type": "marketplace_seller_activity",
                "raw_observation": page["text"],
                "source_url": page["url"],
                "page_hash": page["content_hash"],
                "snapshot_artifact_ref": page["snapshot_artifact_ref"],
                "research_evidence": page["research_evidence"].model_dump(mode="json"),
                "extracted_by": "model:test",
            }})
        return ChangeSet(ChangeSetId(new_id("cs")), task.tenant_id, task.run_id, changes)


def build_step(*, page_budget=3, signal_budget=6, **search_options):
    run = _run()
    plan = replace(research_plan(), max_pages_read=page_budget, max_signals=signal_budget)
    search = RecordingSearch(run, **search_options)
    pages, demand = RecordingPages(search), _Demand()
    step = ResearchExecuteSearchStep(
        _Reader(plan), search, pages, CaptureEveryPage(), demand, _Prospecting(),
        free_search_enabled=True,
    )
    return run, step, search, pages, demand


@pytest.mark.parametrize("budget,expected", [
    (3, [(1, 0), (2, 0), (3, 0)]),
    (5, [(1, 0), (2, 0), (3, 0), (1, 1), (2, 1)]),
    (6, [(1, 0), (2, 0), (3, 0), (1, 1), (2, 1), (3, 1)]),
])
async def test_queries_receive_one_page_before_any_query_gets_its_second(budget, expected):
    run, step, search, pages, demand = build_step(page_budget=budget)
    outcome = await step.execute(run)
    assert pages.calls == expected
    assert outcome[2]["pages_used"] == budget
    assert outcome[2]["searches_used"] == 3
    assert len(demand.requests) == budget
    assert search.discarded == 1


async def test_same_url_from_multiple_queries_still_requires_each_page_authorization():
    run, step, search, pages, demand = build_step(
        page_budget=3, signal_budget=3, duplicate_urls=True,
    )
    outcome = await step.execute(run)
    assert pages.calls == [(1, 0), (2, 0), (3, 0)]
    assert outcome[2]["pages_used"] == 3
    assert outcome[2]["searches_used"] == 3
    assert len(demand.requests) == 3
    assert {request.research_evidence.discovery_lane for request in demand.requests} == {
        "importer", "distributor", "ecommerce",
    }
    assert search.discarded == 1


async def test_empty_source_does_not_prevent_other_queries_or_waste_page_budget():
    run, step, search, pages, _ = build_step(page_budget=3, empty_first=True)
    outcome = await step.execute(run)
    assert pages.calls == [(2, 0), (3, 0), (2, 1)]
    assert outcome[2]["signal_count"] == 3
    assert outcome[2]["searches_used"] == 3
    assert search.discarded == 1


async def test_rejected_first_source_still_spreads_remaining_attempts():
    run, step, search, pages, _ = build_step(page_budget=3)
    pages.reject_first = True
    outcome = await step.execute(run)
    assert pages.calls == [(1, 0), (2, 0), (3, 0)]
    assert outcome[2]["signal_count"] == 2
    assert outcome[2]["pages_used"] == 3
    assert search.discarded == 1


@pytest.mark.parametrize("category", [
    ToolErrorCategory.PAGE_ACCESS_FORBIDDEN,
    ToolErrorCategory.LOGIN_OR_CAPTCHA,
    ToolErrorCategory.UNSAFE_REDIRECT,
])
async def test_unreadable_page_does_not_block_other_independent_sources(category):
    run, step, search, pages, _ = build_step(page_budget=3)
    pages.reject_first = True
    pages.reject_category = category
    outcome = await step.execute(run)
    assert outcome[2]["pages_used"] == 3
    assert outcome[2]["signal_count"] == 2
    assert search.search_calls == 3
    assert search.discarded == 1


@pytest.mark.parametrize("first_rejected,stop_at,page_budget,expected_pages,expected_signals", [
    (False, 2, 3, [(1, 0)], 1),
    (True, 2, 5, [(1, 0), (1, 1)], 1),
    (True, 3, 6, [(1, 0), (1, 1), (2, 0), (2, 1)], 2),
])
async def test_real_search_slot_invalidated_by_quota_stop_preserves_prior_evidence(
    first_rejected, stop_at, page_budget, expected_pages, expected_signals,
):
    from types import SimpleNamespace

    from tool_gateway.errors import ToolCallStatus
    from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher
    from tool_gateway.handlers.web_search import ToolGatewayWebSearcher
    from tool_gateway.handlers.web_slots import WebSearchResultSlot
    from tool_gateway.pipeline import ToolCallResult

    run, step, _, _, demand = build_step(page_budget=page_budget)
    slot = WebSearchResultSlot(new_id, maximum_batches=100)
    handles = []
    page_calls = []

    class Gateway:
        calls = 0

        async def invoke(self, ctx):
            self.calls += 1
            if self.calls == stop_at:
                raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
            batch = slot.put(ctx.tenant_id, "US", "hinges", (
                WebSearchResult("公开资料", "https://acme.example/about", ""),
                WebSearchResult("更多资料", "https://acme.example/other", ""),
            ))
            handles.append(batch.handle)
            return ToolCallResult(ctx.tool_id, ToolCallStatus.SUCCEEDED, output={"provider_ref": batch.handle})

    class Quota:
        async def run_state(self, run_id):
            return SimpleNamespace(stop_reason=FreeSearchStopReason.QUOTA_EXHAUSTED)

    class Pages:
        async def read_page(self, tenant, run_id, batch, index):
            slot.get_batch(batch.handle)
            page_calls.append((handles.index(batch.handle) + 1, index))
            if first_rejected and index == 0:
                raise ToolGatewayError(ToolErrorCategory.PAGE_ACCESS_FORBIDDEN)
            return PageSnapshot("公开经营信息。", batch.results[index].url, NOW, HASH, ARTIFACT)

    gateway = Gateway()
    step._searcher = FreeSearchGatewaySearcher(
        ToolGatewayWebSearcher(gateway, slot, "employee:manager"), Quota(), run.tenant_id,
    )
    step._pages = Pages()
    result = await step.execute(run)
    assert result[2]["completion_reason"] == "quota_exhausted"
    assert page_calls == expected_pages
    assert result[2]["pages_used"] == len(expected_pages)
    assert result[2]["signal_count"] == len(demand.requests) == expected_signals
    assert gateway.calls == result[2]["searches_used"] == stop_at
    from shared.errors import ValidationError

    with pytest.raises(ValidationError, match="句柄无效"):
        slot.get_batch(handles[0])


@pytest.mark.parametrize("budget,expected,signals", [
    (3, [(1, 0), (2, 0), (3, 0)], 0),
    (4, [(1, 0), (1, 1), (2, 0), (3, 0)], 1),
    (5, [(1, 0), (1, 1), (2, 0), (2, 1), (3, 0)], 2),
    (6, [(1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)], 3),
])
async def test_rejected_pages_use_fair_query_allowance_before_searching_again(
    budget, expected, signals,
):
    """额外尝试不能挤占后续查询的至少一次页面机会，也不能超过总预算。"""
    run, step, search, pages, demand = build_step(page_budget=budget)
    pages.rejected_indices = {0}
    pages.reject_category = ToolErrorCategory.PAGE_ACCESS_FORBIDDEN

    outcome = await step.execute(run)

    assert pages.calls == expected
    assert outcome[2]["pages_used"] == budget
    assert outcome[2]["signal_count"] == len(demand.requests) == signals
    assert search.search_calls == 3
    assert {batch_number for batch_number, _ in pages.calls} == {1, 2, 3}
    assert search.discarded == 1


async def test_all_blocked_results_stop_at_global_page_budget_without_extra_searches():
    run, step, search, pages, demand = build_step(page_budget=5)
    pages.rejected_indices = {0, 1}
    pages.reject_category = ToolErrorCategory.LOGIN_OR_CAPTCHA

    outcome = await step.execute(run)

    assert pages.calls == [(1, 0), (1, 1), (2, 0), (2, 1), (3, 0)]
    assert outcome[2]["pages_used"] == 5
    assert outcome[2]["completion_reason"] == "page_disallowed"
    assert outcome[2]["signal_count"] == len(demand.requests) == 0
    assert search.search_calls == 3
    assert search.discarded == 1


@pytest.mark.parametrize("page_budget,signal_budget,expected,signals,searches", [
    (2, 6, [(1, 0), (2, 0)], 0, 2),
    (6, 1, [(1, 0), (1, 1)], 1, 1),
])
async def test_rejected_page_fallback_respects_page_and_signal_capacity(
    page_budget, signal_budget, expected, signals, searches,
):
    run, step, search, pages, demand = build_step(
        page_budget=page_budget, signal_budget=signal_budget,
    )
    pages.rejected_indices = {0}
    pages.reject_category = ToolErrorCategory.PAGE_ACCESS_FORBIDDEN

    outcome = await step.execute(run)

    assert pages.calls == expected
    assert outcome[2]["pages_used"] == len(expected) <= page_budget
    assert outcome[2]["signal_count"] == len(demand.requests) == signals
    assert search.search_calls == searches
    assert search.discarded == 1


@pytest.mark.parametrize("category", [
    ToolErrorCategory.PERMISSION_DENIED,
    ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
    ToolErrorCategory.RATE_LIMITED,
])
async def test_non_skippable_page_denial_never_uses_fallback_allowance(category):
    run, step, search, pages, _ = build_step(page_budget=6)
    pages.reject_first = True
    pages.reject_category = category

    with pytest.raises(ToolGatewayError) as failure:
        await step.execute(run)

    assert failure.value.category is category
    assert pages.calls == [(1, 0)]
    assert search.search_calls == 1
    assert search.discarded == 1


async def test_quota_failure_keeps_existing_evidence_and_reason_without_more_searches():
    run, step, search, _pages, _ = build_step(page_budget=3, stop_at=2)
    outcome = await step.execute(run)
    assert outcome[2]["completion_reason"] == "quota_exhausted"
    assert outcome[2]["signal_count"] >= 1
    assert search.search_calls == 1
    assert search.discarded == 1


async def test_page_failure_releases_every_held_batch():
    run, step, search, pages, _ = build_step()
    pages.crash = True
    with pytest.raises(ToolGatewayError):
        await step.execute(run)
    assert search.discarded == 1


async def test_small_page_budget_covers_different_sources_before_repeating_public_web():
    from agent_runtime.assistant.discovery_queries import build_discovery_queries
    from workflows.demand_discovery.ports import DiscoverySearchQuery

    run, step, search, _pages, demand = build_step(page_budget=3)
    queries = build_discovery_queries(
        countries=("US",), categories=("hinges",), max_queries=10, result_limit=2,
    )
    step._reader = _Reader(replace(
        research_plan(), max_search_queries=10, max_pages_read=3, max_signals=6,
        queries=tuple(DiscoverySearchQuery(**vars(q)) for q in queries),
    ))
    outcome = await step.execute(run)
    assert outcome[2]["planned_source_channels"] == [
        "public_web", "industry_directory", "association_members", "trade_show_exhibitors",
        "public_procurement", "company_news", "public_linkedin_company", "public_trade_records",
    ]
    assert outcome[2]["searched_source_channels"] == [
        "public_web", "industry_directory", "association_members",
    ]
    assert outcome[2]["source_channels"] == outcome[2]["searched_source_channels"]
    assert outcome[2]["completion_reason"] == "budget_exhausted"
    assert len(demand.requests) == 3
    assert search.search_calls == 3


def test_source_rotation_also_spreads_limited_capacity_across_markets_and_categories():
    from collections import Counter

    from agent_runtime.assistant.discovery_queries import build_discovery_queries
    from workflows.demand_discovery.ports import DiscoverySearchQuery
    from workflows.demand_discovery.research import _spread_queries

    queries = tuple(DiscoverySearchQuery(**vars(q)) for q in build_discovery_queries(
        countries=("US", "DE"), categories=("hinges", "fasteners"),
        max_queries=40, result_limit=2,
    ))
    scheduled = _spread_queries(queries)
    assert Counter((q.country, q.category) for q in scheduled[:8]) == {
        ("US", "hinges"): 2, ("US", "fasteners"): 2,
        ("DE", "hinges"): 2, ("DE", "fasteners"): 2,
    }
    assert Counter(q.query for q in scheduled) == Counter(q.query for q in queries)


@pytest.mark.parametrize("url,description", [
    ("https://www.linkedin.com/company/acme", ""),
    ("https://de.linkedin.com/company/acme", ""),
    ("https://www.importyeti.com/company/acme", ""),
    ("https://www.importgenius.com/importers/acme", ""),
    ("https://panjiva.com/Acme/1234", ""),
    ("https://ted.europa.eu/en/notice/1234-2026", ""),
    ("https://www.find-tender.service.gov.uk/Notice/1234-2026", ""),
    ("https://expo.example/exhibitors/acme", ""),
    ("https://association.example/members/acme", ""),
    ("https://expo.example/profile/acme", "Exhibitor profile. "),
    ("https://association.example/profile/acme", "Association member profile. "),
])
def test_third_party_profile_cannot_become_the_buyers_own_website(url, description):
    evidence = ResearchEvidence.from_page(
        proposal_id="proposal:test", query="US hinges importer", discovery_lane="importer",
        query_country="US", query_category="hinges", url=url,
        text=description + "We are Acme Tools, an importer. We are based in US.",
    )
    assert evidence.identity_status == "pending_verification"
    assert evidence.website_domain is None
    assert evidence.country is None


def test_company_domain_containing_a_platform_name_is_not_misclassified_by_substring():
    evidence = ResearchEvidence.from_page(
        proposal_id="proposal:test", query="US hinges importer", discovery_lane="importer",
        query_country="US", query_category="hinges", url="https://linkedin-tools.example/about",
        text="We are Acme Tools, an importer. We are based in US.",
    )
    assert evidence.identity_status == "self_described"
    assert evidence.website_domain == "linkedin-tools.example"
