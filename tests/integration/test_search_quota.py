"""真实 Postgres 免费账户额度、跨 Run 并发和崩溃后禁止重发。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from connectors.search_contracts import SearchCostStatus, SearchUsage
from connectors.tavily.client import TavilySearchConnector
from connectors.tavily.transport import TavilyHttpResponse, TavilyRateLimitedError
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId, UserId, new_id
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _modules() -> tuple[Any, Any]:
    try:
        return (
            importlib.import_module("infra.db.search_quota"),
            importlib.import_module("tool_gateway.handlers.free_search"),
        )
    except ModuleNotFoundError:
        pytest.fail("RED：免费账户持久预留及 Gateway reader 尚未实现")


class SearchTransport:
    def __init__(self, *, limit: int = 1, error: BaseException | None = None) -> None:
        self.calls = 0
        self.usage_calls = 0
        self.limit = limit
        self.error = error
        self.usage_error: Exception | None = None
        self.account: dict[str, object] = {
            "current_plan": "Researcher",
            "plan_limit": limit,
            "plan_usage": 0,
            "paygo_usage": 0,
            "paygo_limit": 0,
        }

    async def usage(self, *, api_key: str) -> TavilyHttpResponse:
        self.usage_calls += 1
        if self.usage_error:
            raise self.usage_error
        return TavilyHttpResponse(200, {"account": self.account})

    async def search(
        self, query: str, country: str, limit: int, *, api_key: str
    ) -> TavilyHttpResponse:
        self.calls += 1
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return TavilyHttpResponse(
            200,
            {
                "results": [
                    {
                        "title": "Acme",
                        "url": "https://example.com/",
                        "content": "factory",
                    }
                ]
            },
        )


class Pages:
    async def validate_url(self, url: str) -> str:
        return url

    async def fetch(self, url: str) -> None:
        raise AssertionError("本测试不得读取页面")


class Secrets:
    def resolve(self, ref: str) -> str:
        assert ref == "TAVILY_API_KEY_REF"
        return "synthetic-test-key-only"


@pytest_asyncio.fixture
async def quota(
    integration_engine: AsyncEngine,
) -> AsyncIterator[tuple[Any, Any, TenantId]]:
    db, _ = _modules()
    tenant = TenantId(new_id("tn"))
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    repository = db.PostgresSearchQuotaRepository(factory, tenant, now=lambda: NOW)
    try:
        yield repository, factory, tenant
    finally:
        async with integration_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM search_quota_runs WHERE tenant_id = :tenant"),
                {"tenant": str(tenant)},
            )
            await connection.execute(
                text("DELETE FROM search_quota_reservations WHERE tenant_id = :tenant"),
                {"tenant": str(tenant)},
            )
            await connection.execute(
                text("DELETE FROM search_quota_accounts WHERE tenant_id = :tenant"),
                {"tenant": str(tenant)},
            )


def _reader(quota: Any, tenant: TenantId, run: RunId, transport: SearchTransport):
    _, handlers = _modules()
    factory = handlers.FreeSearchReaderFactory(
        tenant_id=tenant,
        quota=quota,
        connector_factory=lambda: TavilySearchConnector(transport, Pages()),
        secret_resolver=Secrets(),
    )
    return factory.for_run(tenant, run, "a" * 64, fingerprint_version="v1")


async def test_two_runs_compete_for_last_credit_only_one_dispatch(quota) -> None:
    repository, _, tenant = quota
    transport = SearchTransport()
    readers = [
        _reader(repository, tenant, RunId(new_id("run")), transport) for _ in range(2)
    ]
    results = await asyncio.gather(
        *[reader.search(tenant, "factory", "US", 1) for reader in readers],
        return_exceptions=True,
    )
    assert transport.calls == 1
    assert sum(isinstance(result, tuple) for result in results) == 1
    state = await repository.snapshot()
    assert state.remaining == 0
    assert state.reservations == 1
    denied_run = next(
        reader.run_id
        for reader, result in zip(readers, results)
        if isinstance(result, Exception)
    )
    denied_state = await repository.run_state(denied_run)
    assert denied_state is not None, [
        (type(item).__name__, getattr(item, "category", None)) for item in results
    ]
    assert denied_state.stop_reason == "quota_exhausted"


@pytest.mark.parametrize(
    "failure", [TimeoutError(), TavilyRateLimitedError(1), asyncio.CancelledError()]
)
async def test_dispatched_failure_remains_uncertain_and_same_run_never_retries(
    quota, failure
) -> None:
    repository, factory, tenant = quota
    transport = SearchTransport(limit=10, error=failure)
    run = RunId(new_id("run"))
    with pytest.raises((ToolGatewayError, asyncio.CancelledError)):
        await _reader(repository, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    db, _ = _modules()
    rebuilt = db.PostgresSearchQuotaRepository(
        factory, tenant, now=lambda: NOW + timedelta(days=40)
    )
    reservation = await rebuilt.get(run, "a" * 64)
    assert reservation.status == "uncertain"
    assert (await rebuilt.run_state(run)).stop_reason == "request_uncertain"
    with pytest.raises(ToolGatewayError) as rejected:
        await _reader(rebuilt, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    assert rejected.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert transport.calls == 1
    assert (await rebuilt.snapshot()).reservations == 1


async def test_crash_before_dispatch_keeps_reserved_and_blocks_replay(quota) -> None:
    repository, factory, tenant = quota
    run = RunId(new_id("run"))
    await repository.reserve(
        run, "a" * 64, SearchUsage("Researcher", 10, 0, False, SearchCostStatus.FREE),
        fingerprint_version="v1",
    )
    db, _ = _modules()
    rebuilt = db.PostgresSearchQuotaRepository(factory, tenant, now=lambda: NOW)
    assert (await rebuilt.get(run, "a" * 64)).status == "reserved"
    assert (await rebuilt.run_state(run)).stop_reason == "request_uncertain"
    transport = SearchTransport(limit=10)
    with pytest.raises(ToolGatewayError):
        await _reader(rebuilt, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    assert transport.calls == 0
    assert transport.usage_calls == 0


@pytest.mark.parametrize("case", ["usage_failure", "paygo", "unknown", "missing_paygo"])
async def test_unverified_free_usage_cannot_dispatch(quota, case: str) -> None:
    repository, _, tenant = quota
    transport = SearchTransport()
    if case == "usage_failure":
        transport.usage_error = RuntimeError("sensitive-provider-detail")
    elif case == "paygo":
        transport.account["paygo_limit"] = 100
    elif case == "unknown":
        transport.account["current_plan"] = "Unknown"
    else:
        del transport.account["paygo_limit"]
    run = RunId(new_id("run"))
    with pytest.raises(ToolGatewayError) as rejected:
        await _reader(repository, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    assert "sensitive" not in str(rejected.value)
    assert transport.calls == 0
    assert (await repository.snapshot()).reservations == 0
    assert (await repository.run_state(run)).stop_reason == (
        "paid_enabled" if case == "paygo" else "usage_unknown"
    )


async def test_same_account_different_binding_cannot_reset_and_other_tenant_cannot_read(
    quota,
) -> None:
    repository, factory, tenant = quota
    transport = SearchTransport()
    await _reader(repository, tenant, RunId(new_id("run")), transport).search(
        tenant, "factory", "US", 1
    )
    db, _ = _modules()
    second_binding = db.PostgresSearchQuotaRepository(factory, tenant, now=lambda: NOW)
    with pytest.raises(ToolGatewayError):
        await _reader(second_binding, tenant, RunId(new_id("run")), transport).search(
            tenant, "factory", "US", 1
        )
    other_tenant = TenantId(new_id("tn"))
    other = db.PostgresSearchQuotaRepository(factory, other_tenant, now=lambda: NOW)
    assert await other.snapshot() is None
    with pytest.raises(ToolGatewayError):
        await _reader(other, other_tenant, RunId(new_id("run")), transport).search(
            other_tenant, "factory", "US", 1
        )
    assert transport.calls == 1
    assert await other.snapshot() is None


async def test_success_is_consumed_and_stale_or_new_month_snapshot_never_replenishes(
    quota,
) -> None:
    repository, _, tenant = quota
    run = RunId(new_id("run"))
    transport = SearchTransport(limit=3)
    await _reader(repository, tenant, run, transport).search(tenant, "factory", "US", 1)
    assert (await repository.get(run, "a" * 64)).status == "consumed"
    transport.account["plan_usage"] = 2
    with pytest.raises(ToolGatewayError):
        await _reader(repository, tenant, RunId(new_id("run")), transport).search(
            tenant, "factory", "US", 1
        )
    transport.account["plan_usage"] = 0
    with pytest.raises(ToolGatewayError):
        await _reader(repository, tenant, RunId(new_id("run")), transport).search(
            tenant, "factory", "US", 1
        )
    assert transport.calls == 1
    assert (await repository.snapshot()).remaining == 0


class Artifacts:
    async def put(self, *args, **kwargs):
        raise AssertionError("搜索不写页面证据")

    async def get(self, *args, **kwargs):
        raise AssertionError("搜索不读页面证据")

    async def get_meta(self, *args, **kwargs):
        raise AssertionError("搜索不读页面证据")


class Playbook:
    def __init__(self, allowed=True):
        self.allowed = allowed

    async def allows_research(self, tenant_id, category, country):
        return self.allowed


class CountryPolicy:
    def __init__(self, allowed=True):
        self.allowed = allowed

    async def decision(self, tenant_id, country, action):
        from domains.compliance.schemas import CountryPolicyDecision

        return CountryPolicyDecision(
            country_key=country,
            action=action,
            configured=True,
            allowed=self.allowed,
            active_version_id=new_id("cpp"),
            content_hash="a" * 64,
            requirements=(),
        )


async def _workflow_run(factory, tenant, *, budget=2):
    from infra.db.tables import WorkflowRunRow

    run = RunId(new_id("run"))
    async with factory() as session, session.begin():
        session.add(
            WorkflowRunRow(
                tenant_id=str(tenant),
                run_id=str(run),
                workflow_type="demand_discovery",
                workflow_version=1,
                subject_ref="task-test",
                current_step="execute_search",
                status="running",
                context={}
                if budget is None
                else {"query_budget": budget, "page_budget": 1},
                idempotency_key=str(run),
            )
        )
    return run


def _composition(
    factory, tenant, transport, *, playbook=True, country=True, confirmed=True,
    secret_ref="TAVILY_API_KEY_REF",
    fingerprints=None,
):
    from apps.scheduler_worker.web_discovery import (
        WebDiscoveryToolComposition,
        build_web_discovery_tools,
    )
    from tool_gateway.fingerprint import HmacFingerprintProvider

    try:
        config = WebDiscoveryToolComposition(
            Playbook(playbook),
            Secrets(),
            secret_ref,
            transport,
            Pages(),
            Artifacts(),
            provider="tavily",
            exclusive_account_confirmed=confirmed,
        )
    except TypeError:
        pytest.fail("RED：生产组合尚未提供显式 Tavily 免费选择")
    return build_web_discovery_tools(
        factory=factory,
        tenant_id=tenant,
        tool_user=UserId(new_id("usr")),
        fingerprints=fingerprints or HmacFingerprintProvider("v1", b"x" * 32),
        composition=config,
        country_policy=CountryPolicy(country),
        lease_duration=timedelta(seconds=30),
        now=lambda: NOW,
    )


async def test_tavily_composition_passes_gateway_and_returns_typed_quota_reason(quota):
    repository, factory, tenant = quota
    transport = SearchTransport()
    tools = _composition(factory, tenant, transport)
    assert transport.calls == transport.usage_calls == 0
    run = await _workflow_run(factory, tenant)
    batch = await tools.searcher.search(tenant, run, "factory", "US", "hinges", 1)
    assert batch.results[0].title == "Acme"
    tools.searcher.release(batch)
    second_run = await _workflow_run(factory, tenant)
    with pytest.raises(ToolGatewayError) as rejected:
        await tools.searcher.search(tenant, second_run, "factory", "US", "hinges", 1)
    assert rejected.value.reason == "quota_exhausted"
    assert rejected.value.is_retryable is False
    assert transport.calls == 1
    assert (await repository.snapshot()).remaining == 0
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT cost_class FROM tool_calls WHERE tenant_id = :tenant AND run_id = :run"
                    ),
                    {"tenant": tenant, "run": run},
                )
            )
            .scalars()
            .all()
        )
    assert rows == ["free"]


@pytest.mark.parametrize(
    "gate", ["playbook", "country", "budget", "tenant", "parameters"]
)
async def test_gateway_rejection_never_reads_usage_or_reserves_account(quota, gate):
    repository, factory, tenant = quota
    transport = SearchTransport()
    tools = _composition(
        factory,
        tenant,
        transport,
        playbook=gate != "playbook",
        country=gate != "country",
    )
    run = await _workflow_run(factory, tenant, budget=None if gate == "budget" else 2)
    if gate == "tenant":
        run = RunId(new_id("run"))
    with pytest.raises((ToolGatewayError, ValidationError)):
        await tools.searcher.search(
            tenant, run, "" if gate == "parameters" else "factory", "US", "hinges", 1
        )
    assert transport.calls == transport.usage_calls == 0
    assert await repository.snapshot() is None


async def test_existing_per_run_budget_still_limits_tavily_before_usage(quota):
    _, factory, tenant = quota
    transport = SearchTransport(limit=20)
    tools = _composition(factory, tenant, transport)
    run = await _workflow_run(factory, tenant, budget=1)
    batch = await tools.searcher.search(tenant, run, "factory", "US", "hinges", 1)
    tools.searcher.release(batch)
    with pytest.raises(ToolGatewayError):
        await tools.searcher.search(tenant, run, "different factory", "US", "hinges", 1)
    assert transport.calls == transport.usage_calls == 1


async def test_unconfirmed_deployment_binding_does_not_compose(quota):
    from shared.errors import ValidationError

    _, factory, tenant = quota
    with pytest.raises(ValidationError):
        _composition(factory, tenant, SearchTransport(), confirmed=False)


async def test_pending_run_blocks_different_query_but_other_run_can_use_remaining(
    quota,
):
    repository, _, tenant = quota
    _, handlers = _modules()
    transport = SearchTransport(limit=10, error=TimeoutError())
    run = RunId(new_id("run"))
    with pytest.raises(ToolGatewayError):
        await _reader(repository, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    other_query = handlers.FreeSearchReaderFactory(
        tenant,
        repository,
        lambda: TavilySearchConnector(transport, Pages()),
        Secrets(),
    ).for_run(tenant, run, "b" * 64, fingerprint_version="v1")
    with pytest.raises(ToolGatewayError):
        await other_query.search(tenant, "different query", "US", 1)
    transport.error = None
    await _reader(repository, tenant, RunId(new_id("run")), transport).search(
        tenant, "factory", "US", 1
    )
    assert transport.calls == 2
    assert (await repository.run_state(run)).stop_reason == "request_uncertain"


async def test_completed_operation_cannot_redispatch_after_result_is_lost(quota):
    repository, _, tenant = quota
    transport = SearchTransport(limit=10)
    run = RunId(new_id("run"))
    await _reader(repository, tenant, run, transport).search(tenant, "factory", "US", 1)
    with pytest.raises(ToolGatewayError):
        await _reader(repository, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    assert transport.calls == 1


async def test_account_reservation_commit_failure_never_dispatches(quota, monkeypatch):
    repository, factory, tenant = quota
    transport = SearchTransport(limit=10)
    run = RunId(new_id("run"))
    original = repository.reserve

    async def fail_after_reserved(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic-database-detail")

    monkeypatch.setattr(repository, "reserve", fail_after_reserved)
    with pytest.raises(ToolGatewayError) as failure:
        await _reader(repository, tenant, run, transport).search(
            tenant, "factory", "US", 1
        )
    assert "synthetic" not in str(failure.value)
    assert transport.calls == 0
    db, _ = _modules()
    rebuilt = db.PostgresSearchQuotaRepository(factory, tenant, now=lambda: NOW)
    assert (await rebuilt.run_state(run)).stop_reason == "request_uncertain"


async def test_0039_roundtrip_schema_matches_orm(db_url):
    from sqlalchemy import (
        CheckConstraint,
        ForeignKeyConstraint,
        UniqueConstraint,
        inspect,
    )

    from infra.db.session import create_engine_from
    from infra.db.tables import Base
    from tests.integration.test_migrations import _run_alembic

    tables = ("search_quota_accounts", "search_quota_reservations", "search_quota_runs")
    engine = create_engine_from(db_url)

    def contract(connection):
        inspector = inspect(connection)
        for name in tables:
            table = Base.metadata.tables[name]
            columns = inspector.get_columns(name)
            assert {item["name"] for item in columns} == set(table.columns.keys())
            assert {item["name"]: item["nullable"] for item in columns} == {
                item.name: item.nullable for item in table.columns
            }
            assert {item["name"] for item in inspector.get_check_constraints(name)} == {
                item.name
                for item in table.constraints
                if isinstance(item, CheckConstraint)
            }
            assert {
                item["name"] for item in inspector.get_unique_constraints(name)
            } == {
                item.name
                for item in table.constraints
                if isinstance(item, UniqueConstraint)
            }
            assert {item["name"] for item in inspector.get_foreign_keys(name)} == {
                item.name
                for item in table.constraints
                if isinstance(item, ForeignKeyConstraint)
            }

    try:
        async with engine.connect() as conn:
            await conn.run_sync(contract)
        _run_alembic(db_url, "downgrade", "0038")
        async with engine.connect() as conn:
            names = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
            assert not set(tables) & set(names)
        _run_alembic(db_url, "upgrade", "head")
        async with engine.connect() as conn:
            await conn.run_sync(contract)
            assert (
                await conn.scalar(text("SELECT version_num FROM alembic_version"))
                == "0039"
            )
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_legacy_brave_composition_retains_provider_and_does_not_claim_free_quota(quota):
    from apps.scheduler_worker.web_discovery import (
        WebDiscoveryToolComposition,
        build_web_discovery_tools,
    )
    from connectors.web_search.transport import SearchHttpResponse
    from tool_gateway.fingerprint import HmacFingerprintProvider

    repository, factory, tenant = quota

    class BraveTransport:
        calls = 0

        async def search(self, query, country, count, *, api_key):
            self.calls += 1
            return SearchHttpResponse({"web": {"results": [
                {"title": "Brave result", "url": "https://example.com/", "description": "factory"}
            ]}})

    class BraveSecrets:
        def resolve(self, ref):
            assert ref == "WEB_SEARCH_API_KEY_REF"
            return "synthetic-brave-test-key"

    transport = BraveTransport()
    tools = build_web_discovery_tools(
        factory=factory, tenant_id=tenant, tool_user=UserId(new_id("usr")),
        fingerprints=HmacFingerprintProvider("v1", b"x" * 32),
        composition=WebDiscoveryToolComposition(
            Playbook(), BraveSecrets(), "WEB_SEARCH_API_KEY_REF", transport, Pages(), Artifacts()
        ),
        country_policy=CountryPolicy(), lease_duration=timedelta(seconds=30), now=lambda: NOW,
    )
    run = await _workflow_run(factory, tenant)
    batch = await tools.searcher.search(tenant, run, "factory", "US", "hinges", 1)
    assert batch.results[0].title == "Brave result"
    tools.searcher.release(batch)
    assert transport.calls == 1
    assert await repository.snapshot() is None


async def test_rotating_deployment_key_reference_does_not_create_new_free_account(quota):
    _, factory, tenant = quota
    transport = SearchTransport()
    first = _composition(factory, tenant, transport)
    run = await _workflow_run(factory, tenant)
    batch = await first.searcher.search(tenant, run, "factory", "US", "hinges", 1)
    first.searcher.release(batch)
    second = _composition(factory, tenant, transport, secret_ref="ROTATED_DEPLOYMENT_KEY_REF")
    with pytest.raises(ToolGatewayError) as failure:
        await second.searcher.search(
            tenant, await _workflow_run(factory, tenant), "factory", "US", "hinges", 1
        )
    assert failure.value.reason == "quota_exhausted"
    assert transport.calls == 1


@pytest.mark.parametrize("recovered_query", ["factory", "different query"])
async def test_consumed_before_delivery_crash_cannot_redispatch_after_hmac_rotation(
    quota, monkeypatch, recovered_query
):
    from infra.db.search_quota import PostgresSearchQuotaRepository
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.free_search_contracts import FreeSearchError

    repository, factory, tenant = quota
    transport = SearchTransport(limit=20)
    run = await _workflow_run(factory, tenant, budget=4)
    first = _composition(factory, tenant, transport)
    original_consume = PostgresSearchQuotaRepository.consume

    async def crash_after_consumed(self, run_id, request_key):
        await original_consume(self, run_id, request_key)
        raise asyncio.CancelledError()

    with monkeypatch.context() as crash:
        crash.setattr(PostgresSearchQuotaRepository, "consume", crash_after_consumed)
        with pytest.raises(asyncio.CancelledError):
            await first.searcher.search(tenant, run, "factory", "US", "hinges", 1)
    async with factory() as session:
        statuses = (await session.execute(text(
            "SELECT status FROM search_quota_reservations WHERE tenant_id=:tenant AND run_id=:run"
        ), {"tenant": tenant, "run": run})).scalars().all()
    assert statuses == ["consumed"]
    assert transport.calls == 1

    restarted = _composition(
        factory, tenant, transport,
        fingerprints=HmacFingerprintProvider("v2", b"y" * 32),
    )
    with pytest.raises(FreeSearchError) as failure:
        await restarted.searcher.search(tenant, run, recovered_query, "US", "hinges", 1)
    assert failure.value.reason == "request_uncertain"
    assert failure.value.is_retryable is False
    assert transport.calls == transport.usage_calls == 1
    assert (await repository.run_state(run)).stop_reason == "request_uncertain"
    async with factory() as session:
        stored_version = await session.scalar(text(
            "SELECT fingerprint_version FROM search_quota_runs WHERE tenant_id=:tenant AND run_id=:run"
        ), {"tenant": tenant, "run": run})
    assert stored_version == "v1"
    async with factory() as session:
        ledger = (await session.execute(text(
            "SELECT status,error_category FROM tool_calls WHERE tenant_id=:tenant AND run_id=:run "
            "AND fingerprint_version='v2'"
        ), {"tenant": tenant, "run": run})).all()
    assert ledger == [("failed_transient", "reconciliation_required")]
    with pytest.raises(FreeSearchError) as repeated:
        await restarted.searcher.search(tenant, run, recovered_query, "US", "hinges", 1)
    assert repeated.value.reason == "request_uncertain"
    assert repeated.value.is_retryable is False
    assert transport.calls == transport.usage_calls == 1
    new_run = await _workflow_run(factory, tenant)
    batch = await restarted.searcher.search(tenant, new_run, "factory", "US", "hinges", 1)
    restarted.searcher.release(batch)
    assert transport.calls == transport.usage_calls == 2


async def test_legacy_null_fingerprint_binding_is_not_claimed_by_restarted_worker(quota):
    repository, factory, tenant = quota
    run = await _workflow_run(factory, tenant)
    async with factory() as session, session.begin():
        await session.execute(text(
            "INSERT INTO search_quota_runs (tenant_id,run_id,fingerprint_version,updated_at) "
            "VALUES (:tenant,:run,NULL,:now)"
        ), {"tenant": tenant, "run": run, "now": NOW})
    transport = SearchTransport(limit=20)
    with pytest.raises(ToolGatewayError) as failure:
        await _composition(factory, tenant, transport).searcher.search(
            tenant, run, "factory", "US", "hinges", 1
        )
    assert failure.value.reason == "request_uncertain"
    assert transport.calls == transport.usage_calls == 0
    assert (await repository.snapshot()).reservations == 0
    async with factory() as session:
        assert await session.scalar(text(
            "SELECT fingerprint_version FROM search_quota_runs WHERE tenant_id=:tenant AND run_id=:run"
        ), {"tenant": tenant, "run": run}) is None


async def test_reserve_rechecks_version_under_account_lock_and_never_overwrites_binding(quota):
    repository, _, _ = quota
    run = RunId(new_id("run"))
    await repository.check_available(run, "a" * 64, fingerprint_version="v1")
    with pytest.raises(ToolGatewayError) as failure:
        await repository.reserve(
            run, "b" * 64, SearchUsage("Researcher", 20, 0, False, SearchCostStatus.FREE),
            fingerprint_version="v2",
        )
    assert failure.value.reason == "request_uncertain"
    assert (await repository.snapshot()).reservations == 0
    assert (await repository.run_state(run)).stop_reason == "request_uncertain"
    # 首次版本保持 v1；合法的同版本调用仍可预留，错版本没有窃取绑定。
    await repository.reserve(
        run, "a" * 64, SearchUsage("Researcher", 20, 0, False, SearchCostStatus.FREE),
        fingerprint_version="v1",
    )
    assert (await repository.snapshot()).reservations == 1
