"""研究审计投影必须从 tenant+run 的持久预留计算，而不是尝试次数。"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.tables import (
    SearchQuotaAccountRow,
    SearchQuotaReservationRow,
    SearchQuotaRunRow,
    WorkflowRunRow,
)
from shared.schemas.identifiers import RunId, TenantId, new_id

NOW = datetime(2026, 8, 27, tzinfo=UTC)


async def test_research_summary_whitelists_and_aggregates_only_tenant_run(
    integration_engine,
):
    tenant, other = TenantId(new_id("tn")), TenantId(new_id("tn"))
    run, another = RunId(new_id("run")), RunId(new_id("run"))
    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        async with factory() as session, session.begin():
            session.add(SearchQuotaAccountRow(tenant_id=tenant, provider="tavily"))
            await session.flush()
            session.add(
                WorkflowRunRow(
                    tenant_id=tenant,
                    run_id=run,
                    workflow_type="demand_discovery",
                    workflow_version=2,
                    subject_ref="proposal:test",
                    current_step="complete",
                    status="completed",
                    context={
                        "execution_mode": "research_only",
                        "searches_used": 9,
                        "pages_used": 2,
                        "signal_count": 1,
                        "hypothesis_count": 0,
                        "planned_discovery_lanes": [
                            "importer",
                            "distributor",
                            "ecommerce",
                        ],
                        "discovery_lanes": ["importer"],
                        "completion_reason": "budget_exhausted",
                        "sensitive": "must-not-leak",
                    },
                    idempotency_key=run,
                )
            )
            for owner, run_id, states in [
                (tenant, run, ["consumed", "reserved", "uncertain"]),
                (tenant, another, ["consumed"]),
            ]:
                for index, state in enumerate(states):
                    session.add(
                        SearchQuotaReservationRow(
                            tenant_id=owner,
                            provider="tavily",
                            run_id=run_id,
                            request_key=str(index) * 64,
                            status=state,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
            session.add(
                SearchQuotaRunRow(
                    tenant_id=tenant,
                    run_id=run,
                    stop_reason="quota_exhausted",
                    updated_at=NOW,
                )
            )
        repo = PostgresRunAuditRepository(factory)
        detail = await repo.get_run(tenant, run)
        research = getattr(detail.summary, "research", None)
        assert research is not None, "RED：Run 缺少研究白名单摘要"
        assert research.consumed_credits == 1
        assert research.reserved_credits == 1
        assert research.uncertain_credits == 1
        assert research.stop_reason == "request_uncertain"
        assert research.searches_used == 9
        assert research.discovery_lanes == ("importer",)
        assert research.planned_discovery_lanes == (
            "importer",
            "distributor",
            "ecommerce",
        )
        assert "must-not-leak" not in detail.model_dump_json()
        assert await repo.get_run(other, run) is None
        await transaction.rollback()


async def test_account_projection_joins_signal_refs_without_cross_tenant_evidence(
    integration_engine,
):
    from dataclasses import replace
    from importlib import import_module

    import pytest

    from domains.demand.schemas import ResearchEvidence
    from domains.prospecting.schemas import ProspectAccountView
    from shared.schemas.identifiers import ProspectAccountId
    from tests.integration.test_demand_signals import (
        MutableClock,
        _request,
        _seed_raw_artifact,
        _service,
    )

    try:
        reader_type = import_module(
            "infra.db.research_evidence"
        ).PostgresResearchEvidenceReader
    except ModuleNotFoundError:
        pytest.fail("RED：企业研究来源读模型尚未实现")
    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        tenant, other = TenantId(new_id("tn")), TenantId(new_id("tn"))
        await _seed_raw_artifact(factory, tenant)
        request = _request()
        evidence = ResearchEvidence.from_page(
            proposal_id="proposal:test",
            query="US hinges importer",
            discovery_lane="importer",
            query_country="US",
            query_category="hinges",
            text="Directory of businesses",
            url=request.source_url,
        )
        signal_id = await _service(factory, tenant, MutableClock(NOW)).capture_signal(
            tenant,
            replace(request, research_evidence=evidence),
        )
        account = ProspectAccountView(
            ProspectAccountId(new_id("acc")),
            tenant,
            "Synthetic",
            "US",
            NOW,
            source_signal_refs=(str(signal_id),),
        )
        reader = reader_type(factory)
        result = await reader.for_accounts(tenant, [account])
        assert (
            result[str(account.account_id)][0].research_evidence.identity_status
            == "pending_verification"
        )
        assert result[str(account.account_id)][0].page_hash == request.page_hash
        assert (
            await reader.for_accounts(other, [replace(account, tenant_id=other)]) == {}
        )
        await transaction.rollback()
