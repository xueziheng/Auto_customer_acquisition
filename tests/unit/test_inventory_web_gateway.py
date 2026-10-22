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
