from __future__ import annotations

from apps.api.main import create_app


def test_phase1_module_routers_expose_their_real_contracts() -> None:
    paths = create_app().openapi()["paths"]

    assert {
        "/products",
        "/sourcing-cases",
        "/costing-quotes/opportunities/{opportunity_id}/cost-sheets",
        "/costing-quotes/cost-sheets/{cost_sheet_id}",
        "/costing-quotes/cost-sheets/{cost_sheet_id}/items",
        "/costing-quotes/cost-sheets/{cost_sheet_id}/readiness",
        "/team/employees",
        "/team/territory",
        "/work-uploads",
        "/work-uploads/{upload_id}/artifact",
        "/work-uploads/{upload_id}/extraction",
        "/work-uploads/{upload_id}/confirm",
        "/commitments",
        "/commitments/overdue",
        "/runs",
        "/runs/{run_id}",
        "/settings/playbook",
        "/settings/playbook/proposals",
        "/settings/playbook/versions",
    }.issubset(paths)


def test_replaced_shallow_modules_do_not_advertise_contract_only_status() -> None:
    schema = create_app().openapi()

    assert "Phase1ModuleStatus" not in schema["components"]["schemas"]
    assert "/products/status" not in schema["paths"]
    assert "/sourcing/status" not in schema["paths"]
