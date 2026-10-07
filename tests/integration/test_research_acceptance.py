"""验收专用 workflow 仅显式组合可用；不扩普通研究入口权限。"""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId, UserId, new_id
from tests.integration.test_search_quota import (
    NOW,
    Artifacts,
    CountryPolicy,
    Pages,
    Playbook,
    SearchTransport,
    Secrets,
    _workflow_run,
)


@asynccontextmanager
async def _source_database(engine):
    """来源链使用独立连接，不以savepoint共享连接隐藏真实事务互锁。"""
    from sqlalchemy import delete

    from infra.db.tables import (
        SearchQuotaAccountRow,
        SearchQuotaReservationRow,
        SearchQuotaRunRow,
    )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    try:
        yield factory, tenant
    finally:
        async with factory() as session, session.begin():
            for table in (
                SearchQuotaRunRow,
                SearchQuotaReservationRow,
                SearchQuotaAccountRow,
            ):
                await session.execute(delete(table).where(table.tenant_id == tenant))


async def test_second_acceptance_entry_is_not_run_while_first_is_in_flight(
    integration_engine,
):
    import asyncio

    from apps.scheduler_worker.research_acceptance import run_source_acceptance
    from apps.scheduler_worker.web_discovery import WebDiscoveryToolComposition
    from tests.unit.workflows.test_research_discovery import research_plan
    from tool_gateway.fingerprint import HmacFingerprintProvider

    started, release = asyncio.Event(), asyncio.Event()

    class BlockingUsage(SearchTransport):
        async def usage(self, *, api_key):
            started.set()
            await release.wait()
            raise ValidationError("受控usage不可读")

    class Reader:
        async def load_confirmed(self, *args):
            return research_plan()

    async with _source_database(integration_engine) as (factory, tenant):
        kwargs = {
            "factory": factory,
            "tenant_id": tenant,
            "actor_id": UserId(new_id("usr")),
            "proposal_id": "proposal:test",
            "reader": Reader(),
            "composition": WebDiscoveryToolComposition(
                Playbook(),
                Secrets(),
                "TAVILY_API_KEY_REF",
                BlockingUsage(),
                Pages(),
                Artifacts(),
                provider="tavily",
                exclusive_account_confirmed=True,
            ),
            "country_policy": CountryPolicy(),
            "fingerprints": HmacFingerprintProvider("v1", b"a" * 32),
            "lease_duration": timedelta(seconds=30),
            "now": lambda: NOW,
        }
        first = asyncio.create_task(run_source_acceptance(**kwargs))
        try:
            await asyncio.wait_for(started.wait(), 5)
            second = await asyncio.wait_for(run_source_acceptance(**kwargs), 3)
            assert second["status"] == "not_run"
            assert second["reason"] == "acceptance_in_progress"
        finally:
            release.set()
            await asyncio.wait_for(first, 5)


async def test_acceptance_workflow_type_is_explicit_and_default_gateway_rejects_it(
    integration_engine,
):
    from sqlalchemy import update

    from apps.scheduler_worker.web_discovery import (
        WebDiscoveryToolComposition,
        build_web_discovery_tools,
    )
    from infra.db.tables import WorkflowRunRow
    from tool_gateway.errors import ToolGatewayError
    from tool_gateway.fingerprint import HmacFingerprintProvider

    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        tenant = TenantId(new_id("tn"))
        run = await _workflow_run(factory, tenant)
        async with factory() as session, session.begin():
            await session.execute(
                update(WorkflowRunRow)
                .where(
                    WorkflowRunRow.tenant_id == tenant,
                    WorkflowRunRow.run_id == run,
                )
                .values(workflow_type="research_source_acceptance")
            )
        transport = SearchTransport(limit=10)
        kwargs = {
            "factory": factory,
            "tenant_id": tenant,
            "tool_user": UserId(new_id("usr")),
            "fingerprints": HmacFingerprintProvider("v1", b"a" * 32),
            "composition": WebDiscoveryToolComposition(
                Playbook(),
                Secrets(),
                "TAVILY_API_KEY_REF",
                transport,
                Pages(),
                Artifacts(),
                provider="tavily",
                exclusive_account_confirmed=True,
            ),
            "country_policy": CountryPolicy(),
            "lease_duration": timedelta(seconds=30),
            "now": lambda: NOW,
        }
        with pytest.raises(ToolGatewayError):
            await build_web_discovery_tools(**kwargs).searcher.search(
                tenant, run, "factory", "US", "hinges", 1
            )
        assert transport.calls == transport.usage_calls == 0
        try:
            accepted = build_web_discovery_tools(
                **kwargs, workflow_type="research_source_acceptance"
            )
        except TypeError:
            pytest.fail("RED: Gateway composition 缺显式验收 workflow 绑定")
        batch = await accepted.searcher.search(
            tenant, run, "factory", "US", "hinges", 1
        )
        assert batch.results[0].title == "Acme"
        accepted.searcher.release(batch)
        assert transport.calls == 1
        for terminal_status in ("cancelled", "failed", "completed"):
            async with factory() as session, session.begin():
                await session.execute(
                    update(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == tenant,
                        WorkflowRunRow.run_id == run,
                    )
                    .values(status=terminal_status)
                )
            with pytest.raises(ToolGatewayError):
                await accepted.searcher.search(
                    tenant, run, "second factory", "US", "hinges", 1
                )
        assert transport.calls == 1
        with pytest.raises(ValidationError):
            await accepted.searcher.search(
                TenantId(new_id("tn")), run, "factory", "US", "hinges", 1
            )
        with pytest.raises(ValueError):
            build_web_discovery_tools(**kwargs, workflow_type="outreach_campaign")
        await transaction.rollback()


@pytest.mark.parametrize("fail_result_read", [False, True])
async def test_source_acceptance_uses_gateway_snapshots_and_cannot_be_polled_by_normal_scheduler(
    integration_engine,
    fail_result_read,
):
    import importlib
    from dataclasses import replace

    from sqlalchemy import event, select

    from apps.scheduler_worker.web_discovery import WebDiscoveryToolComposition
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.web_search.transport import PublicPageResponse
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.tables import RawArtifactRow, ToolCallRow, WorkflowRunRow
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from tests.integration.test_phase1_closed_loop import _MemoryBlobTransport
    from tests.unit.workflows.test_research_discovery import research_plan
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from workflows.demand_discovery.flow import build_demand_discovery_definition

    try:
        module = importlib.import_module("apps.scheduler_worker.research_acceptance")
    except ModuleNotFoundError:
        pytest.fail("RED: 缺少真实来源验收装配")
    async with _source_database(integration_engine) as (factory, tenant):
        actor = UserId(new_id("usr"))

        class Reader:
            async def load_confirmed(self, requested, proposal, user):
                assert (
                    requested == tenant
                    and proposal == "proposal:test"
                    and user == actor
                )
                return replace(research_plan(), max_pages_read=3)

        class PageTransport(Pages):
            async def fetch(self, url):
                return PublicPageResponse(
                    url,
                    b"<html><body>We are Acme Tools, an importer. We are based in US.</body></html>",
                )

        transport = SearchTransport(limit=100)
        artifacts = RawArtifactStoreImpl(
            lambda bound: SqlAlchemyArtifactUnitOfWork(factory, bound),
            _MemoryBlobTransport(),
            100000,
            lambda: NOW,
            new_id,
        )
        composition = WebDiscoveryToolComposition(
            Playbook(),
            Secrets(),
            "TAVILY_API_KEY_REF",
            transport,
            PageTransport(),
            artifacts,
            provider="tavily",
            exclusive_account_confirmed=True,
        )
        fingerprints = HmacFingerprintProvider("v1", b"a" * 32)
        observed_runs = []

        async def execute_acceptance():
            return await module.run_source_acceptance(
                factory=factory,
                tenant_id=tenant,
                actor_id=actor,
                proposal_id="proposal:test",
                reader=Reader(),
                composition=composition,
                country_policy=CountryPolicy(),
                fingerprints=fingerprints,
                lease_duration=timedelta(seconds=30),
                now=lambda: NOW,
                on_run_started=observed_runs.append,
            )

        def result_read_fault(connection, cursor, statement, parameters, context, many):
            if "GROUP BY search_quota_reservations.status" in statement:
                raise RuntimeError("controlled result read failure")

        if fail_result_read:
            event.listen(
                integration_engine.sync_engine,
                "before_cursor_execute",
                result_read_fault,
            )
            try:
                with pytest.raises(
                    RuntimeError, match="controlled result read failure"
                ):
                    await execute_acceptance()
            finally:
                event.remove(
                    integration_engine.sync_engine,
                    "before_cursor_execute",
                    result_read_fault,
                )
            assert transport.calls == 3
            assert len(observed_runs) == 1
            async with factory() as session:
                status = (
                    await session.execute(
                        select(WorkflowRunRow.status).where(
                            WorkflowRunRow.tenant_id == tenant,
                            WorkflowRunRow.run_id == observed_runs[0],
                        )
                    )
                ).scalar_one()
            assert status == "completed", "结果读取失败不能否认已执行的真实Run"
        report = await execute_acceptance()
        assert report["run_id"] == observed_runs[0]
        assert report["status"] == "completed", report["reason"]
        assert report["scope"] == "pages_only"
        assert report["model"] == report["outreach"] == "not_run"
        assert report["consumed_credits"] == 3
        assert report["reserved_credits"] == report["uncertain_credits"] == 0
        from infra.db.run_audit import PostgresRunAuditRepository

        audit_runs = await PostgresRunAuditRepository(factory).list_runs(
            tenant, workflow_type=None, status=None, limit=10
        )
        assert audit_runs[0].research is None, "pages-only验收不能污染业务研究摘要"
        assert {item["discovery_lane"] for item in report["pages"]} == {
            "importer",
            "distributor",
            "ecommerce",
        }
        assert all(
            item["page_hash"] and item["snapshot_artifact_ref"]
            for item in report["pages"]
        )
        again = await module.run_source_acceptance(
            factory=factory,
            tenant_id=tenant,
            actor_id=actor,
            proposal_id="proposal:test",
            reader=Reader(),
            composition=composition,
            country_policy=CountryPolicy(),
            fingerprints=fingerprints,
            lease_duration=timedelta(seconds=30),
            now=lambda: NOW,
        )
        assert again["run_id"] == report["run_id"]
        assert transport.calls == 3
        async with factory() as session:
            tools = (
                (
                    await session.execute(
                        select(ToolCallRow).where(
                            ToolCallRow.tenant_id == tenant
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert {tool.tool_id for tool in tools} == {"web.search", "web.read_page"}
            assert {tool.user_id for tool in tools} == {actor}
            assert (
                len(
                    (
                        await session.execute(
                            select(RawArtifactRow).where(
                                RawArtifactRow.tenant_id == tenant
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                == 1
            )

        class MustNotRun:
            async def execute(self, run):
                raise AssertionError("不能领取此流程")

        definition = build_demand_discovery_definition()
        normal = PostgresWorkflowEngine(
            factory,
            {step.handler_ref: MustNotRun() for step in definition.steps},
            now=lambda: NOW,
        )
        normal.register(definition)
        # 新验收Run待处理时普通scheduler也不能领取；不是只检查终态。
        acceptance = PostgresWorkflowEngine(
            factory,
            {"acceptance.plan": MustNotRun(), "acceptance.pages": MustNotRun()},
            now=lambda: NOW,
        )
        acceptance.register(module.acceptance_definition())
        pending = await acceptance.start(
            tenant, "research_source_acceptance", "another", {}, "another"
        )
        assert await normal.poll_due(tenant, 10) == 0
        async with factory() as session:
            status = (
                await session.execute(
                    select(WorkflowRunRow.status).where(
                        WorkflowRunRow.tenant_id == tenant,
                        WorkflowRunRow.run_id == pending,
                    )
                )
            ).scalar_one()
        assert status == "running"


@pytest.mark.parametrize("capability", ["search", "page"])
async def test_concurrent_last_run_budget_never_dispatches_twice(
    integration_engine, capability
):
    """两个真实Gateway请求竞争同Run最后预算，未决received也计入保守上限。"""
    import asyncio

    from sqlalchemy import delete

    from apps.scheduler_worker.web_discovery import (
        WebDiscoveryToolComposition,
        build_web_discovery_tools,
    )
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.web_search.transport import PublicPageResponse
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.tables import (
        SearchQuotaAccountRow,
        SearchQuotaReservationRow,
        SearchQuotaRunRow,
    )
    from tests.integration.test_phase1_closed_loop import _MemoryBlobTransport
    from tool_gateway.errors import ToolGatewayError
    from tool_gateway.fingerprint import HmacFingerprintProvider

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))

    class PageTransport(Pages):
        calls = 0

        async def fetch(self, url):
            self.calls += 1
            return PublicPageResponse(
                url, b"<html><body>Public company description.</body></html>"
            )

    page_transport, search = PageTransport(), SearchTransport(limit=100)
    artifacts = RawArtifactStoreImpl(
        lambda bound: SqlAlchemyArtifactUnitOfWork(factory, bound),
        _MemoryBlobTransport(),
        100000,
        lambda: NOW,
        new_id,
    )
    tools = build_web_discovery_tools(
        factory=factory,
        tenant_id=tenant,
        tool_user=UserId(new_id("usr")),
        fingerprints=HmacFingerprintProvider("v1", b"a" * 32),
        composition=WebDiscoveryToolComposition(
            Playbook(),
            Secrets(),
            "TAVILY_API_KEY_REF",
            search,
            page_transport,
            artifacts,
            provider="tavily",
            exclusive_account_confirmed=True,
        ),
        country_policy=CountryPolicy(),
        lease_duration=timedelta(seconds=30),
        now=lambda: NOW,
    )
    run_id = await _workflow_run(factory, tenant, budget=1)
    try:
        if capability == "search":
            outcomes = await asyncio.gather(
                *(
                    tools.searcher.search(tenant, run_id, query, "US", "hinges", 1)
                    for query in ("first factory", "second factory")
                ),
                return_exceptions=True,
            )
            dispatched = search.calls
        else:
            batch = await tools.searcher.search(
                tenant, run_id, "factory", "US", "hinges", 1
            )
            outcomes = await asyncio.gather(
                *(
                    tools.page_reader.read_page(tenant, run_id, batch, 0)
                    for _ in range(2)
                ),
                return_exceptions=True,
            )
            dispatched = page_transport.calls
        assert dispatched <= 1
        assert sum(isinstance(result, ToolGatewayError) for result in outcomes) >= 1
        assert all(
            not isinstance(result, Exception) or isinstance(result, ToolGatewayError)
            for result in outcomes
        )
    finally:
        tools.searcher.discard_all()
        async with factory() as session, session.begin():
            for table in (
                SearchQuotaRunRow,
                SearchQuotaReservationRow,
                SearchQuotaAccountRow,
            ):
                await session.execute(delete(table).where(table.tenant_id == tenant))


async def test_engine_run_lock_does_not_deadlock_gateway_budget_check(
    integration_engine,
):
    """真实独立连接，抓住engine持Run行锁→Gateway另连接同锁的自死锁。"""
    import asyncio

    from sqlalchemy import delete, select

    from apps.scheduler_worker.web_discovery import (
        WebDiscoveryToolComposition,
        build_web_discovery_tools,
    )
    from infra.db.tables import (
        SearchQuotaAccountRow,
        SearchQuotaReservationRow,
        SearchQuotaRunRow,
        WorkflowRunRow,
    )
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from workflows.engine.runner import StepDefinition, WorkflowDefinition

    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    transport = SearchTransport(limit=10)
    tools = build_web_discovery_tools(
        factory=factory,
        tenant_id=tenant,
        tool_user=UserId(new_id("usr")),
        fingerprints=HmacFingerprintProvider("v1", b"a" * 32),
        composition=WebDiscoveryToolComposition(
            Playbook(),
            Secrets(),
            "TAVILY_API_KEY_REF",
            transport,
            Pages(),
            Artifacts(),
            provider="tavily",
            exclusive_account_confirmed=True,
        ),
        country_policy=CountryPolicy(),
        lease_duration=timedelta(seconds=30),
        now=lambda: NOW,
    )

    class SearchStep:
        async def execute(self, run):
            batch = await tools.searcher.search(
                tenant, run.run_id, "factory", "US", "hinges", 1
            )
            tools.searcher.release(batch)
            return "complete", None, {}

    engine = PostgresWorkflowEngine(factory, {"search": SearchStep()}, now=lambda: NOW)
    engine.register(
        WorkflowDefinition(
            "demand_discovery",
            1,
            (StepDefinition("execute_search", "search", max_retries=0),),
        )
    )
    run_id = await engine.start(
        tenant,
        "demand_discovery",
        "test",
        {"query_budget": 1, "page_budget": 1},
        "test",
    )
    try:
        try:
            await asyncio.wait_for(engine.poll_due(tenant, 1), timeout=3)
        except TimeoutError:
            pytest.fail("RED: engine Run 行锁与Gateway预算行锁互等，Provider零调用")
        async with factory() as session:
            status = (
                await session.execute(
                    select(WorkflowRunRow.status).where(
                        WorkflowRunRow.tenant_id == tenant,
                        WorkflowRunRow.run_id == run_id,
                    )
                )
            ).scalar_one()
        assert status == "completed"
        assert transport.calls == 1
    finally:
        async with factory() as session, session.begin():
            for table in (
                SearchQuotaRunRow,
                SearchQuotaReservationRow,
                SearchQuotaAccountRow,
            ):
                await session.execute(delete(table).where(table.tenant_id == tenant))


async def test_acceptance_reader_requires_actual_boss_active_confirmed_research(
    integration_engine,
):
    import importlib
    from dataclasses import asdict

    from domains.directives.schemas import (
        DemandDiscoveryPlanInput,
        DiscoverySearchQueryInput,
    )
    from infra.db.tables import EmployeeRow
    from shared.schemas.identifiers import EmployeeId
    from tests.unit.workflows.test_research_discovery import research_plan

    try:
        module = importlib.import_module(
            "apps.scheduler_worker.research_acceptance_dependencies"
        )
    except ModuleNotFoundError:
        pytest.fail("RED: 来源验收缺真实老板与当前确认提案读取器")
    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        tenant, boss = TenantId(new_id("tn")), EmployeeId(new_id("emp"))
        user = UserId(new_id("usr"))
        async with factory() as session, session.begin():
            session.add(
                EmployeeRow(
                    tenant_id=tenant,
                    employee_id=boss,
                    user_id=user,
                    name="Test Boss",
                    role="boss",
                    is_active=True,
                    created_at=NOW,
                    languages=[],
                    timezone="UTC",
                )
            )
        reader, _playbook, _policy = module.build_acceptance_readers(factory, tenant)
        assert await reader.user_for_employee(tenant, boss) == user
        plan = research_plan()
        dto = DemandDiscoveryPlanInput(
            **{
                **asdict(plan),
                "queries": tuple(
                    DiscoverySearchQueryInput(**asdict(q)) for q in plan.queries
                ),
            }
        )
        proposal = await reader.directives.submit_discovery_proposal(
            tenant, "test", dto, "test", ["只研究"], "controlled"
        )
        with pytest.raises(ValidationError):
            await reader.load_confirmed(tenant, proposal, user)
        await reader.directives.confirm_proposal(tenant, proposal, boss)
        assert await reader.load_confirmed(tenant, proposal, user) == plan
        with pytest.raises(PermissionDenied):
            await reader.load_confirmed(TenantId(new_id("tn")), proposal, user)
        with pytest.raises(PermissionDenied):
            await reader.load_confirmed(tenant, proposal, UserId(new_id("emp")))
        second = await reader.directives.submit_discovery_proposal(
            tenant, "new", dto, "new", ["只研究"], "controlled"
        )
        await reader.directives.confirm_proposal(tenant, second, boss)
        with pytest.raises(ValidationError):
            await reader.load_confirmed(tenant, proposal, user)
        await transaction.rollback()
