"""研究证据持久化、跨线路去重和历史提案兼容，真实隔离Postgres。"""

from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from domains.demand.schemas import ResearchEvidence
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_demand_signals import (
    NOW,
    MutableClock,
    _request,
    _seed_raw_artifact,
    _service,
    _signal_rows,
)


@pytest_asyncio.fixture
async def research_connection(
    integration_engine: AsyncEngine,
) -> AsyncIterator[AsyncConnection]:
    """隔离研究数据，避免跨线路唯一键妨碍共享测试库后续历史降级测试。"""
    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


async def test_research_same_page_cross_lane_retains_evidence_but_replay_is_idempotent(
    research_connection,
):
    factory = async_sessionmaker(
        research_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    service = _service(factory, tenant, MutableClock(NOW))
    await _seed_raw_artifact(factory, tenant)
    request = _request()
    ids = []
    for lane in ("importer", "distributor", "ecommerce"):
        evidence = ResearchEvidence.from_page(
            proposal_id="proposal:test",
            query=f"US hinges {lane}",
            discovery_lane=lane,
            query_country="US",
            query_category="hinges",
            text="Business directory of hinge distributors.",
            url=request.source_url,
        )
        item = replace(request, research_evidence=evidence)
        signal_id = await service.capture_signal(tenant, item)
        assert await service.capture_signal(tenant, item) == signal_id
        ids.append(signal_id)
    assert len(set(ids)) == 3
    rows = await _signal_rows(factory, tenant)
    assert len(rows) == 3
    view = await service.list_signals(tenant)
    assert {x.research_evidence.discovery_lane for x in view} == {
        "importer",
        "distributor",
        "ecommerce",
    }
    assert all(
        x.research_evidence.identity_status == "pending_verification" for x in view
    )
    assert await service.list_signals(other) == []
    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    from infra.db.tables import DemandSignalRow

    async with factory() as session, session.begin():
        with pytest.raises(DBAPIError, match="research evidence is immutable"):
            async with session.begin_nested():
                await session.execute(
                    update(DemandSignalRow)
                    .where(
                        DemandSignalRow.tenant_id == tenant,
                        DemandSignalRow.signal_id == ids[0],
                    )
                    .values(research_evidence=None)
                )
    from shared.errors import ValidationError

    with pytest.raises(ValidationError, match="待核验"):
        await service.create_hypothesis(
            tenant,
            new_id("acc"),
            "hinges",
            [ids[0]],
            "可能需要铰链，值得验证",
            "model-v2",
        )


async def test_confirmed_research_v2_runs_without_contacts_and_reuses_account_across_lanes(
    research_connection,
):
    import json
    from unittest.mock import AsyncMock

    from agent_runtime.demand_intelligence.agent import DemandIntelligenceAgent
    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from apps.scheduler_worker.directive_reader import (
        DirectiveDemandDiscoveryTaskReader,
    )
    from connectors.web_search.client import PageSnapshot, WebSearchResult
    from domains.directives.schemas import (
        DemandDiscoveryPlanInput,
        DiscoverySearchQueryInput,
    )
    from domains.directives.service_impl import DirectiveServiceImpl
    from domains.prospecting.service_impl import ProspectingServiceImpl
    from infra.db.directive_uow import SqlAlchemyDirectiveUnitOfWork
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.errors import InvalidStateTransition
    from shared.schemas.identifiers import ArtifactId, EmployeeId, UserId
    from tests.integration.test_demand_signals import PAGE_HASH, SNAPSHOT_ARTIFACT_REF
    from tests.integration.test_phase1_closed_loop import (
        _DirectiveEmployees,
        _StableHasher,
    )
    from tests.unit.workflows.test_research_discovery import research_plan
    from tool_gateway.handlers.web_slots import SearchResultBatch
    from workflows.demand_discovery.flow import (
        build_demand_discovery_handlers,
        register_demand_discovery,
    )

    factory = async_sessionmaker(
        research_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    tenant, boss = TenantId(new_id("tn")), EmployeeId(new_id("emp"))
    directives = DirectiveServiceImpl(
        lambda bound: SqlAlchemyDirectiveUnitOfWork(factory, bound, now=lambda: NOW),
        _DirectiveEmployees(tenant, boss),
        now=lambda: NOW,
    )
    plan = replace(
        research_plan(),
        max_pages_read=3,
        max_signals=3,
        max_hypotheses=3,
        minimum_confidence_tier="low",
    )
    dto = DemandDiscoveryPlanInput(
        **{
            **vars(plan),
            "queries": tuple(
                DiscoverySearchQueryInput(**vars(q)) for q in plan.queries
            ),
        }
    )
    proposal = await directives.submit_discovery_proposal(
        tenant,
        "只研究美国铰链的三线路",
        dto,
        "只研究，不触达",
        ["三查询三页面上限"],
        "model-v2",
    )
    reader = DirectiveDemandDiscoveryTaskReader(directives)
    assert await directives.get_active(tenant) is None
    with pytest.raises(InvalidStateTransition):
        await reader.load_confirmed(tenant, proposal, UserId(str(boss)))
    await directives.confirm_proposal(tenant, proposal, boss)
    assert await reader.load_confirmed(tenant, proposal, UserId(str(boss))) == plan
    view = await directives.get_proposal(tenant, proposal)
    assert view.parsed_fields["execution_mode"] == "research_only"
    assert "discovery_lane" in view.parsed_fields["queries"]
    demand = _service(factory, tenant, MutableClock(NOW))
    prospecting = ProspectingServiceImpl(
        lambda bound: SqlAlchemyProspectingUnitOfWork(factory, bound, now=lambda: NOW),
        _StableHasher(),
        now=lambda: NOW,
    )
    await _seed_raw_artifact(factory, tenant)
    text = "We are Acme Tools, an importer and distributor with an online store. We are based in US."

    class Search:
        async def search(self, tenant, run_id, query, country, category, limit):
            return SearchResultBatch(
                new_id("wsb"),
                tenant,
                country,
                category,
                (WebSearchResult("Acme", "https://acme.example/about", ""),),
            )

        def release(self, batch):
            pass

        def discard_all(self):
            pass

    class Pages:
        async def read_page(self, *args):
            return PageSnapshot(
                text,
                "https://acme.example/about",
                NOW,
                PAGE_HASH,
                ArtifactId(SNAPSHOT_ARTIFACT_REF),
            )

    class Model:
        async def analyze_pages(self, *, discovery, **kwargs):
            count = len(discovery["pages"])
            return json.dumps(
                {
                    "signals": [
                        {
                            "signal_type": "marketplace_seller_activity",
                            "source_page_index": i,
                            "source_excerpt": text,
                            "possible_need": "hinges",
                            "evidence_level": "agent_industry_inference",
                        }
                        for i in range(count)
                    ],
                    "hypotheses": [
                        {
                            "account_name_signal_index": i,
                            "country_signal_index": i,
                            "signal_indexes": [i],
                            "country": "US",
                            "category": "hinges",
                            "reasoning": "经营相关商品，可能需要铰链，值得验证",
                        }
                        for i in range(count)
                    ],
                }
            )

    # 持久服务真实；仅外部搜索、页面和模型响应受控。无联系人组合。
    queue = AsyncMock()
    handlers = build_demand_discovery_handlers(
        task_reader=reader,
        searcher=Search(),
        page_reader=Pages(),
        capability=DemandIntelligenceAgent(
            "model-v2", Model(), None, CredentialMarkerGuard()
        ),
        demand=demand,
        prospecting=prospecting,
        account_queue=queue,
        free_search_enabled=True,
    )
    engine = PostgresWorkflowEngine(factory, handlers, now=lambda: NOW)
    register_demand_discovery(engine)
    run_id = await engine.start(
        tenant,
        "demand_discovery",
        proposal,
        {
            "proposal_id": proposal,
            "acting_user_id": str(boss),
        },
        f"research:{proposal}",
    )
    for _ in range(5):
        await engine.poll_due(tenant, 10)
    from sqlalchemy import select

    from infra.db.tables import WorkflowRunRow

    async with factory() as session:
        row = (
            await session.execute(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == tenant,
                    WorkflowRunRow.run_id == run_id,
                )
            )
        ).scalar_one()
    run = engine._row_to_run(row)
    assert run.workflow_version == 2
    assert run.status.value == "completed"
    assert run.context["signal_count"] == 3
    assert run.context["hypothesis_count"] == 1
    assert (
        run.context["queued_count"]
        == run.context["validated_need_count"]
        == run.context["qualified_opportunity_count"]
        == 0
    )
    queue.start.assert_not_called()
    accounts = await prospecting.list_accounts(tenant)
    assert len(accounts) == 1 and accounts[0].website_domain == "acme.example"
    assert len(accounts[0].source_signal_refs) == 3
    # 重放受控研究动作不重复写Signal或Hypothesis；真实搜索重放仍受持久quota保护。
    repeated = await handlers["demand_discovery.v2.execute_search"].execute(run)
    assert repeated[2]["signal_ids"] == run.context["signal_ids"]
    assert set(repeated[2]["hypothesis_ids"]) == set(run.context["hypothesis_ids"])
