import importlib
from types import SimpleNamespace

import pytest

from connectors.search_contracts import SearchCostStatus


def reader_type():
    try:
        return importlib.import_module("infra.db.inventory_sources").InventorySources
    except ModuleNotFoundError:
        pytest.fail("RED: 运行来源reader尚未实现")


async def test_sources_are_unavailable_until_free_quota_is_verified():
    class Backend:
        async def source_history(self, tenant):
            return ()

    class Quota:
        value = None

        async def snapshot(self):
            return self.value

    quota = Quota()
    reader = reader_type()(
        tenant_id="tenant", backend=Backend(), quota=quota, configured=True
    )
    states = await reader.list_sources("tenant")
    assert len(states) == 33 and not any(s.availability == "available" for s in states)
    quota.value = SimpleNamespace(
        cost_status=SearchCostStatus.FREE, remaining=5, included_credits_free=True
    )
    states = await reader.list_sources("tenant")
    assert {s.source_id for s in states if s.availability == "available"} >= {
        "company_websites",
        "dealer_networks",
        "industry_directories",
        "trade_associations",
        "chambers",
        "trade_fairs",
    }
    assert (
        next(s for s in states if s.source_id == "sam_gov").availability
        == "not_integrated"
    )
    with pytest.raises(ValueError):
        await reader.list_sources("other")


async def test_cold_start_prefers_company_sites_and_keeps_exploration():
    from domains.prospecting.service import select_sources
    class Backend:
        async def source_history(self, tenant): return ()
    class Quota:
        async def snapshot(self):
            return SimpleNamespace(cost_status=SearchCostStatus.FREE, remaining=5, included_credits_free=True)
    states=await reader_type()(tenant_id='tenant',backend=Backend(),quota=Quota(),configured=True).list_sources('tenant')
    plan=select_sources(list(states),3)
    assert plan[:2] == ('company_websites','dealer_networks')
    assert plan[-1] == 'chambers'

@pytest.mark.parametrize('initial', [None, SearchCostStatus.UNKNOWN, SearchCostStatus.FREE, SearchCostStatus.PAID])
async def test_prepare_recovers_only_unverified_usage_under_guard(initial):
    class Backend:
        async def source_history(self, tenant): return ()
    class Quota:
        value = None if initial is None else SimpleNamespace(cost_status=initial, remaining=5, included_credits_free=False)
        async def snapshot(self): return self.value
    quota = Quota()
    calls = []
    async def guard(): calls.append('guard')
    async def probe(job, checked):
        await checked()
        calls.append('probe')
        quota.value = SimpleNamespace(cost_status=SearchCostStatus.FREE, remaining=5, included_credits_free=False)
    reader = reader_type()(tenant_id='tenant', backend=Backend(), quota=quota, configured=True, probe=probe)
    before = await reader.list_sources('tenant')
    if initial in (None, SearchCostStatus.UNKNOWN):
        assert next(s for s in before if s.source_id == 'company_websites').availability == 'unhealthy'
    assert calls == []
    await reader.prepare(SimpleNamespace(tenant_id='tenant'), guard)
    await reader.prepare(SimpleNamespace(tenant_id='tenant'), guard)
    assert calls == (['guard', 'guard', 'probe'] if initial in (None, SearchCostStatus.UNKNOWN) else [])
    after = await reader.list_sources('tenant')
    assert next(s for s in after if s.source_id == 'company_websites').availability == ('permission_denied' if initial is SearchCostStatus.PAID else 'available')


@pytest.mark.parametrize(
    ('account', 'expected_state', 'expected_remaining', 'should_probe'),
    [
        ({'current_plan': 'Researcher', 'plan_limit': 5, 'plan_usage': 0,
          'paygo_usage': 0, 'paygo_limit': None}, 'available', 5, False),
        ({'current_plan': 'Researcher', 'plan_limit': 5, 'plan_usage': 5,
          'paygo_usage': 0, 'paygo_limit': None}, 'quota_exhausted', 0, False),
        ({'current_plan': 'Researcher', 'plan_limit': 5, 'plan_usage': 0,
          'paygo_usage': 0, 'paygo_limit': 0}, 'available', 5, False),
        ({'current_plan': 'Researcher', 'plan_limit': 5, 'plan_usage': 0,
          'paygo_usage': 0}, 'unhealthy', None, True),
        ({'current_plan': 'Project', 'plan_limit': 5, 'plan_usage': 0,
          'paygo_usage': 0, 'paygo_limit': 0}, 'permission_denied', None, False),
    ],
    ids=['included-free', 'included-exhausted', 'free', 'unverified', 'paid'],
)
async def test_parser_usage_proof_controls_source_plan_and_prepare(
    account, expected_state, expected_remaining, should_probe
):
    from connectors.tavily.client import _decode_usage
    from domains.prospecting.service import select_sources
    from tests.unit.test_inventory_acquisition_driver import JOB
    from tool_gateway.free_search_contracts import (
        SearchQuotaSnapshot,
        verified_free_remaining,
    )

    usage = _decode_usage({'account': account})
    assert verified_free_remaining(usage) == expected_remaining
    if account.get('paygo_limit', 'missing') is None:
        assert usage.cost_status is SearchCostStatus.UNKNOWN
        assert usage.paygo_enabled is None
        assert usage.included_credits_free is True
    snapshot = SearchQuotaSnapshot(
        tenant_id=JOB.tenant_id, provider='tavily',
        remaining=expected_remaining or 0, reservations=0,
        cost_status=usage.cost_status, usage_limit=usage.limit, usage_used=usage.used,
        paygo_enabled=usage.paygo_enabled, checked_at=JOB.created_at,
        included_credits_free=usage.included_credits_free,
    )

    class Backend:
        async def source_history(self, tenant):
            return ()

    class Quota:
        async def snapshot(self):
            return snapshot

    calls = []

    async def guard():
        calls.append('guard')

    async def probe(job, checked):
        await checked()
        calls.append('probe')

    reader = reader_type()(
        tenant_id=JOB.tenant_id, backend=Backend(), quota=Quota(),
        configured=True, probe=probe,
    )
    states = await reader.list_sources(JOB.tenant_id)
    website = next(s for s in states if s.source_id == 'company_websites')
    assert website.availability == expected_state
    assert website.cost_class == ('free' if expected_remaining is not None else 'unknown')
    assert bool(select_sources(list(states), 3)) is (expected_state == 'available')
    assert calls == []  # GET投影不得访问Provider。
    await reader.prepare(JOB, guard)
    assert calls == (['guard', 'guard', 'probe'] if should_probe else [])


@pytest.mark.parametrize('configured,status,remaining,expected', [
    (False, SearchCostStatus.FREE, 5, 'not_configured'),
    (True, SearchCostStatus.UNKNOWN, 5, 'unhealthy'),
    (True, SearchCostStatus.FREE, 0, 'quota_exhausted'),
    (True, SearchCostStatus.PAID, 5, 'permission_denied'),
    (True, SearchCostStatus.FREE, 5, 'available'),
])
async def test_support_tools_report_actual_capability_without_becoming_sources(configured, status, remaining, expected):
    from domains.prospecting.service import select_sources

    class Backend:
        async def source_history(self, tenant): return ()
    class Quota:
        async def snapshot(self):
            return SimpleNamespace(cost_status=status, remaining=remaining, included_credits_free=False)
    states = await reader_type()(tenant_id='tenant', backend=Backend(), quota=Quota(), configured=configured).list_sources('tenant')
    by_id = {s.source_id: s for s in states}
    tavily = by_id['tavily']
    assert tavily.inventory_capability == 'search_backend'
    assert tavily.source_role == 'base_search'
    assert tavily.availability == expected
    assert by_id['codex_cli'].inventory_capability == 'model_runner'
    assert by_id['codex_cli'].source_role == 'model_executor'
    assert by_id['codex_cli'].availability != 'available'
    assert by_id['brave'].inventory_capability == 'not_integrated'
    assert by_id['brave'].availability == 'not_integrated'
    assert by_id['hunter'].source_role == 'contact_enrichment'
    assert by_id['ted'].source_role == 'procurement_signal'
    assert by_id['rss'].source_role == 'incremental_subscription'
    assert by_id['company_websites'].inventory_capability == 'web_discovery'
    assert len(states) == 33
    # 即使支持工具显示配额可用，也不能成为独立企业来源。
    assert not {'tavily', 'codex_cli', 'brave'} & set(select_sources(list(states), 33))


async def test_model_runner_configuration_snapshot_never_proves_account_is_runnable():
    class Backend:
        async def source_history(self, tenant): return ()
    class Quota:
        async def snapshot(self): return None
    values = []
    async def model_snapshot(tenant):
        assert tenant == 'tenant'
        return values[-1]
    reader = reader_type()(tenant_id='tenant', backend=Backend(), quota=Quota(), configured=False, model_snapshot=model_snapshot)
    for snapshot, expected in [(None, 'not_configured'), (SimpleNamespace(model_data_export_enabled=False), 'permission_denied'), (SimpleNamespace(model_data_export_enabled=True), 'unhealthy')]:
        values.append(snapshot)
        state = next(s for s in await reader.list_sources('tenant') if s.source_id == 'codex_cli')
        assert state.availability == expected
        assert state.inventory_capability == 'model_runner'


def test_planner_rejects_support_tools_even_with_available_states():
    from domains.prospecting.service import SourceState, select_sources
    states = [SourceState(source_id=s, availability='available', cost_class='free', relevant=True) for s in ('tavily', 'brave', 'codex_cli', 'hunter', 'company_websites')]
    assert select_sources(states, 33) == ('company_websites',)
