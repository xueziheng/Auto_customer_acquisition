import importlib
from types import SimpleNamespace

import pytest


async def test_inventory_gate_binds_job_and_sets_only_public_research_preflight():
    try:
        Gate = importlib.import_module(
            "apps.scheduler_worker.inventory_web"
        ).InventoryWebGate
    except ModuleNotFoundError:
        pytest.fail("RED: inventory Gateway独立授权尚未实现")
    calls = []

    async def guard():
        calls.append("guard")

    gate = Gate(
        tenant_id="t",
        run_id="r",
        user_id="u",
        country="KE",
        category="tricycles",
        guard=guard,
    )
    state = SimpleNamespace(preflight=None)
    context = SimpleNamespace(
        tenant_id="t",
        run_id="r",
        user_id="u",
        params={"country": "KE", "category": "tricycles"},
    )
    assert await gate.check(context, state) is None
    assert state.preflight.country == "KE" and calls == ["guard"]
    context.tenant_id = "other"
    assert await gate.check(context, state) is not None


def test_page_only_composition_requires_no_search_credentials():
    from datetime import UTC, datetime

    from apps.scheduler_worker.inventory_web import build_inventory_web
    from shared.schemas.identifiers import RunId, TenantId, UserId
    async def guard():
        pass
    tools = build_inventory_web(factory=object(), tenant_id=TenantId("tenant"), run_id=RunId("run"),
        user_id=UserId("user"), country="KE", category="steel", guard=guard,
        fingerprints=__import__("tool_gateway.fingerprint", fromlist=["HmacFingerprintProvider"]).HmacFingerprintProvider("v1", b"f" * 32), page_transport=object(), artifacts=object(), now=lambda: datetime.now(UTC),
        candidate_url="https://example.com/")
    assert tools.archive_candidate is not None
