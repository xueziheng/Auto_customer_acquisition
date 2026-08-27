"""研究模式不借用触达授权，不把目录或目标国家升级为企业事实。"""

import importlib
from dataclasses import replace

import pytest

_directive_models = importlib.import_module("domains.directives.models")
DemandDiscoveryConfig = _directive_models.DemandDiscoveryConfig
DiscoverySearchQueryConfig = _directive_models.DiscoverySearchQueryConfig
from domains.directives.service_impl import _validate_demand_discovery
from shared.errors import ValidationError
from tests.unit.workflows.test_demand_discovery import _plan
from workflows.demand_discovery.flow import build_demand_discovery_definition
from workflows.demand_discovery.steps import _validate_plan


def research_plan():
    plan = _plan()
    query = plan.queries[0]
    return replace(
        plan,
        execution_mode="research_only",
        campaign_id="",
        assessment_ref="",
        role_hints=(),
        max_search_queries=3,
        queries=tuple(
            replace(query, query=f"US hinges {lane}", discovery_lane=lane)
            for lane in ("importer", "distributor", "ecommerce")
        ),
    )


def test_research_plan_accepts_three_lanes_without_campaign():
    plan = research_plan()
    _validate_plan(plan)
    config = DemandDiscoveryConfig(
        **{
            **vars(plan),
            "queries": [DiscoverySearchQueryConfig(**vars(q)) for q in plan.queries],
            **{
                key: list(getattr(plan, key))
                for key in (
                    "target_countries",
                    "target_categories",
                    "excluded_countries",
                    "excluded_categories",
                    "role_hints",
                )
            },
        }
    )
    _validate_demand_discovery(config)


@pytest.mark.parametrize("field", ["campaign_id", "assessment_ref"])
def test_legacy_default_still_requires_outreach_fields(field):
    with pytest.raises(ValidationError):
        _validate_plan(replace(_plan(), **{field: ""}))


@pytest.mark.parametrize(
    "patch",
    [
        {"max_search_queries": 2},
        {"excluded_countries": ("US",)},
        {"excluded_categories": ("hinges",)},
        {"target_countries": ("DE",)},
        {"target_categories": ("chairs",)},
    ],
)
def test_research_three_lane_plan_cannot_exceed_confirmed_scope(patch):
    with pytest.raises(ValidationError):
        _validate_plan(replace(research_plan(), **patch))


def test_v2_has_distinct_handlers_without_changing_v1_replay_definition():
    old = build_demand_discovery_definition()
    new = build_demand_discovery_definition(version=2)
    assert old.version == 1 and new.version == 2
    assert old.steps[1].handler_ref == "demand_discovery.execute_search"
    assert new.steps[1].handler_ref != old.steps[1].handler_ref
    assert new.steps[1].max_retries == 0


def test_historical_persisted_plan_without_mode_or_lane_keeps_outreach_semantics():
    import json
    from dataclasses import asdict

    from infra.db.repositories.directives import _content_from_json

    old_plan = json.loads(json.dumps(asdict(_plan())))
    old_plan.pop("execution_mode")
    for query in old_plan["queries"]:
        query.pop("discovery_lane")
    content = _content_from_json(
        {
            "objective": "discover_and_validate_demand",
            "market_assignments": [],
            "discovery": None,
            "demand_discovery": old_plan,
            "outreach": None,
            "handoff": None,
            "paused_markets": [],
            "monthly_budget_credits": None,
            "notes": None,
        }
    )
    assert content.demand_discovery.execution_mode == "outreach_preparation"
    assert all(q.discovery_lane is None for q in content.demand_discovery.queries)
    _validate_demand_discovery(content.demand_discovery)


@pytest.mark.parametrize(
    "text,url,status,kind",
    [
        (
            "We are Acme Tools, an importer of hinges. We are based in the United States.",
            "https://acme.example/about",
            "self_described",
            "company_self_description",
        ),
        (
            "We are Acme Tools, a distributor of hinges. Contact us.",
            "https://acme.example/about",
            "pending_verification",
            "company_self_description",
        ),
        (
            "We are Acme Tools, an online store for hinges. We ship to the United States.",
            "https://acme.us/about",
            "pending_verification",
            "company_self_description",
        ),
        (
            "Distributor directory: Acme Tools is based in the United States. Website acme.example.",
            "https://directory.example/companies",
            "pending_verification",
            "directory_listing",
        ),
        (
            "We are Acme Tools, an importer. Branch offices in United States.",
            "https://acme.example/about",
            "pending_verification",
            "company_self_description",
        ),
    ],
)
def test_research_source_identity_requires_self_description_and_location(
    text, url, status, kind
):
    from domains.demand.schemas import ResearchEvidence

    evidence = ResearchEvidence.from_page(
        proposal_id="proposal:test",
        query="US hinges importer",
        discovery_lane="importer",
        query_country="US",
        query_category="hinges",
        text=text,
        url=url,
    )
    assert evidence.identity_status == status
    assert evidence.source_kind == kind
    if status == "self_described":
        assert evidence.company_name == "Acme Tools"
        assert evidence.website_domain == "acme.example"
        assert evidence.country == "US"
        assert evidence.identity_quote in text and evidence.country_quote in text
    else:
        assert evidence.country is None


def test_directory_self_domain_in_footer_cannot_establish_buyer_identity():
    from domains.demand.schemas import ResearchEvidence

    evidence = ResearchEvidence.from_page(
        proposal_id="proposal:test",
        query="US hinges distributor",
        discovery_lane="distributor",
        query_country="US",
        query_category="hinges",
        url="https://directory.example/dealers",
        text="Dealer directory. We are Directory Inc, a directory. We are based in US. directory.example",
    )
    assert evidence.identity_status == "pending_verification"
    assert evidence.website_domain is None


async def test_research_v2_does_not_queue_contacts_even_above_threshold():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shared.schemas.evidence import ConfidenceTier
    from shared.schemas.identifiers import new_id
    from tests.unit.workflows.test_demand_discovery import _Demand, _Reader, _run
    from workflows.demand_discovery.flow import build_demand_discovery_handlers

    queue = AsyncMock()
    demand = _Demand()
    demand.get_confidence = AsyncMock(
        return_value=SimpleNamespace(tier=ConfidenceTier.EXTREME)
    )
    handlers = build_demand_discovery_handlers(
        task_reader=_Reader(research_plan()),
        searcher=object(),
        page_reader=object(),
        capability=object(),
        demand=demand,
        prospecting=object(),
        account_queue=queue,
    )
    run = _run()
    run.context["hypothesis_ids"] = [new_id("hyp")]
    result = await handlers["demand_discovery.v2.score_and_queue"].execute(run)
    assert result[2]["queued_count"] == 0
    assert result[2]["validated_need_count"] == 0
    assert result[2]["qualified_opportunity_count"] == 0
    demand.get_confidence.assert_awaited_once()
    queue.start.assert_not_called()


async def test_research_v2_never_uses_unconfirmed_paid_provider():
    from tests.unit.workflows.test_demand_discovery import (
        _Demand,
        _Reader,
        _run,
        _Searcher,
    )
    from workflows.demand_discovery.flow import build_demand_discovery_handlers

    run = _run()
    searcher = _Searcher(run)
    handlers = build_demand_discovery_handlers(
        task_reader=_Reader(research_plan()),
        searcher=searcher,
        page_reader=object(),
        capability=object(),
        demand=_Demand(),
        prospecting=object(),
        account_queue=None,
    )
    result = await handlers["demand_discovery.v2.execute_search"].execute(run)
    assert result[0] == "complete"
    assert result[2]["completion_reason"] == "unsupported"
    assert searcher.search_calls == 0


@pytest.mark.parametrize("lane", ["importer", "distributor", "ecommerce"])
async def test_research_run_creates_only_signals_and_hypotheses_for_each_lane(lane):
    import json
    from unittest.mock import AsyncMock

    from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from connectors.web_search.client import PageSnapshot
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
    from workflows.demand_discovery.flow import build_demand_discovery_handlers

    text = "We are Acme Tools, an importer and distributor with an online store. We are based in US."

    class Pages:
        async def read_page(self, *args):
            return PageSnapshot(text, "https://acme.example/about", NOW, HASH, ARTIFACT)

    class Model:
        async def analyze_pages(self, **kwargs):
            return json.dumps(
                {
                    "signals": [
                        {
                            "signal_type": "marketplace_seller_activity",
                            "source_page_index": 0,
                            "source_excerpt": text,
                            "possible_need": "hinges",
                            "evidence_level": "agent_industry_inference",
                        }
                    ],
                    "hypotheses": [
                        {
                            "account_name_signal_index": 0,
                            "country_signal_index": 0,
                            "country": "US",
                            "category": "hinges",
                            "signal_indexes": [0],
                            "reasoning": "企业经营相关商品，可能需要铰链，值得验证",
                        }
                    ],
                }
            )

    run = _run()
    plan = research_plan()
    plan = replace(
        plan,
        queries=tuple(sorted(plan.queries, key=lambda q: q.discovery_lane != lane)),
    )
    demand, prospecting, queue = _Demand(), _Prospecting(), AsyncMock()
    searcher = _Searcher(run)
    handlers = build_demand_discovery_handlers(
        task_reader=_Reader(plan),
        searcher=searcher,
        page_reader=Pages(),
        capability=DemandIntelligenceAgent(
            "model-v2", Model(), None, CredentialMarkerGuard()
        ),
        demand=demand,
        prospecting=prospecting,
        account_queue=queue,
        free_search_enabled=True,
    )
    result = await handlers["demand_discovery.v2.execute_search"].execute(run)
    assert result[2]["signal_count"] == result[2]["hypothesis_count"] == 1
    assert (
        result[2]["validated_need_count"]
        == result[2]["qualified_opportunity_count"]
        == 0
    )
    assert demand.requests[0].research_evidence.discovery_lane == lane
    assert prospecting.requests[0].country == "US"
    assert prospecting.requests[0].entity_name == "Acme Tools"
    queue.start.assert_not_called()


@pytest.mark.parametrize(
    "reason",
    [
        "quota_exhausted",
        "usage_unknown",
        "paid_enabled",
        "request_uncertain",
        "unsupported",
    ],
)
async def test_free_search_stop_reason_is_not_reported_as_no_results(reason):
    from tests.unit.workflows.test_demand_discovery import (
        _Demand,
        _Reader,
        _run,
        _Searcher,
    )
    from tool_gateway.free_search_contracts import FreeSearchError, FreeSearchStopReason
    from workflows.demand_discovery.flow import build_demand_discovery_handlers

    run = _run()

    class Stopped(_Searcher):
        async def search(self, *args):
            raise FreeSearchError(FreeSearchStopReason(reason))

    handlers = build_demand_discovery_handlers(
        task_reader=_Reader(research_plan()),
        searcher=Stopped(run),
        page_reader=object(),
        capability=object(),
        demand=_Demand(),
        prospecting=object(),
        account_queue=None,
        free_search_enabled=True,
    )
    result = await handlers["demand_discovery.v2.execute_search"].execute(run)
    assert result[2]["completion_reason"] == reason
    assert result[2]["hypothesis_count"] == 0


async def test_v2_preserves_legacy_outreach_preparation_queue_semantics():
    from unittest.mock import AsyncMock

    from shared.schemas.evidence import EvidenceItem, EvidenceLevel, derive_confidence
    from shared.schemas.identifiers import new_id
    from tests.unit.workflows.test_demand_discovery import _Reader, _run
    from workflows.demand_discovery.flow import build_demand_discovery_handlers

    class Demand:
        async def get_confidence(self, tenant, hypothesis_id):
            from tests.unit.workflows.test_demand_discovery import NOW

            return derive_confidence(
                [
                    EvidenceItem(
                        level=EvidenceLevel.PUBLIC_COMPANY_EVENT,
                        source_type="web_page",
                        source_id="source:test",
                        observed_at=NOW,
                        summary="公开企业变化",
                    )
                ],
                now=NOW,
            )

    queue = AsyncMock()
    queue.start.return_value = new_id("run")
    handlers = build_demand_discovery_handlers(
        task_reader=_Reader(replace(_plan(), minimum_confidence_tier="low")),
        searcher=object(),
        page_reader=object(),
        capability=object(),
        demand=Demand(),
        prospecting=object(),
        account_queue=queue,
    )
    run = _run()
    run.workflow_version = 2
    run.context["hypothesis_ids"] = [new_id("hyp")]
    result = await handlers["demand_discovery.v2.score_and_queue"].execute(run)
    assert result[2]["queued_count"] == 1
    assert queue.start.await_count == 1


def test_research_public_rfq_is_not_customer_reply_evidence():
    models = importlib.import_module("domains.demand.models")
    DemandSignal, SignalType = models.DemandSignal, models.SignalType
    from domains.demand.schemas import ResearchEvidence
    from shared.schemas.identifiers import DemandSignalId, TenantId
    from shared.schemas.provenance import Provenance, SourceType
    from tests.unit.workflows.test_demand_discovery import HASH, NOW

    evidence = ResearchEvidence.from_page(
        proposal_id="proposal:test",
        query="US hinges importer",
        discovery_lane="importer",
        query_country="US",
        query_category="hinges",
        text="We are Acme Tools, an importer. We are based in US.",
        url="https://acme.example/about",
    )
    signal = DemandSignal(
        DemandSignalId("sig:test"),
        TenantId("tenant:test"),
        SignalType.PUBLIC_RFQ,
        "Acme Tools",
        "A public request",
        Provenance(
            source_type=SourceType.WEB_PAGE,
            source_id=HASH,
            extracted_by="model-v2",
            extracted_at=NOW,
            source_url=evidence.source_url,
            page_hash=HASH,
        ),
        NOW,
        research_evidence=evidence,
    )
    assert signal.evidence_level.value == "public_company_event"
