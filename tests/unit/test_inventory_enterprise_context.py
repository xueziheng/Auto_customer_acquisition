"""企业搜索上下文不得流入公共目录或外部搜索。"""

import pytest
from pydantic import ValidationError

from apps.scheduler_worker.inventory_query import compose_inventory_query
from domains.prospecting.inventory import InventoryQuery


def test_multiregion_private_projection_and_identity():
    q = InventoryQuery(
        product_tags=("hinges",),
        regions=("africa", "europe"),
        countries=("KE", "DE"),
        intent="寻找经销商",
        confirmed_refs=({"document_id": "doc1", "revision_id": "rev1"},),
    )
    assert q.public_projection().intent is None
    assert not q.public_projection().confirmed_refs
    assert q.target_countries == ("DE", "KE")
    assert q != q.model_copy(update={"intent": "寻找工厂"})


def test_contradictory_region_rejected():
    with pytest.raises(ValidationError):
        InventoryQuery(product_tags=("hinges",), regions=("africa",), countries=("DE",))


def test_private_model_supplement_is_deterministically_removed():
    q = InventoryQuery(
        product_tags=("hinges",),
        intent="目标为经销商",
        confirmed_refs=({"document_id": "doc1", "revision_id": "rev1"},),
    )
    result = compose_inventory_query(
        q,
        "official company website contact",
        "SECRETMOQ acme.example",
        public_domains=("acme.example",),
    )
    assert "SECRETMOQ" not in result
    assert "acme.example" in result


async def test_reader_only_accepts_current_confirmed_revision():
    from datetime import UTC, datetime

    from domains.products.service import EnterpriseKnowledgeServiceImpl
    from shared.errors import PermissionDenied
    from tests.unit.test_enterprise_knowledge import (
        BOSS,
        SALES,
        TENANT,
        MemoryRepo,
        MemoryUow,
        processed,
    )
    from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader

    repo = MemoryRepo()
    service = EnterpriseKnowledgeServiceImpl(
        lambda tenant: MemoryUow(repo), now=lambda: datetime.now(UTC)
    )
    doc, _, detail = await processed(service)
    reader = ConfirmedContextReader(service)
    refs = (
        {"document_id": doc.document_id, "revision_id": detail.revision.revision_id},
    )
    with pytest.raises(PermissionDenied):
        await reader.read(TENANT, SALES.employee_id, refs)
    await service.confirm(
        TENANT,
        BOSS,
        doc.document_id,
        revision_id=detail.revision.revision_id,
        expected_version=detail.document.version,
    )
    context = await reader.read(TENANT, SALES.employee_id, refs)
    assert context[0]["facts"][0]["value"] == "stainless steel"
    assert context[0]["fact_provenance"]
    with pytest.raises(PermissionDenied):
        await reader.read(
            TENANT, SALES.employee_id, ({**refs[0], "revision_id": "old"},)
        )
    repo.roles.pop(SALES.employee_id)
    with pytest.raises(PermissionDenied):
        await reader.read(TENANT, SALES.employee_id, refs)


async def test_revoked_context_blocks_next_external_tool_during_model_loop():
    from types import SimpleNamespace

    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from shared.errors import PermissionDenied
    from tests.unit.test_inventory_acquisition_driver import JOB

    calls = []
    live = [True]

    class Reader:
        async def read(self, *args):
            if not live[0]:
                raise PermissionDenied("撤权")
            return []

    async def search(*args, **kwargs):
        calls.append(args[2])
        live[0] = False
        return SimpleNamespace(results=())

    async def factory(*args):
        return SimpleNamespace(searcher=SimpleNamespace(search=search))

    async def guard():
        pass

    async def generate(job, source, session, payload):
        await session.call("source_search", {"source_id": source, "query": "SECRET"})
        await session.call("source_search", {"source_id": source, "query": "SECRET"})

    job = JOB.model_copy(
        update={
            "query": InventoryQuery(
                product_tags=("hinges",),
                confirmed_refs=({"document_id": "doc1", "revision_id": "rev1"},),
            )
        }
    )
    with pytest.raises(ValueError, match="采集工具失败"):
        await CodexInventoryCollector(
            factory, generate, context_reader=Reader()
        ).collect(job, "company_websites", guard)
    assert len(calls) == 1
    assert "SECRET" not in calls[0]


def test_multi_region_country_evidence_supported_outside_original_africa_set():
    from datetime import UTC, datetime

    from domains.prospecting.inventory import matches_query
    from domains.prospecting.public_evidence import project_candidate

    candidate = project_candidate(
        source_id="company_websites",
        source_url="https://acme.example/contact",
        text="Acme Ltd\nAddress: Canada\nHinges",
        content_hash="a" * 64,
        artifact_id="art_test",
        observed_at=datetime.now(UTC),
        name="Acme Ltd",
        country="CA",
        product_tags=("hinges",),
        public_emails=(),
    )
    assert matches_query(
        candidate, InventoryQuery(product_tags=("hinges",), regions=("americas",))
    )
    assert not matches_query(
        candidate, InventoryQuery(product_tags=("hinges",), regions=("europe",))
    )


async def test_selected_search_country_and_page_batch_share_gateway_preflight():
    from types import SimpleNamespace

    from apps.scheduler_worker.inventory_web import InventoryWebGate
    from shared.schemas.identifiers import new_id
    from tool_gateway.handlers.web_slots import WebSearchResultSlot

    async def guard():
        pass

    slot = WebSearchResultSlot(new_id, maximum_batches=4)
    batch = slot.put("t", "NG", "hinges", ())
    gate = InventoryWebGate(
        tenant_id="t",
        run_id="r",
        user_id="u",
        country="KE",
        countries=("KE", "NG"),
        category="hinges",
        search_slot=slot,
        guard=guard,
    )
    state = SimpleNamespace(preflight=None)
    ctx = SimpleNamespace(
        tenant_id="t",
        run_id="r",
        user_id="u",
        params={"search_result_handle": batch.handle, "result_index": 0},
    )
    assert await gate.check(ctx, state) is None
    assert state.preflight.country == "NG"
    ctx.params = {"country": "DE", "category": "hinges"}
    assert await gate.check(ctx, state) is not None


async def test_multicountry_tools_keep_public_domain_followup_and_strip_private_words():
    from types import SimpleNamespace

    from apps.scheduler_worker.inventory_collector import CodexInventoryCollector
    from tests.unit.test_inventory_acquisition_driver import JOB

    calls = []

    class Reader:
        async def read(self, *args):
            return []

    async def search(tenant, run, query, country, *args, **kwargs):
        calls.append((query, country))
        return SimpleNamespace(
            results=(
                SimpleNamespace(
                    url="https://acme.example/contact",
                    title="Acme",
                    description="Hinges",
                ),
            )
        )

    async def factory(*args):
        return SimpleNamespace(searcher=SimpleNamespace(search=search))

    async def guard():
        pass

    async def generate(job, source, session, payload):
        await session.call("source_search", {"source_id": source, "query": "SECRETMOQ"})
        await session.call(
            "source_search", {"source_id": source, "query": "SECRETMOQ acme.example"}
        )
        return {"candidates": []}

    query = InventoryQuery(
        product_tags=("hinges",),
        countries=("KE", "DE"),
        intent="内部要求",
        confirmed_refs=({"document_id": "doc1", "revision_id": "rev1"},),
    )
    await CodexInventoryCollector(factory, generate, context_reader=Reader()).collect(
        JOB.model_copy(update={"query": query}), "company_websites", guard
    )
    assert [country for _, country in calls] == ["DE", "KE"]
    assert "Germany" in calls[0][0] and "Kenya" in calls[1][0]
    assert "acme.example" in calls[1][0]
    assert all("SECRETMOQ" not in text and "内部要求" not in text for text, _ in calls)


async def test_http_rejects_stale_reference_before_catalog_allocation(monkeypatch):
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from domains.products.schemas import KnowledgeActor
    from domains.products.service import EnterpriseKnowledgeServiceImpl
    from tests.unit.test_enterprise_knowledge import (
        ANALYSIS,
        SOURCE,
        TEXT,
        MemoryRepo,
        MemoryUow,
    )
    from tests.unit.test_prospect_inventory_api import EMPLOYEE, TENANT, setup

    app, inventory = setup(role="boss")
    repo = MemoryRepo()
    repo.roles[EMPLOYEE] = ("boss", "usr_fixture")
    knowledge = EnterpriseKnowledgeServiceImpl(lambda tenant: MemoryUow(repo))
    actor = KnowledgeActor(tenant_id=TENANT, employee_id=EMPLOYEE)
    doc = await knowledge.register_upload(
        TENANT, actor, SOURCE, idempotency_key="http-fixture"
    )
    claim = await knowledge.claim_next(TENANT, lease_owner="fixture")
    detail = await knowledge.complete_processing(
        TENANT,
        claim,
        source_text=TEXT,
        analysis=ANALYSIS,
        model="synthetic",
        extracted_by="synthetic",
    )
    await knowledge.confirm(
        TENANT,
        actor,
        doc.document_id,
        revision_id=detail.revision.revision_id,
        expected_version=detail.document.version,
    )
    monkeypatch.setattr(
        "apps.api.routers.prospect_inventory.get_api_dependencies",
        lambda request: SimpleNamespace(
            enterprise_knowledge=SimpleNamespace(service=knowledge)
        ),
    )
    body = {
        "query": {
            "product_tags": ["hinges"],
            "intent": "寻找经销商",
            "countries": ["KE", "DE"],
            "confirmed_refs": [
                {
                    "document_id": doc.document_id,
                    "revision_id": detail.revision.revision_id,
                }
            ],
        },
        "count": 1,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json=body,
            headers={"Idempotency-Key": "valid"},
        )
        assert response.status_code == 202
        assert inventory.calls[0][2].intent == "寻找经销商"
        inventory.calls.clear()
        body["query"]["confirmed_refs"][0]["revision_id"] = "stale"
        response = await client.post(
            "/prospects/inventory/requests",
            json=body,
            headers={"Idempotency-Key": "stale"},
        )
        assert response.status_code == 403
        assert not inventory.calls


def test_model_can_narrow_only_to_selected_public_country_and_known_business_strategy():
    from apps.scheduler_worker.inventory_query import choose_inventory_country

    query = InventoryQuery(
        product_tags=("hinges",), countries=("DE", "KE"), intent="内部客户画像"
    )
    assert choose_inventory_country(query, "Kenya distributor SECRET", 0) == "KE"
    assert choose_inventory_country(query, "China SECRET", 0) == "DE"
    text = compose_inventory_query(
        query.model_copy(update={"countries": (), "country": "KE"}),
        "official company website contact",
        "distributor SECRET",
    )
    assert "distributor" in text and "SECRET" not in text
