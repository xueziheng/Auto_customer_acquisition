"""三档执行预算、重放与循环授权；受控外部端口不调用真实供应商。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest

from domains.prospecting.service import ServicePlanState, tier_policy
from shared.errors import ValidationError
from workflows.demand_discovery.ports import DemandDiscoveryPlan, DiscoverySearchQuery

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def module():
    try:
        return import_module("workflows.demand_discovery.service_tiers")
    except ModuleNotFoundError:
        pytest.fail("三档研究执行尚未接线")


def plan(mode="research_only"):
    return DemandDiscoveryPlan("研究需求", tuple(DiscoverySearchQuery(
        f"query {i}", "US", "valves", 5, ("importer", "distributor", "ecommerce")[i % 3]
    ) for i in range(30)), ("US",), ("valves",), (), (), 30, 50, 40, 40,
        "medium", "research", "", (), "", mode)


def state(tier="basic"):
    return ServicePlanState(tenant_id="tenant-a", tier=tier, version=1,
        period_started_at=NOW, period_ends_at=NOW + timedelta(days=30),
        candidates_reserved=0, searches_reserved=0, contacts_reserved=0,
        schedule_enabled=False)


@pytest.mark.parametrize("tier,searches,pages,signals", [
    ("basic",10,10,10), ("standard",20,25,20), ("advanced",30,40,30),
])
def test_tiers_restrict_confirmed_plan_and_preserve_three_lanes(tier, searches, pages, signals):
    result = module().bounded_plan(plan(), tier_policy(tier))
    assert len(result.queries) == searches
    assert result.max_pages_read == pages
    assert result.max_signals == signals
    assert {q.discovery_lane for q in result.queries} == {"importer", "distributor", "ecommerce"}
    assert set(result.queries).issubset(set(plan().queries))


def test_small_explicit_budget_never_expands_to_tier_allowance():
    small = replace(plan(), queries=plan().queries[:3], max_search_queries=3,
                    max_pages_read=3, max_signals=2, max_hypotheses=1)
    result = module().bounded_plan(small, tier_policy("advanced"))
    assert result == small


class Store:
    def __init__(self, value):
        self.value = value
        self.reservations = {}
        self.receipts = {}
        self.reserve_calls = 0
    async def get(self, tenant_id):
        return self.value
    async def reserve(self, tenant_id, **kwargs):
        self.reserve_calls += 1
        self.reservations.setdefault(kwargs["key"], kwargs)
        self.receipts.setdefault(kwargs["key"], SimpleNamespace(
            key=kwargs["key"], tier=self.value.tier, created_at=kwargs["now"],
            period_started_at=self.value.period_started_at,
            candidates=kwargs["candidates"], searches=kwargs["searches"], contacts=kwargs["contacts"],
        ))
    async def reservation(self, tenant_id, *, key, now):
        return self.receipts.get(key)


class Reader:
    async def load_confirmed(self, tenant_id, proposal_id, actor):
        return plan()


async def test_expired_plan_stops_before_delegate_runs():
    m = module()
    store = Store(state())
    reader = m.TieredDiscoveryTaskReader(Reader(), store, now=lambda: NOW + timedelta(days=31))
    with pytest.raises(ValidationError):
        await reader.load_confirmed("tenant-a", "proposal", "boss")


async def test_budget_is_reserved_before_research_and_not_contact_or_send():
    m = module()
    store = Store(state())
    reader = m.TieredDiscoveryTaskReader(Reader(), store, now=lambda: NOW)
    class Handler:
        async def execute(self, run):
            assert store.reservations["run-a:research"]["searches"] == 10
            return "advance", "generate_hypotheses", {"queued_count": 0}
    handler = m.TieredResearchStep(Handler(), reader, store, now=lambda: NOW)
    result = await handler.execute(SimpleNamespace(tenant_id="tenant-a",run_id="run-a",
        subject_ref="proposal",context={"proposal_id":"proposal", "acting_user_id":"boss"}))
    reservation = store.reservations["run-a:research"]
    assert reservation["candidates"] == 10 and reservation["contacts"] == 0
    assert result[2]["service_tier"] == "basic"
    assert result[2]["queued_count"] == 0


@pytest.mark.parametrize("original,new_tier,days", [
    ("basic", "basic", 8), ("basic", "standard", 1), ("standard", "basic", 8),
])
async def test_retry_preserves_original_queries_and_reservation(original, new_tier, days):
    m = module()
    store = Store(state(original))
    clock = [NOW]
    plans = []
    reader = m.TieredDiscoveryTaskReader(Reader(), store, now=lambda: clock[0])

    class Handler:
        async def execute(self, run):
            plans.append(await reader.load_confirmed(run.tenant_id, "proposal", "boss"))
            return "advance", "generate_hypotheses", {}

    handler = m.TieredResearchStep(Handler(), reader, store, now=lambda: clock[0])
    run = SimpleNamespace(tenant_id="tenant-a", run_id="run-retry", subject_ref="proposal",
        context={"proposal_id": "proposal", "acting_user_id": "boss"})
    await handler.execute(run)
    clock[0] += timedelta(days=days)
    store.value = state(new_tier)
    await handler.execute(run)
    assert store.reserve_calls == 1
    assert set(plans[1].queries).issubset(plans[0].queries)
    assert plans[1].max_pages_read <= plans[0].max_pages_read
    assert plans[1].max_signals <= plans[0].max_signals
    if original != "standard":
        assert plans[1] == plans[0]
