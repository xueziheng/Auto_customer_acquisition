"""需求探索 workflow 的确认门、硬预算与证据传递验收。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agent_runtime.base import ChangeSet
from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from connectors.web_search.client import PageSnapshot, WebSearchResult
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    ChangeSetId,
    RunId,
    TenantId,
    new_id,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.demand_discovery.ports import DemandDiscoveryPlan, DiscoverySearchQuery
from workflows.demand_discovery.steps import ExecuteSearchStep
from workflows.engine.runner import StepStatus, WorkflowRun

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
HASH = "a" * 64
ARTIFACT = ArtifactId("art_01K3H0T8NBWM3KGT9XQ06YRC5V")


def _plan(**overrides: object) -> DemandDiscoveryPlan:
    fields: dict[str, object] = {
        "objective": "探索公开需求信号",
        "queries": (
            DiscoverySearchQuery("industrial hinges expansion", "US", "hinges", 2),
            DiscoverySearchQuery("industrial hinges factory", "US", "hinges", 2),
        ),
        "target_countries": ("US",),
        "target_categories": ("hinges",),
        "excluded_countries": (),
        "excluded_categories": (),
        "max_search_queries": 2,
        "max_pages_read": 2,
        "max_signals": 1,
        "max_hypotheses": 1,
        "minimum_confidence_tier": "low_mid",
        "strategy_group": "company_change",
        "campaign_id": new_id("cmp"),
        "role_hints": ("procurement",),
        "assessment_ref": "lia:synthetic-v1",
    }
    fields.update(overrides)
    return DemandDiscoveryPlan(**fields)  # type: ignore[arg-type]


def _run() -> WorkflowRun:
    proposal_id = "proposal:synthetic-v1"
    return WorkflowRun(
        RunId(new_id("run")),
        TenantId(new_id("tn")),
        "demand_discovery",
        1,
        proposal_id,
        "execute_search",
        StepStatus.RUNNING,
        NOW,
        context={
            "proposal_id": proposal_id,
            "acting_user_id": "employee:manager",
        },
    )


class _Reader:
    def __init__(self, plan: DemandDiscoveryPlan | None) -> None:
        self.plan = plan
        self.calls = 0

    async def load_confirmed(self, tenant_id, proposal_id, acting_user):
        del tenant_id, proposal_id, acting_user
        self.calls += 1
        if self.plan is None:
            raise ValidationError("需求探索提案未确认或已过期")
        return self.plan


class _Searcher:
    def __init__(self, run: WorkflowRun) -> None:
        self.run = run
        self.search_calls = 0
        self.released: list[str] = []
        self.discarded = 0

    async def search(self, tenant_id, run_id, query, country, category, limit):
        del query, limit
        assert tenant_id == self.run.tenant_id and run_id == self.run.run_id
        self.search_calls += 1
        return SearchResultBatch(
            new_id("wsb"),
            tenant_id,
            country,
            category,
            (
                WebSearchResult("one", "https://example.com/one", ""),
                WebSearchResult("two", "https://example.com/two", ""),
            ),
        )

    def release(self, batch):
        self.released.append(batch.handle)

    def discard_all(self):
        self.discarded += 1


class _PageReader:
    def __init__(
        self,
        *,
        first_permanent_failure: bool = False,
        url: str | None = None,
    ) -> None:
        self.calls = 0
        self.first_permanent_failure = first_permanent_failure
        self.url = url

    async def read_page(self, tenant_id, run_id, batch, result_index):
        del tenant_id, run_id, batch
        self.calls += 1
        if self.first_permanent_failure and result_index == 0:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
        return PageSnapshot(
            "Example.com is headquartered in the US and opened a new production line.",
            self.url or f"https://example.com/{result_index}",
            NOW,
            HASH,
            ARTIFACT,
        )


class _Capability:
    def __init__(self, run: WorkflowRun, changes: list[dict[str, object]]) -> None:
        self.workflow_run = run
        self.changes = changes
        self.calls = 0

    async def run(self, task, context):
        del context
        self.calls += 1
        return ChangeSet(
            ChangeSetId(new_id("cs")),
            self.workflow_run.tenant_id,
            self.workflow_run.run_id,
            self.changes,
        )


class _Demand:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.hypotheses: list[tuple[object, ...]] = []

    async def capture_signal(self, tenant_id, request):
        del tenant_id
        self.requests.append(request)
        return new_id("sig")

    async def create_hypothesis(
        self, tenant_id, account_id, category, refs, reasoning, inferred_by
    ):
        self.hypotheses.append(
            (tenant_id, account_id, category, refs, reasoning, inferred_by)
        )
        return new_id("hyp")


class _Prospecting:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def resolve_account(self, tenant_id, request):
        del tenant_id
        self.requests.append(request)
        return new_id("acc")


def _step(run, reader, searcher, page_reader, capability, demand, prospecting=None):
    return ExecuteSearchStep(
        reader,
        searcher,
        page_reader,
        capability,
        demand,
        prospecting or _Prospecting(),
    )


class _DemandModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def analyze_pages(self, *, system_prompt, discovery):
        self.calls.append({"system_prompt": system_prompt, "discovery": discovery})
        return json.dumps(
            {
                "signals": [
                    {
                        "signal_type": "product_line_expansion",
                        "source_page_index": 0,
                        "source_excerpt": (
                            "Example.com is headquartered in the US and opened "
                            "a new production line."
                        ),
                        "possible_need": "hinges",
                        "evidence_level": "public_company_event",
                    }
                ],
                "hypotheses": [
                    {
                        "account_name_signal_index": 0,
                        "country": "US",
                        "country_signal_index": 0,
                        "category": "hinges",
                        "reasoning": "企业扩产，可能需要铰链，值得验证",
                        "signal_indexes": [0],
                    }
                ],
            }
        )


async def test_production_origin_binds_identity_to_host_not_url_name() -> None:
    run = _run()
    model = _DemandModel()
    capability = DemandIntelligenceAgent(
        "model-v1", model, object(), CredentialMarkerGuard()
    )
    demand = _Demand()
    prospecting = _Prospecting()

    _action, _next, patch = await _step(
        run,
        _Reader(_plan(max_pages_read=1)),
        _Searcher(run),
        _PageReader(
            url="https://EXAMPLE.com/people/Alice-SMITH?ref=alice"
        ),
        capability,
        demand,
        prospecting,
    ).execute(run)

    model_blob = json.dumps(model.calls, ensure_ascii=False).casefold()
    patch_blob = json.dumps(patch, ensure_ascii=False).casefold()
    assert "alice" not in model_blob
    assert "/people/" not in model_blob
    assert demand.requests[0].entity_name == "example.com"
    assert prospecting.requests[0].entity_name == "example.com"
    assert prospecting.requests[0].website_domain == "example.com"
    provenance = prospecting.requests[0].field_provenance
    assert provenance["name"].extracted_by == "system:url-host-v1"
    assert provenance["name"].source_quote == (
        "https://EXAMPLE.com/people/Alice-SMITH?ref=alice"
    )
    assert provenance["country"].extracted_by == "model-v1"
    assert provenance["country"].source_quote.startswith(
        "Example.com is headquartered in the US"
    )
    assert "alice" not in patch_blob
    assert "/people/" not in patch_blob


async def test_unconfirmed_or_stale_proposal_has_zero_io_and_zero_domain_write() -> None:
    run = _run()
    reader = _Reader(None)
    searcher = _Searcher(run)
    page_reader = _PageReader()
    capability = _Capability(run, [])
    demand = _Demand()

    with pytest.raises(ValidationError, match="未确认或已过期"):
        await _step(
            run, reader, searcher, page_reader, capability, demand
        ).execute(run)

    assert searcher.search_calls == page_reader.calls == capability.calls == 0
    assert demand.requests == []


async def test_search_and_page_caps_charge_failures_and_release_batch() -> None:
    run = _run()
    reader = _Reader(_plan())
    searcher = _Searcher(run)
    page_reader = _PageReader(first_permanent_failure=True)
    capability = _Capability(run, [])
    demand = _Demand()

    action, next_step, patch = await _step(
        run, reader, searcher, page_reader, capability, demand
    ).execute(run)

    assert (action, next_step) == ("advance", "generate_hypotheses")
    assert searcher.search_calls == 1
    assert page_reader.calls == 2
    assert patch["searches_used"] == 1
    assert patch["pages_used"] == 2
    assert patch["completion_reason"] == "budget_exhausted"
    assert len(searcher.released) == 1
    assert searcher.discarded == 0


async def test_snapshot_tuple_reaches_domain_capture_without_workflow_body_leak() -> None:
    run = _run()
    reader = _Reader(_plan(max_pages_read=1))
    searcher = _Searcher(run)
    page_reader = _PageReader()
    changes = [
        {
            "domain": "demand",
            "operation": "capture_signal",
            "risk_level": "low",
            "payload": {
                "signal_type": "product_line_expansion",
                "entity_name": "Acme",
                "raw_observation": (
                    "Example.com is headquartered in the US and opened "
                    "a new production line."
                ),
                "possible_need": "hinges",
                "source_type": "web_page",
                "source_id": HASH,
                "source_url": "https://example.com/0",
                "page_hash": HASH,
                "snapshot_artifact_ref": str(ARTIFACT),
                "observed_at": NOW.isoformat(),
                "evidence_level": "public_company_event",
                "extracted_by": "model-v1",
            },
        }
    ]
    capability = _Capability(run, changes)
    demand = _Demand()

    _action, _next, patch = await _step(
        run, reader, searcher, page_reader, capability, demand
    ).execute(run)

    request = demand.requests[0]
    assert (
        request.source_url,
        request.observed_at,
        request.page_hash,
        request.snapshot_artifact_ref,
    ) == ("https://example.com/0", NOW, HASH, str(ARTIFACT))
    assert "raw_observation" not in patch
    assert "possible_need" not in patch
    assert "Acme opened" not in repr(patch)


async def test_changeset_caps_reject_before_any_domain_write() -> None:
    run = _run()
    reader = _Reader(_plan(max_pages_read=1, max_signals=1))
    searcher = _Searcher(run)
    page_reader = _PageReader()
    signal_change = {
        "domain": "demand",
        "operation": "capture_signal",
        "risk_level": "low",
        "payload": {},
    }
    capability = _Capability(run, [signal_change, signal_change])
    demand = _Demand()

    with pytest.raises(ValidationError, match="超过确认上限"):
        await _step(
            run, reader, searcher, page_reader, capability, demand
        ).execute(run)

    assert demand.requests == []
