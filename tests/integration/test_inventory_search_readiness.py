"""独立API与worker共享当前绑定的执行投影；GET不探测或创建run。"""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from apps.api.dependencies import get_request_identity
from apps.api.routers.prospect_inventory import router
from apps.scheduler_worker.inventory_web import build_inventory_web
from connectors.deepseek_search.client import DeepSeekSearchConnector
from connectors.deepseek_search.transport import (
    DeepSeekSearchError,
    DeepSeekSearchPermanentError,
)
from domains.opportunities.permissions import ScopeLevel
from domains.prospecting.inventory import InventoryQuery
from infra.db.inventory_search_budget import InventorySearchBudget
from infra.db.inventory_search_readiness import InventorySearchReadinessReader
from infra.db.inventory_sources import InventorySources
from infra.db.prospect_inventory_tables import AcquisitionJobRow
from infra.db.tables import ToolCallRow
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import EmployeeId, RunId, UserId
from shared.schemas.search_backend import InventorySearchPolicy, SearchBackendPin
from tests.integration.test_prospect_inventory import NOW
from tests.integration.test_prospect_inventory import inventory as inventory_fixture
from tests.unit.test_standalone_model_settings import settings as model_settings
from tool_gateway.errors import ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.deepseek_search import DeepSeekSearchReaderFactory

inventory = inventory_fixture


@pytest.mark.parametrize("outcome", ["success", "failed", "unknown"])
async def test_api_reads_worker_search_without_io_and_drops_stale_versions(
    inventory, tmp_path, monkeypatch, outcome
):
    backend, _, factory, tenant, _ = inventory
    settings = StandaloneModelSettings.model_validate(
        {
            **model_settings(),
            "api_business_tenant": tenant,
            "api_control_tenant": "platform",
            "api_key_file": tmp_path / "unused-synthetic-reference",
            "api_connection_version": "cv1",
            "api_verified_at": NOW,
        }
    )
    policy = InventorySearchPolicy(
        version="p1", paid_search_enabled=True, maximum_cost_cny="0.05"
    )

    def sources(current=settings, current_policy=policy):
        return InventorySources(
            tenant_id=tenant,
            backend=backend,
            quota=object(),
            configured=False,
            search_policy=current_policy,
            search_readiness=InventorySearchReadinessReader(
                factory, tenant, current, now=lambda: NOW
            ).read,
        )

    worker = sources()
    app = FastAPI()
    app.include_router(router, prefix="/prospects")
    app.state.prospect_inventory_factory = lambda _: backend
    app.state.inventory_sources = sources()  # 与worker不同实例，没有probe。
    app.dependency_overrides[get_request_identity] = lambda: SimpleNamespace(
        tenant_id=tenant,
        employee=SimpleNamespace(is_active=True, tenant_id=tenant, role="boss"),
        opportunity_actor=SimpleNamespace(
            scope=SimpleNamespace(level=ScopeLevel.TENANT)
        ),
    )
    monkeypatch.setattr(
        "infra.model_catalog.FileModelCatalog.for_run",
        lambda *a: pytest.fail("GET不得创建run pin"),
    )

    monkeypatch.setattr(
        "infra.model_api.ModelKeyFileResolver.resolve",
        lambda *a: pytest.fail("GET不得解析Key"),
    )
    monkeypatch.setattr(
        "connectors.deepseek_search.transport.DeepSeekSearchApiTransport.search",
        lambda *a, **kw: pytest.fail("GET不得调用供应商"),
    )

    async def snapshot(client):
        response = await client.get("/prospects/inventory/sources")
        assert response.status_code == 200
        return next(
            row["state"]
            for row in response.json()
            if row["source_id"] == "company_websites"
        )

    calls = []

    class Transport:
        async def search(self, *args, **kwargs):
            calls.append(1)
            in_flight = await snapshot(client)
            assert in_flight["availability"] == "available"
            assert in_flight["search_backend"]["search_execution_state"] == "running"
            if outcome == "failed":
                raise DeepSeekSearchPermanentError("authentication")
            if outcome == "unknown":
                raise DeepSeekSearchError("unknown")
            return {
                "results": [
                    {
                        "title": "Public",
                        "url": "https://example.com/",
                        "snippet": "Public result",
                    }
                ]
            }

    async def guard():
        pass

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        initial = await snapshot(client)
        assert initial["availability"] == "available"  # 可调度，不代表搜索已实测。
        assert initial["search_backend"]["search_execution_state"] == "not_run"
        await backend.request(
            tenant,
            EmployeeId("emp"),
            InventoryQuery(product_tags=("steel",), country="KE"),
            1,
            "readiness",
        )
        job = await backend.claim_job(tenant, lease_duration=timedelta(minutes=10))
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
        await backend.initialize_source_plan(
            tenant, job.id, job.lease_token, ("company_websites",)
        )
        await backend.reserve_source(
            tenant, job.id, job.lease_token, "company_websites"
        )
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
        tools = build_inventory_web(
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
            now=lambda: NOW,
        )
        if outcome == "success":
            batch = await tools.searcher.search(
                tenant,
                RunId(str(job.id)),
                "steel Kenya",
                "KE",
                "steel",
                1,
                quota_request_key="a" * 64,
            )
            tools.searcher.release(batch)
        else:
            with pytest.raises(ToolGatewayError):
                await tools.searcher.search(
                    tenant,
                    RunId(str(job.id)),
                    "steel Kenya",
                    "KE",
                    "steel",
                    1,
                    quota_request_key="a" * 64,
                )
        expected = "succeeded" if outcome == "success" else outcome
        projected = await snapshot(client)
        assert projected["search_backend"]["search_execution_state"] == expected
        assert projected["availability"] == "available"
        assert (
            next(
                s
                for s in await worker.list_sources(tenant)
                if s.source_id == "company_websites"
            ).model_dump(mode="json")
            == projected
        )
        if outcome == "unknown":
            # 旧任务仍不能换key重派，但独立新任务不会被历史unknown永久锁死。
            with pytest.raises(ValueError, match="核对"):
                await budget.reserve("company_websites", "b" * 64)
            await backend.request(
                tenant,
                EmployeeId("emp"),
                InventoryQuery(product_tags=("steel",), country="US"),
                1,
                "new-job",
            )
            new_job = await backend.claim_job(
                tenant, lease_duration=timedelta(minutes=10)
            )
            assert new_job.id != job.id
            new_budget = InventorySearchBudget(
                factory,
                tenant,
                new_job.id,
                new_job.lease_token,
                SearchBackendPin(
                    backend_id="deepseek", connection_version="cv1", policy_version="p1"
                ),
                policy,
                now=lambda: NOW,
            )
            await new_budget.bind()
            await backend.initialize_source_plan(
                tenant, new_job.id, new_job.lease_token, ("company_websites",)
            )
            await backend.reserve_source(
                tenant, new_job.id, new_job.lease_token, "company_websites"
            )
            await new_budget.reserve("company_websites", "c" * 64)
            assert (await snapshot(client))["availability"] == "available"
        for patch, availability, connection in [
            ({"api_connection_version": "cv2"}, "available", "passed"),
            ({"api_connection_version": None}, "not_configured", "not_configured"),
            ({"api_verified_at": None}, "unhealthy", "not_run"),
            ({"api_business_tenant": "other"}, "not_configured", "not_configured"),
        ]:
            app.state.inventory_sources = sources(settings.model_copy(update=patch))
            state = await snapshot(client)
            assert state["availability"] == availability
            assert state["search_backend"]["connection_status"] == connection
            if connection != "not_run":
                assert state["search_backend"]["search_execution_state"] == "not_run"
        # 同政策名但内容变化也不能继承成功或失败。
        app.state.inventory_sources = sources(
            current_policy=policy.model_copy(update={"maximum_requests": 5})
        )
        assert (await snapshot(client))["search_backend"][
            "search_execution_state"
        ] == "not_run"
        assert len(calls) == 1
        async with factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ToolCallRow)
                    .where(ToolCallRow.tenant_id == tenant)
                )
                == 1
            )
            job_row = await db.scalar(
                select(AcquisitionJobRow).where(
                    AcquisitionJobRow.tenant_id == tenant,
                    AcquisitionJobRow.job_id == job.id,
                )
            )
            assert len(job_row.search_reservations) == 1
            assert job_row.search_binding["connection_version"] == "cv1"
