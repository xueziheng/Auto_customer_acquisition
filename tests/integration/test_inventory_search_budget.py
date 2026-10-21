"""任务级预算跨来源计数，重建仓储保持未知围栏与原连接。"""

from datetime import timedelta
from decimal import Decimal

import pytest

from domains.prospecting.inventory import InventoryQuery
from shared.schemas.identifiers import EmployeeId
from shared.schemas.search_backend import InventorySearchPolicy, SearchBackendPin
from tests.integration.test_prospect_inventory import NOW
from tests.integration.test_prospect_inventory import inventory as inventory_fixture

inventory = inventory_fixture


async def test_durable_search_pin_unknown_and_job_cap(inventory):
    from infra.db.inventory_search_budget import InventorySearchBudget

    service, _catalog, factory, tenant, _ = inventory
    await service.request(
        tenant,
        EmployeeId("emp"),
        InventoryQuery(product_tags=("steel",)),
        1,
        "search-budget",
    )
    job = await service.claim_job(tenant, lease_duration=timedelta(minutes=10))
    assert job is not None
    pin = SearchBackendPin(
        backend_id="deepseek", connection_version="cv1", policy_version="p1"
    )
    policy = InventorySearchPolicy(
        version="p1",
        paid_search_enabled=True,
        maximum_requests=2,
        maximum_cost_cny=Decimal("0.010"),
    )
    budget = InventorySearchBudget(
        factory, tenant, job.id, job.lease_token, pin, policy, now=lambda: NOW
    )
    await budget.bind()
    await service.initialize_source_plan(
        tenant,
        job.id,
        job.lease_token,
        ("company_websites", "trade_associations", "dealer_networks"),
    )
    for source in ("company_websites", "trade_associations", "dealer_networks"):
        await service.reserve_source(tenant, job.id, job.lease_token, source)
    await budget.reserve("company_websites", "a" * 64)
    restarted = InventorySearchBudget(
        factory, tenant, job.id, job.lease_token, pin, policy, now=lambda: NOW
    )
    with pytest.raises(ValueError):
        await restarted.reserve("company_websites", "b" * 64)
    await budget.finish("a" * 64)
    await restarted.reserve("trade_associations", "b" * 64)
    await restarted.finish("b" * 64)
    with pytest.raises(ValueError):
        await restarted.reserve("dealer_networks", "c" * 64)
    changed = InventorySearchBudget(
        factory,
        tenant,
        job.id,
        job.lease_token,
        pin.model_copy(update={"connection_version": "cv2"}),
        policy,
        now=lambda: NOW,
    )
    with pytest.raises(ValueError):
        await changed.bind()


async def test_trusted_job_cap_is_persistently_monotone(inventory):
    from infra.db.inventory_search_budget import InventorySearchBudget

    service, _, factory, tenant, _ = inventory
    await service.request(
        tenant,
        EmployeeId("emp"),
        InventoryQuery(product_tags=("steel",)),
        1,
        "search-cap",
    )
    job = await service.claim_job(tenant, lease_duration=timedelta(minutes=10))
    pin = SearchBackendPin(
        backend_id="deepseek", connection_version="cv1", policy_version="p1"
    )
    policy = InventorySearchPolicy(
        version="p1",
        paid_search_enabled=True,
        maximum_requests=10,
        maximum_cost_cny="0.05",
    )

    def budget(cap=None):
        return InventorySearchBudget(
            factory,
            tenant,
            job.id,
            job.lease_token,
            pin,
            policy,
            now=lambda: NOW,
            trusted_maximum_requests=cap,
        )

    await budget(1).bind()
    await service.initialize_source_plan(
        tenant, job.id, job.lease_token, ("company_websites", "chambers")
    )
    for source in ("company_websites", "chambers"):
        await service.reserve_source(tenant, job.id, job.lease_token, source)
    await budget().reserve("company_websites", "a" * 64)
    await budget().finish("a" * 64)
    await budget(10).bind()
    for cap in (None, 10, 0):
        with pytest.raises(ValueError):
            await budget(cap).reserve("chambers", "b" * 64)

    from sqlalchemy import select

    from infra.db.prospect_inventory_tables import AcquisitionJobRow

    async with factory() as session:
        stored = await session.scalar(
            select(AcquisitionJobRow).where(
                AcquisitionJobRow.tenant_id == tenant,
                AcquisitionJobRow.job_id == job.id,
            )
        )
        assert stored.search_binding["effective_request_cap"] == 0
        assert len(stored.search_reservations) == 1
    await budget(10).bind()
    async with factory() as session:
        stored = await session.scalar(
            select(AcquisitionJobRow).where(
                AcquisitionJobRow.tenant_id == tenant,
                AcquisitionJobRow.job_id == job.id,
            )
        )
        assert stored.search_binding["effective_request_cap"] == 0


@pytest.mark.parametrize("retry_query", ["steel Kenya", "different products Kenya"])
async def test_actual_gateway_unknown_search_cannot_change_query_or_key(
    inventory, retry_query
):
    from types import SimpleNamespace

    from sqlalchemy import select

    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from apps.scheduler_worker.inventory_web import build_inventory_web
    from connectors.deepseek_search.client import DeepSeekSearchConnector
    from connectors.deepseek_search.transport import DeepSeekSearchError
    from infra.db.inventory_search_budget import InventorySearchBudget
    from infra.db.prospect_inventory_tables import AcquisitionJobRow
    from infra.db.tables import ToolCallRow
    from shared.schemas.identifiers import RunId, UserId
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.handlers.deepseek_search import DeepSeekSearchReaderFactory

    service, _, factory, tenant, _ = inventory
    await service.request(
        tenant,
        EmployeeId("emp"),
        InventoryQuery(product_tags=("steel",), country="KE"),
        1,
        "unknown-search",
    )
    job = await service.claim_job(tenant, lease_duration=timedelta(minutes=10))
    policy = InventorySearchPolicy(
        version="p1", paid_search_enabled=True, maximum_cost_cny="0.05"
    )
    budget = InventorySearchBudget(
        factory,
        tenant,
        job.id,
        job.lease_token,
        SearchBackendPin(
            backend_id="deepseek", connection_version="cv1", policy_version="p1"
        ),
        policy,
        now=lambda: NOW,
    )
    await budget.bind()
    await service.initialize_source_plan(
        tenant, job.id, job.lease_token, ("company_websites",)
    )
    await service.reserve_source(tenant, job.id, job.lease_token, "company_websites")
    calls = []

    class Transport:
        async def search(self, *args, **kwargs):
            calls.append(1)
            raise DeepSeekSearchError("unknown")

    async def guard():
        pass

    reader = DeepSeekSearchReaderFactory(
        tenant,
        RunId(str(job.id)),
        "company_websites",
        DeepSeekSearchConnector(
            Transport(),
            SimpleNamespace(resolve=lambda _: "synthetic"),
            base_url="https://api.deepseek.com",
        ),
        budget,
        guard,
    )

    async def tools(*_):
        return build_inventory_web(
            factory=factory,
            tenant_id=tenant,
            run_id=RunId(str(job.id)),
            user_id=UserId("usr"),
            country="KE",
            category="steel",
            guard=guard,
            fingerprints=HmacFingerprintProvider("v1", b"f" * 32),
            page_transport=object(),
            artifacts=object(),
            search_factory=reader,
            maximum_query_chars=128,
            now=lambda: NOW,
        )

    async def generate(job, source, session, payload):
        for query in ("steel Kenya", retry_query):
            try:
                await session.call(
                    "source_search", {"source_id": source, "query": query}
                )
            except ValueError:
                pass
        return {"candidates": []}

    await CodexInventoryCollector(tools, generate).collect(
        job, "company_websites", guard
    )
    assert len(calls) == 1
    with pytest.raises(ValueError, match="核对"):
        await budget.reserve("company_websites", "new-key")
    async with factory() as db:
        row = await db.scalar(
            select(AcquisitionJobRow).where(
                AcquisitionJobRow.tenant_id == tenant,
                AcquisitionJobRow.job_id == job.id,
            )
        )
        assert (
            len(row.search_reservations) == 1
            and row.search_reservations[0]["state"] == "started"
        )
        ledger = (
            await db.scalars(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == tenant,
                    ToolCallRow.run_id == str(job.id),
                    ToolCallRow.tool_id == "web.search",
                )
            )
        ).all()
        assert len(ledger) == 1 and ledger[0].status == "failed_transient"
