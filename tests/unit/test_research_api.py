"""研究状态与确认契约：只读快照不等于真实供应商验证。"""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from importlib import import_module

import pytest

from connectors.search_contracts import SearchCostStatus
from domains.directives.schemas import ProposalView
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId
from tool_gateway.free_search_contracts import SearchQuotaSnapshot

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
NOW = datetime(2026, 8, 27, tzinfo=UTC)


def module():
    try:
        return import_module("apps.api.research")
    except ModuleNotFoundError:
        pytest.fail("RED：研究状态及确认投影尚未实现")


class Quota:
    def __init__(self, snapshot=None):
        self.value = snapshot

    async def snapshot(self):
        return self.value


def snapshot(**changes):
    return replace(
        SearchQuotaSnapshot(
            TENANT,
            "tavily",
            7,
            93,
            SearchCostStatus.FREE,
            100,
            0,
            False,
            NOW,
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "configured,state,allowed",
    [
        (False, "not_configured", False),
        (True, "configured_unverified", True),
    ],
)
async def test_first_snapshot_is_not_required_to_request_gateway_check(
    configured, state, allowed
):
    service = module().ResearchAccessService(TENANT, Quota(), configured=configured)
    result = await service.status(TENANT)
    assert result.state == state
    assert result.can_confirm_research is allowed
    assert result.runtime_activation == "not_verified"
    assert result.remaining_lower_bound is None


@pytest.mark.parametrize(
    "value,state,remaining",
    [
        (snapshot(), "free_last_verified", 7),
        (snapshot(remaining=0), "quota_exhausted", 0),
        (snapshot(paygo_enabled=True), "paid_enabled", None),
        (snapshot(cost_status=SearchCostStatus.UNKNOWN), "usage_unknown", None),
    ],
)
async def test_status_distinguishes_paid_unknown_exhausted_and_safe_lower_bound(
    value, state, remaining
):
    service = module().ResearchAccessService(TENANT, Quota(value), configured=True)
    result = await service.status(TENANT)
    assert result.state == state
    assert result.remaining_lower_bound == remaining
    assert "reservations" not in result.model_dump()
    assert result.can_confirm_research is True
    assert result.confirmation_requires_recheck is (state != "free_last_verified")


async def test_snapshot_read_failure_stays_closed_until_read_recovers():
    class RecoveringQuota:
        failed = True

        async def snapshot(self):
            if self.failed:
                raise RuntimeError("敏感存储详情")
            return snapshot(cost_status=SearchCostStatus.UNKNOWN)

    quota = RecoveringQuota()
    service = module().ResearchAccessService(TENANT, quota, configured=True)
    failed = await service.status(TENANT)
    assert failed.state == "snapshot_unavailable"
    assert failed.can_confirm_research is False
    quota.failed = False
    recovered = await service.status(TENANT)
    assert recovered.can_confirm_research is True
    assert recovered.confirmation_requires_recheck is True


@pytest.mark.parametrize("remaining", [0, 7])
async def test_verified_included_credits_are_visible_with_unknown_overage_state(remaining):
    value = snapshot(
        cost_status=SearchCostStatus.UNKNOWN,
        paygo_enabled=None,
        included_credits_free=True,
        remaining=remaining,
    )
    service = module().ResearchAccessService(TENANT, Quota(value), configured=True)

    result = await service.status(TENANT)

    assert result.state == ("free_last_verified" if remaining else "quota_exhausted")
    assert result.remaining_lower_bound == remaining
    assert result.checked_at == NOW
    assert result.runtime_activation == "not_verified"
    assert value.cost_status is SearchCostStatus.UNKNOWN
    assert value.paygo_enabled is None


async def test_unverified_unknown_overage_does_not_claim_available_free_credits():
    value = snapshot(cost_status=SearchCostStatus.UNKNOWN, paygo_enabled=None)
    result = await module().ResearchAccessService(
        TENANT, Quota(value), configured=True,
    ).status(TENANT)

    assert result.state == "usage_unknown"
    assert result.remaining_lower_bound is None


@pytest.mark.parametrize("case", ["read_failure", "not_current", "not_configured", "missing_budget"])
async def test_execution_recovery_requires_known_absence_and_current_allowed_proposal(case):
    from types import SimpleNamespace

    class Reader:
        async def find_run(self, tenant, proposal_id):
            assert tenant == TENANT
            if case == "read_failure":
                raise RuntimeError("敏感详情")

    class Directives:
        async def get_active(self, tenant):
            return SimpleNamespace(source_proposal_id="another" if case == "not_current" else "dpr_test")

    proposal = ProposalView(
        "dpr_test", "只研究", "推断", [], {
            "execution_mode": "research_only",
            **{key: "1" for key in ("max_search_queries", "max_pages_read", "max_signals", "max_hypotheses")},
        }, "confirmed", NOW,
    )
    if case == "missing_budget":
        proposal = replace(proposal, parsed_fields={"execution_mode": "research_only"})
    result = await module().read_discovery_execution(
        TENANT, proposal, Directives(),
        module().ResearchAccessService(TENANT, Quota(), configured=case != "not_configured"),
        Reader(),
    )
    assert result.state == ("unknown" if case == "read_failure" else "not_started")
    assert result.can_resume is False
    assert result.run_id is None


async def test_status_rejects_other_tenant_and_mismatched_snapshot():
    service = module().ResearchAccessService(TENANT, Quota(), configured=True)
    with pytest.raises(PermissionDenied):
        await service.status(TenantId("tn_other"))
    mismatched = module().ResearchAccessService(
        TENANT,
        Quota(snapshot(tenant_id=TenantId("tn_other"))),
        configured=True,
    )
    with pytest.raises(PermissionDenied):
        await mismatched.status(TENANT)


async def test_proposal_projection_requires_budget_and_does_not_require_campaign():
    import json

    service = module().ResearchAccessService(TENANT, Quota(), configured=True)
    proposal = ProposalView(
        "dpr_test",
        "只研究",
        "待确认推断",
        ["不触达"],
        {
            "execution_mode": "research_only",
            "target_countries": "US",
            "target_categories": "hinges",
            "max_search_queries": "3",
            "max_pages_read": "2",
            "max_signals": "1",
            "max_hypotheses": "1",
            "queries": json.dumps(
                [
                    {"discovery_lane": lane}
                    for lane in ("importer", "distributor", "ecommerce")
                ]
            ),
        },
        "pending_confirmation",
        NOW,
    )
    projected = await service.proposal(TENANT, proposal)
    assert projected.can_confirm is True
    assert projected.execution_mode == "research_only"
    assert projected.planned_discovery_lanes == ("importer", "distributor", "ecommerce")
    assert projected.research_access.state == "configured_unverified"
    incomplete = replace(
        proposal, parsed_fields={**proposal.parsed_fields, "max_pages_read": ""}
    )
    assert (
        await service.proposal(TENANT, incomplete)
    ).confirmation_blocked_reason == "budget_missing"
    legacy = replace(proposal, parsed_fields={"campaign_id": "cmp_old"})
    assert (
        await service.proposal(TENANT, legacy)
    ).execution_mode == "outreach_preparation"


async def test_proposal_source_coverage_uses_saved_queries_without_claiming_execution():
    import json

    proposal = ProposalView(
        "dpr_sources", "多来源研究", "推断", [], {
            "execution_mode": "research_only",
            **{key: "8" for key in ("max_search_queries", "max_pages_read", "max_signals", "max_hypotheses")},
            "queries": json.dumps([
                {"query": "US hinges importer", "discovery_lane": "importer"},
                {"query": "US hinges industry directory companies", "discovery_lane": "distributor"},
                {"query": "US hinges site:linkedin.com/company/", "discovery_lane": "distributor"},
                {"query": "US locks site:linkedin.com/company/", "discovery_lane": "distributor"},
                {"query": "US hinges public shipment records bill of lading importer", "discovery_lane": "importer"},
                {"query": "arbitrary legacy query", "discovery_lane": "importer"},
                {"source_channel": "association_members"},
            ]),
        }, "pending_confirmation", NOW,
    )
    projected = await module().ResearchAccessService(
        TENANT, Quota(), configured=True,
    ).proposal(TENANT, proposal)
    assert projected.planned_source_channels == (
        "public_web", "industry_directory", "public_linkedin_company", "public_trade_records",
    )
    assert "searched_source_channels" not in asdict(projected)


@pytest.mark.parametrize("queries", ["null", "{}", "12", "[null, 12, {}]", "invalid"])
async def test_malformed_historical_queries_do_not_invent_source_coverage(queries):
    proposal = ProposalView(
        "dpr_sources", "历史研究", "推断", [],
        {"execution_mode": "research_only", "queries": queries},
        "confirmed", NOW,
    )
    projected = await module().ResearchAccessService(
        TENANT, Quota(), configured=True,
    ).proposal(TENANT, proposal)
    assert projected.planned_source_channels == ()


@pytest.mark.parametrize("configured,expected", [(False, 409), (True, 200)])
@pytest.mark.parametrize("record", [None, snapshot(cost_status=SearchCostStatus.UNKNOWN), snapshot(paygo_enabled=True)])
async def test_api_confirmation_gate_is_enforced_before_start(configured, expected, record):
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from apps.api.dependencies import get_api_dependencies, get_request_identity
    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from domains.employees.permissions import Phase1EmployeeAuthorizer
    from tests.unit.test_runs_router import _identity

    proposal_id = "dpr_01K39P9M5D6K4A91YEQ80EJZ0X"
    proposal = ProposalView(
        proposal_id,
        "只研究",
        "推断",
        ["不触达"],
        {
            "execution_mode": "research_only",
            "max_search_queries": "3",
            "max_pages_read": "2",
            "max_signals": "1",
            "max_hypotheses": "1",
            "target_countries": "US",
            "target_categories": "hinges",
            "queries": '[{"discovery_lane":"importer"},{"discovery_lane":"distributor"},{"discovery_lane":"ecommerce"}]',
        },
        "pending_confirmation",
        NOW,
    )

    class Directives:
        current = proposal

        async def get_proposal(self, tenant, requested):
            assert tenant == TENANT and requested == proposal_id
            return self.current

        async def confirm_proposal(self, tenant, requested, actor):
            self.current = replace(self.current, state="confirmed", decided_by_id=actor)

        async def get_active(self, tenant):
            return SimpleNamespace(
                source_proposal_id=proposal_id, directive_id="dir_test"
            )

    class Engine:
        def __init__(self):
            self.runs = []

        async def start(self, *args):
            self.runs.append(args)
            return "run_01K39P9M5D6K4A91YEQ80EJZ0X"

    from tests.research_ui_preview import preview_employees

    directives, engine = Directives(), Engine()
    app = create_app(
        settings=ApiSettings(tenant_id=TENANT, dev_mode=True, retry_after_seconds=17)
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity("boss")
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        employees=preview_employees,
        employee_lookup_actor=_identity("boss").employee_actor,
        directives=directives,
        trade_manager=None,
        workflow_engine=engine,
        employee_authorizer=Phase1EmployeeAuthorizer(TENANT),
        research_access=module().ResearchAccessService(
            TENANT, Quota(record), configured=configured
        ),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {
            "X-Employee-Id": str(_identity("boss").employee.employee_id),
            "X-Tenant-Id": str(TENANT),
        }
        read = await client.get(
            f"/commands/discovery-proposals/{proposal_id}", headers=headers
        )
        assert read.status_code == 200
        assert read.json()["execution_mode"] == "research_only"
        assert read.json()["can_confirm"] is configured
        assert engine.runs == []
        status = await client.get("/settings/research", headers=headers)
        assert status.status_code == 200
        assert status.json()["runtime_activation"] == "not_verified"
        result = await client.post(
            f"/commands/discovery-proposals/{proposal_id}/confirm", headers=headers
        )
        assert result.status_code == expected
        assert len(engine.runs) == int(configured)
        assert directives.current.state == (
            "confirmed" if configured else "pending_confirmation"
        )
