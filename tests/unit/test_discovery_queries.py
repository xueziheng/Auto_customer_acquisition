"""研究预算应购买不同公开来源的覆盖，不重复相同的三条查询。"""

from collections import Counter

import pytest

from agent_runtime.assistant.proposal import ResearchProposalBuilder
from domains.assistant.schemas import Clarification, ResearchDraft, SourcedField
from domains.directives.schemas import DemandDiscoveryPlanInput
from tests.unit.test_assistant_decision import context, turn


async def _proposal(**changes: str) -> DemandDiscoveryPlanInput | Clarification:
    fields = {
        "objective": "研究铰链需求",
        "target_countries": "US",
        "target_categories": "hinges",
        "excluded_countries": "CN",
        "excluded_categories": "toys",
        "max_search_queries": "10",
        "max_pages_read": "10",
        "max_signals": "10",
        "max_hypotheses": "3",
        "minimum_confidence_tier": "low_mid",
        "strategy_group": "demand_first",
        "query_limit": "3",
        **changes,
    }
    original = turn("query-scope").model_copy(
        update={"input_text": "；".join(f"{key}={value}" for key, value in fields.items())}
    )
    draft = ResearchDraft(
        fields=tuple(
            SourcedField(name=key, value=value, source_turn_id="query-scope")
            for key, value in fields.items()
        )
    )
    return await ResearchProposalBuilder().build(context(original), draft)


async def test_confirmed_extra_budget_adds_independent_public_discovery_sources():
    plan = await _proposal()
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert len(plan.queries) == 10
    queries = tuple(item.query for item in plan.queries)
    for wording in (
        "industry directory",
        "association member",
        "trade show exhibitor",
        "request for quotation",
        "company expansion",
        "site:linkedin.com/company/",
        "public shipment records",
    ):
        assert any(wording in query for query in queries)
    assert plan.max_search_queries == 10
    assert plan.target_countries == ("US",)
    assert plan.excluded_countries == ("CN",)
    assert plan.target_categories == ("hinges",)
    assert plan.excluded_categories == ("toys",)
    assert all(item.country == "US" and item.category == "hinges" for item in plan.queries)


async def test_original_three_query_budget_keeps_original_public_research_plan():
    plan = await _proposal(max_search_queries="3")
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert tuple(item.query for item in plan.queries) == (
        "US hinges importer", "US hinges distributor", "US hinges ecommerce"
    )


async def test_extra_queries_are_distributed_across_confirmed_markets_and_categories():
    plan = await _proposal(
        target_countries="US,DE", target_categories="hinges,fasteners",
        max_search_queries="19",
    )
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert len(plan.queries) == 19
    counts = Counter((query.country, query.category) for query in plan.queries)
    assert set(counts) == {
        ("US", "hinges"), ("US", "fasteners"),
        ("DE", "hinges"), ("DE", "fasteners"),
    }
    assert max(counts.values()) - min(counts.values()) <= 1


async def test_upper_budget_is_not_filled_with_repeated_or_invented_queries():
    plan = await _proposal(max_search_queries="100")
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert 10 <= len(plan.queries) < 100
    assert len({query.query for query in plan.queries}) == len(plan.queries)
    assert all(len(query.query) <= 400 and len(query.query.split()) <= 50 for query in plan.queries)


@pytest.mark.parametrize(
    ("country", "notice_path"),
    [("DE", "site:ted.europa.eu/en/notice/"),
     ("GB", "site:find-tender.service.gov.uk/Notice/")],
)
async def test_official_notice_searches_use_current_public_notice_paths(country, notice_path):
    plan = await _proposal(target_countries=country, max_search_queries="100")
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert any(notice_path in query.query for query in plan.queries)


async def test_budget_too_small_for_explicit_scope_still_requires_narrowing():
    result = await _proposal(target_countries="US,DE", max_search_queries="5")
    assert isinstance(result, Clarification)
    assert "max_search_queries" in result.missing_fields


@pytest.mark.parametrize("category", ["a " * 49 + "a", "hinges\tfasteners"])
async def test_category_cannot_produce_queries_exceeding_gateway_input_shape(category):
    result = await _proposal(target_categories=category)
    assert isinstance(result, Clarification)
    assert "target_categories" in result.missing_fields


async def test_search_like_category_does_not_change_explicit_scope_or_forge_base_source():
    from agent_runtime.assistant.discovery_queries import query_source_channel

    category = "hinges site:linkedin.com/company/"
    plan = await _proposal(target_categories=category, max_search_queries="3")
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert plan.target_countries == ("US",)
    assert plan.target_categories == (category,)
    assert all(query.country == "US" and query.category == category for query in plan.queries)
    assert all(query_source_channel(query.query) == "public_web" for query in plan.queries)


async def test_source_labels_describe_only_generated_query_directions():
    from agent_runtime.assistant.discovery_queries import query_source_channel

    plan = await _proposal(max_search_queries="100")
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert {query_source_channel(item.query) for item in plan.queries} == {
        "public_web", "industry_directory", "association_members",
        "trade_show_exhibitors", "public_procurement", "company_news",
        "public_linkedin_company", "public_trade_records",
    }
    for original in (
        "US hinges supplier information from LinkedIn",
        "US hinges site:linkedin.com/company.evil/",
        "US hinges site:linkedin.com/company/ extra",
        "US hinges site:ted.europa.eu/en/notice/",
        "US \nhinges site:linkedin.com/company/",
    ):
        assert query_source_channel(original) == "public_web"


@pytest.mark.parametrize("budget", range(12, 42))
async def test_partial_budget_distributes_additional_sources_evenly(budget):
    plan = await _proposal(
        target_countries="US,DE", target_categories="hinges,fasteners",
        max_search_queries=str(budget),
    )
    assert isinstance(plan, DemandDiscoveryPlanInput)
    assert len(plan.queries) <= budget
    counts = Counter((query.country, query.category) for query in plan.queries)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert len({(query.query, query.country, query.category) for query in plan.queries}) == len(plan.queries)
