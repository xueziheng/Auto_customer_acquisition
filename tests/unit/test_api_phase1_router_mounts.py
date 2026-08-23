from __future__ import annotations

from apps.api.main import create_app


def test_all_phase1_shallow_module_routers_are_mounted() -> None:
    paths = create_app().openapi()["paths"]

    assert {
        "/products/status",
        "/sourcing/status",
        "/costing-quotes/status",
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
        "/settings/status",
    }.issubset(paths)


def test_shallow_status_contract_is_truthful_and_has_no_secret_fields() -> None:
    schema = create_app().openapi()
    status_schema = schema["components"]["schemas"]["Phase1ModuleStatus"]

    assert status_schema["properties"]["state"]["const"] == "contract_only"
    assert "secret" not in status_schema["properties"]
    assert "credential" not in status_schema["properties"]
