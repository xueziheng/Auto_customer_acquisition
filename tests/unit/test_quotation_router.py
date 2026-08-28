"""真实ASGI wire与安全错误；服务替身仅测适配，实际factory另有PG/Linux验收。"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.unit.test_costing_router import EMPLOYEE, TENANT, _app


class Port:
    """受控公共端口记录真实HTTP传入的值；不声称领域事务验收。"""

    def __init__(self, calls, values):
        self.calls, self.values, self.failure = calls, values, None

    def __getattr__(self, method):
        async def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            if self.failure is not None:
                raise self.failure
            return self.values[method]

        return call


@pytest_asyncio.fixture
async def wire_case():
    from domains.costing.schemas import QuoteFxView
    from domains.quotations.schemas import QuoteFileView
    from tests.unit.test_need_units import (
        MemoryCase,
        bound_facts,
        confirm,
        unit_service,
    )
    from tests.unit.test_quotation_contracts import (
        detail_fixture,
        frozen_fixture,
        quote_fx_case,
    )
    from tests.unit.test_quote_context_contracts import issuer
    from tests.unit.test_quote_pdf_artifacts import file_values
    from tests.unit.test_quote_preparation_read import ACTOR, preparation_case
    from tests.unit.test_quote_preparation_read import TENANT as FACT_TENANT

    app, _ = _app("boss")
    f, detail = frozen_fixture(), detail_fixture()
    calls = []
    t2 = Port(
        calls,
        {
            "get_policy": f.policy,
            "confirm_policy": f.policy,
            "get_quote_fx": QuoteFxView.model_validate_json(
                quote_fx_case()[1].quote_fx.model_dump_json()
            ),
            "confirm_price": f.price_evidence[0],
            "list_price_evidence": f.price_evidence,
            "get_coverage": f.coverage,
            "confirm_coverage": f.coverage.content_hash,
        },
    )
    t2.values["confirm_quote_fx"] = t2.values["get_quote_fx"]
    quotes = Port(
        calls,
        {
            "get_issuer": None,
            "confirm_issuer": issuer(),
            "get": detail,
            "list_versions": (detail,),
        },
    )
    units = Port(
        calls,
        {
            "get_facts": bound_facts(),
            "confirm": await confirm(unit_service(MemoryCase())),
        },
    )
    units.values["get_confirmation"] = units.values["confirm"]
    freeze = Port(
        calls,
        {"get_scope": f.scope_confirmation, "list_scopes": (f.scope_confirmation,)},
    )
    preparations = Port(
        calls, {"calculate": f.calculation, "confirm_scope": f.scope_confirmation}
    )
    pr, _, _ = preparation_case()
    reads = Port(calls, {"get": await pr.get(FACT_TENANT, "opp_test", actor_id=ACTOR)})
    file = QuoteFileView(**file_values())
    files = Port(calls, {"list_files": (file,)})
    fileapp = Port(
        calls,
        {
            "generate": file,
            "download": (file, b"%PDF-controlled-wire"),
            "read_history": (file, b"%PDF-controlled-wire"),
        },
    )
    composition = SimpleNamespace(
        domain=SimpleNamespace(
            costing_quotes=t2,
            quotations=quotes,
            need_units=units,
            costing_freeze=freeze,
            preparation=preparations,
            preparation_reads=reads,
            creation=Port(calls, {"create": detail}),
            files=files,
        ),
        files_application=fileapp,
        customer_versions=Port(calls, {}),
        approval_starter=Port(calls, {}),
        evidence=SimpleNamespace(preview_reader=Port(calls, {})),
    )
    object.__setattr__(app.state.dependencies, "quotation", composition)
    object.__setattr__(
        app.state.dependencies,
        "costing",
        Port(calls, {"get_sheet": SimpleNamespace(opportunity_id="opp_test")}),
    )
    return SimpleNamespace(
        app=app,
        composition=composition,
        calls=calls,
        frozen=f,
        detail=detail,
        file=file,
    )


ROUTES = {
    "/policies": {"get", "post"},
    "/issuer": {"get", "post"},
    "/quote-fx": {"post"},
    "/quote-fx/{fx_id}": {"get"},
    "/price-evidence": {"post"},
    "/opportunities/{opportunity_id}/price-evidence": {"get"},
    "/cost-sheets/{sheet_id}/coverage": {"get", "post"},
    "/cost-sheets/{sheet_id}/scope-confirmations": {"get", "post"},
    "/cost-sheets/{sheet_id}/scope-confirmations/{confirmation_id}": {"get"},
    "/cost-sheets/{sheet_id}/calculate": {"post"},
    "/opportunities/{opportunity_id}/quote-context": {"get"},
    "/needs/{need_id}/unit": {"get"},
    "/needs/{need_id}/unit-confirmations": {"post"},
    "/needs/{need_id}/unit-confirmations/{confirmation_id}": {"get"},
    "/opportunities/{opportunity_id}/quotes": {"get", "post"},
    "/quotes/{quote_id}": {"get"},
    "/quotes/{quote_id}/revisions": {"post"},
    "/quotes/{quote_id}/submit": {"post"},
    "/opportunities/{opportunity_id}/customer-quote-versions": {"get"},
    "/quotes/{quote_id}/files": {"get", "post"},
    "/quotes/{quote_id}/files/{file_id}": {"get"},
    "/quotes/{quote_id}/files/{file_id}/history": {"get"},
    "/quotes/{quote_id}/files/reconcile": {"post"},
    "/evidence/preview": {"post"},
    "/evidence/locator": {"post"},
}


async def request(app, method, path, *, body=None, key=None, headers=None):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        return await client.request(
            method,
            "/costing-quotes" + path,
            json=body,
            headers={
                "X-Tenant-Id": str(TENANT),
                "X-Employee-Id": str(EMPLOYEE),
                **({"Idempotency-Key": key} if key is not None else {}),
                **(headers or {}),
            },
        )


@pytest.mark.parametrize(
    "path",
    [
        "/policies",
        "/issuer",
        "/quote-fx/fx_test",
        "/opportunities/opp_test/price-evidence",
        "/cost-sheets/cost_test/coverage",
        "/cost-sheets/cost_test/scope-confirmations",
        "/cost-sheets/cost_test/scope-confirmations/scp_test",
        "/opportunities/opp_test/quote-context",
        "/needs/need_test/unit",
        "/needs/need_test/unit-confirmations/nuc_test",
        "/opportunities/opp_test/quotes",
        "/quotes/quo_test",
        "/opportunities/opp_test/customer-quote-versions?limit=5",
        "/quotes/quo_test/files",
        "/quotes/quo_test/files/qfl_test",
        "/quotes/quo_test/files/qfl_test/history",
    ],
)
async def test_unconfigured_quotation_read_is_dependency_failure_not_missing_route_or_empty(
    path,
):
    app, _ = _app("boss")
    response = await request(app, "GET", path)
    assert response.status_code == 503
    assert response.json() == {
        "code": "dependency_unavailable",
        "message": "报价依赖不可用",
    }
    assert "Retry-After" not in response.headers


def test_new_quotation_openapi_declares_every_route_without_dropping_old_costing():
    app, _ = _app()
    paths = app.openapi()["paths"]
    for path, methods in ROUTES.items():
        assert "/costing-quotes" + path in paths, f"缺少安全HTTP路径 {path}"
        assert methods <= paths["/costing-quotes" + path].keys()
    assert "/costing-quotes/cost-sheets/{cost_sheet_id}/readiness" in paths
    assert not any(
        "/approve" in path or "/apply" in path
        for path in paths
        if path.startswith("/costing-quotes")
    )


@pytest.mark.parametrize(
    "path,method",
    [
        ("/policies", "get_policy"),
        ("/issuer", "get_issuer"),
        ("/quote-fx/fx_test", "get_quote_fx"),
        ("/opportunities/opp_test/price-evidence", "list_price_evidence"),
        ("/cost-sheets/cost_test/coverage", "get_coverage"),
        ("/cost-sheets/cost_test/scope-confirmations", "list_scopes"),
        ("/opportunities/opp_test/quote-context", "get"),
        ("/needs/need_test/unit", "get_facts"),
        ("/needs/need_test/unit-confirmations/nuc_first", "get_confirmation"),
        ("/opportunities/opp_test/quotes", "list_versions"),
        ("/quotes/quo_test", "get"),
    ],
)
async def test_safe_reads_bind_trusted_identity_and_do_not_serialize_source_or_basis(
    wire_case, path, method
):
    c = wire_case
    response = await request(c.app, "GET", path)
    assert response.status_code == 200, response.text
    assert c.calls[-1][0] == method
    assert c.calls[-1][1][0] == TENANT
    identity = c.calls[-1][2]
    if "actor" in identity:
        actor = identity["actor"]
        assert (
            getattr(actor, "actor_id", getattr(actor, "employee_id", None)) == EMPLOYEE
        )
    else:
        assert identity["actor_id"] == EMPLOYEE
    assert (
        '"source_quote":' not in response.text and '"source_url":' not in response.text
    )
    assert '"need_facts":' not in response.text and '"runtime":' not in response.text
    if "quotes" in path:
        assert '"basis":' not in response.text


@pytest.mark.parametrize(
    "command_name,path,method",
    [
        ("policy", "/policies", "confirm_policy"),
        ("issuer", "/issuer", "confirm_issuer"),
        ("fx", "/quote-fx", "confirm_quote_fx"),
        ("price", "/price-evidence", "confirm_price"),
        ("coverage", "/cost-sheets/cost_test/coverage", "confirm_coverage"),
        ("scope", "/cost-sheets/cost_test/scope-confirmations", "confirm_scope"),
        ("unit", "/needs/need_test/unit-confirmations", "confirm"),
        ("quote", "/opportunities/opp_test/quotes", "create"),
        ("revision", "/quotes/quo_previous/revisions", "create"),
    ],
)
async def test_confirm_commands_keep_json_wire_key_and_server_identity(
    wire_case, command_name, path, method
):
    from domains.costing.freeze_schemas import CostScopeConfirmationCommand
    from domains.costing.schemas import (
        CostCoverageCreate,
        PricingPolicyCreate,
        QuoteFxCreate,
        SupplierPriceEvidenceCreate,
    )
    from domains.quotations.schemas import QuoteIssuerCreate
    from tests.unit.test_need_units import command
    from tests.unit.test_quotation_contracts import draft

    c = wire_case
    f = c.frozen
    if command_name == "coverage":
        path = f"/cost-sheets/{f.coverage.cost_sheet_id}/coverage"
    types = {
        "policy": (PricingPolicyCreate, f.policy),
        "price": (SupplierPriceEvidenceCreate, f.price_evidence[0]),
        "fx": (
            QuoteFxCreate,
            c.composition.domain.costing_quotes.values["get_quote_fx"],
        ),
        "coverage": (CostCoverageCreate, f.coverage),
    }
    if command_name in types:
        model, value = types[command_name]
        body = model(
            **{name: getattr(value, name) for name in model.model_fields}
        ).model_dump(mode="json")
    elif command_name == "scope":
        scope = f.scope_confirmation
        body = CostScopeConfirmationCommand(
            coverage_id=scope.coverage_id,
            expected_sheet_hash=scope.sheet_hash,
            expected_coverage_hash=scope.coverage_hash,
            expected_need_facts_hash=scope.need_facts_hash,
            terms=scope.terms,
            valid_until=scope.valid_until,
            evidence_bindings=scope.evidence_bindings,
        ).model_dump(mode="json")
    elif command_name == "issuer":
        body = QuoteIssuerCreate(
            name="Company", address="Address", contact="Contact"
        ).model_dump(mode="json")
    elif command_name == "unit":
        body = command().model_dump(mode="json")
    else:
        body = draft(
            **(
                {"replaces_quote_id": "quo_previous", "expected_quote_version": 1}
                if command_name == "revision"
                else {}
            )
        ).model_dump(mode="json")
    response = await request(c.app, "POST", path, body=body, key="user-original-key")
    assert response.status_code == 200, response.text
    called = next(call for call in c.calls if call[0] == method)
    assert (
        called[1][0] == TENANT and called[2]["idempotency_key"] == "user-original-key"
    )
    assert '"source_quote":' not in response.text
    if command_name == "coverage":
        assert c.calls[-1][0] == "get_coverage"
        assert c.calls[-1][2]["content_hash"] == f.coverage.content_hash
    c.calls.clear()
    missing = await request(c.app, "POST", path, body=body)
    assert missing.status_code == 400 and c.calls == []


async def test_calculation_reuses_sheet_opportunity_and_never_embeds_client_fx(
    wire_case,
):
    c = wire_case
    body = {
        "mode": "manual",
        "unit_price": {"amount": "3.10", "currency": "USD"},
        "rounding": {"unit_places": 2, "total_places": 2, "strategy": "ROUND_HALF_UP"},
        "quote_fx_ref": "fx_confirmed",
        "algorithm_version": "costing-v1",
    }
    response = await request(
        c.app, "POST", "/cost-sheets/cost_test/calculate", body=body
    )
    assert response.status_code == 200, response.text
    called = c.calls[-1]
    assert called[0] == "calculate" and called[1][1] == "opp_test"
    assert called[1][3].quote_fx is None
    assert called[1][3].unit_price.amount == Decimal("3.10")
    assert called[2]["quote_fx_ref"] == "fx_confirmed"
    for changes in (
        {"quote_fx": {}},
        {"unit_price": {"amount": 3.1, "currency": "USD"}},
        {"actor": "boss"},
    ):
        c.calls.clear()
        response = await request(
            c.app, "POST", "/cost-sheets/cost_test/calculate", body={**body, **changes}
        )
        assert response.status_code == 400 and not c.calls


@pytest.mark.parametrize(
    "action",
    ["generate", "download", "history", "list", "submit", "versions", "reconcile"],
)
async def test_file_routes_and_submit_use_technical_apps_without_internal_cost_role_gate(
    wire_case, action
):
    from tests.unit.test_quote_pdf_artifacts import NOW, ULID

    c = wire_case
    quote, file = c.file.quote_id, c.file.file_id
    # 文件与版本页的actor权限由真实服务再检查；HTTP不能先套四成本角色。
    from tests.unit.test_costing_router import _EmployeeScope

    object.__setattr__(
        c.app.state.dependencies,
        "employees",
        _EmployeeScope("sales" if action != "submit" else "boss"),
    )
    c.composition.approval_starter.values["start"] = {
        "quote_id": quote,
        "run_id": f"run_{ULID}",
    }
    from domains.quotations.schemas import QuoteCustomerVersionPage

    c.composition.customer_versions.values["list_versions"] = QuoteCustomerVersionPage(
        items=(), next_before_version=None
    )
    c.composition.files_application.values["reconcile"] = {
        "outcome": "metadata_recovered_original_unresolved",
        "file": c.file,
        "original_generation_call_id": f"tcl_{ULID}",
        "recovery_call_id": f"tcl_{ULID}",
        "original_status_at_check": "executing",
        "original_ledger_modified": False,
        "checked_at": NOW,
    }
    paths = {
        "generate": ("POST", f"/quotes/{quote}/files", {}),
        "download": ("GET", f"/quotes/{quote}/files/{file}", None),
        "history": ("GET", f"/quotes/{quote}/files/{file}/history", None),
        "list": ("GET", f"/quotes/{quote}/files", None),
        "submit": ("POST", f"/quotes/{quote}/submit", {}),
        "versions": (
            "GET",
            "/opportunities/opp_test/customer-quote-versions?limit=3",
            None,
        ),
        "reconcile": (
            "POST",
            f"/quotes/{quote}/files/reconcile",
            {"quote_id": quote, "original_generation_call_id": f"tcl_{ULID}"},
        ),
    }
    method, path, body = paths[action]
    response = await request(c.app, method, path, body=body)
    assert response.status_code == (202 if action == "submit" else 200), response.text
    assert c.calls[-1][2]["actor_id"] == EMPLOYEE
    if action in {"download", "history"}:
        assert response.content == b"%PDF-controlled-wire"
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in response.headers["content-disposition"]
        assert str(quote) in response.headers["content-disposition"]
        assert "Buyer" not in str(response.headers)


@pytest.mark.parametrize("missing", ["files", "files_application", "customer_versions"])
@pytest.mark.parametrize(
    "action", ["list", "generate", "reconcile", "download", "history", "versions"]
)
async def test_file_group_missing_any_port_closes_all_six_routes(
    wire_case, missing, action
):
    from tests.unit.test_quote_pdf_artifacts import ULID

    c = wire_case
    quote, file = c.file.quote_id, c.file.file_id
    if missing == "files":
        c.composition.domain.files = None
    else:
        setattr(c.composition, missing, None)
    paths = {
        "list": ("GET", f"/quotes/{quote}/files", None),
        "generate": ("POST", f"/quotes/{quote}/files", {}),
        "reconcile": (
            "POST",
            f"/quotes/{quote}/files/reconcile",
            {
                "quote_id": quote,
                "original_generation_call_id": f"tcl_{ULID}",
            },
        ),
        "download": ("GET", f"/quotes/{quote}/files/{file}", None),
        "history": ("GET", f"/quotes/{quote}/files/{file}/history", None),
        "versions": (
            "GET",
            "/opportunities/opp_test/customer-quote-versions?limit=3",
            None,
        ),
    }
    method, path, body = paths[action]
    response = await request(c.app, method, path, body=body)
    assert response.status_code == 503, response.text
    assert c.calls == []
    core = await request(c.app, "GET", "/issuer")
    assert core.status_code == 200


@pytest.mark.parametrize("path", ["/quotes/quo_test/submit", "/quotes/quo_test/files"])
@pytest.mark.parametrize(
    "body",
    [
        {"force": True},
        {"approved": True},
        {"actor_id": "boss"},
        {"history": True},
        {"template": "x"},
        {"key": "x"},
    ],
)
async def test_empty_commands_reject_client_control_fields_before_service(
    wire_case, path, body
):
    c = wire_case
    response = await request(c.app, "POST", path, body=body)
    assert response.status_code == 400 and not c.calls


@pytest.mark.parametrize(
    "query", ["actor=emp_other", "history=true", "template=x", "key=x", "force=true"]
)
async def test_file_routes_reject_unknown_query_before_object_read(wire_case, query):
    response = await request(
        wire_case.app, "GET", f"/quotes/quo_test/files/qfl_test?{query}"
    )
    assert response.status_code == 400 and not wire_case.calls


@pytest.mark.parametrize(
    "role,expected",
    [
        ("boss", 200),
        ("product", 200),
        ("sourcing", 200),
        ("finance", 200),
        ("sales", 403),
        ("manager", 403),
        ("viewer", 403),
    ],
)
async def test_internal_preparation_keeps_original_cost_roles(
    wire_case, role, expected
):
    from contextlib import asynccontextmanager

    from tests.unit.test_costing_router import _EmployeeService

    class EmployeeService(_EmployeeService):
        async def list_active(self, tenant_id, *, actor):
            return []

    @asynccontextmanager
    async def employees(tenant_id):
        yield EmployeeService(role)

    object.__setattr__(wire_case.app.state.dependencies, "employees", employees)
    response = await request(wire_case.app, "GET", "/policies")
    assert response.status_code == expected
    if expected == 403:
        assert not wire_case.calls


@pytest.mark.parametrize(
    "code,status,call",
    [
        ("invalid_input", 400, False),
        ("template_unsupported", 400, False),
        ("invalid_input", 409, True),
        ("template_unsupported", 409, True),
        ("permission_denied", 403, True),
        ("not_found", 404, True),
        ("rate_limited", 429, True),
        ("storage_inconsistent", 503, True),
    ],
)
async def test_file_errors_preserve_only_real_call_fields_and_legal_retry(
    wire_case, code, status, call
):
    from tests.unit.test_quote_pdf_artifacts import ULID
    from tool_gateway.handlers.quote_files import FILE_FAILURE_MESSAGES
    from workflows.quote_approval.file_schemas import (
        QuoteFileApiError,
        QuoteFileApplicationError,
    )

    detail = QuoteFileApiError(
        code=code,
        message=FILE_FAILURE_MESSAGES[code],
        tool_call_id=f"tcl_{ULID}" if call else None,
        original_generation_call_id=None,
        retry_after_seconds=12 if code == "rate_limited" else None,
    )
    wire_case.composition.files_application.failure = QuoteFileApplicationError(detail)
    response = await request(
        wire_case.app, "POST", f"/quotes/{wire_case.file.quote_id}/files", body={}
    )
    assert response.status_code == status, response.text
    if call:
        assert response.json() == detail.model_dump(mode="json")
    else:
        assert set(response.json()) == {"code", "message"}
    assert response.headers.get("retry-after") == (
        "12" if code == "rate_limited" else None
    )


def test_file_openapi_keeps_error_union_pdf_and_no_internal_success_payloads():
    app, _ = _app()
    schema = app.openapi()
    file_path = "/costing-quotes/quotes/{quote_id}/files"
    assert file_path in schema["paths"], "缺少文件HTTP契约"
    responses = schema["paths"][file_path]["post"]["responses"]
    assert responses["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ApiErrorResponse"
    }
    for status in ("403", "404", "409", "429", "503"):
        union = responses[status]["content"]["application/json"]["schema"]
        assert {ref["$ref"] for ref in union["anyOf"]} == {
            "#/components/schemas/ApiErrorResponse",
            "#/components/schemas/QuoteFileApiError",
        }
    pdf = schema["paths"][file_path + "/{file_id}"]["get"]["responses"]["200"][
        "content"
    ]
    assert "application/pdf" in pdf and "application/json" not in pdf
    internal = schema["components"]["schemas"]["QuoteInternalPublicView"]["properties"]
    assert not {"basis", "intent", "need_facts", "runtime"} & internal.keys()


@pytest.mark.parametrize("operation", ["preview", "locate"])
async def test_evidence_http_projects_only_authorized_text_slot(wire_case, operation):
    import hashlib

    from shared.schemas.evidence_read import PricingEvidenceScope
    from tests.unit.test_quote_pdf_artifacts import ULID

    scope = PricingEvidenceScope(purpose="pricing")
    text = "Controlled supplier quotation."
    digest = hashlib.sha256(text.encode()).hexdigest()
    body = {
        "operation": operation,
        "source_ref": f"upload:upl_{ULID}",
        "scope": {"purpose": "pricing"},
        "profile": "pdf-text-v1",
        "page": 1,
    }
    selection = None
    if operation == "locate":
        body.update(
            start=0,
            end=len(text),
            expected_raw_hash="a" * 64,
            expected_text_hash=digest,
        )
        selection = SimpleNamespace(start=0, end=len(text), excerpt_hash=digest)
    result = SimpleNamespace(
        reference=SimpleNamespace(
            source_ref=body["source_ref"],
            scope=scope,
            raw=SimpleNamespace(artifact_id=f"art_{ULID}", content_hash="a" * 64),
        ),
        profile="pdf-text-v1",
        page=1,
        text=text,
        text_hash=digest,
        selection=selection,
        excerpt=text if selection else None,
        locator="controlled-canonical-locator" if selection else None,
    )
    wire_case.composition.evidence.preview_reader.values["read"] = result
    response = await request(
        wire_case.app,
        "POST",
        "/evidence/" + ("locator" if operation == "locate" else "preview"),
        body=body,
    )
    assert response.status_code == 200, response.text
    expected = {
        "source_ref",
        "scope",
        "artifact_id",
        "raw_hash",
        "profile",
        "page",
        "text",
        "text_hash",
    }
    if operation == "locate":
        expected |= {"start", "end", "excerpt_hash", "excerpt", "locator"}
    assert set(response.json()) == expected
    assert response.json()["text"] == text
    assert wire_case.calls[-1][2]["actor_id"] == EMPLOYEE


@pytest.mark.parametrize(
    "kind,status,code",
    [
        ("missing", 404, "record_not_found"),
        ("corrupt", 503, "facts_corrupt"),
        ("conflict", 409, "idempotency_conflict"),
        ("permission", 403, "permission_denied"),
        ("storage", 503, "dependency_unavailable"),
    ],
)
async def test_new_safe_errors_are_fixed_and_never_disclose_exception_text(
    wire_case, kind, status, code
):
    from domains.costing.service import CostingQuoteNotFoundError
    from domains.quotations.errors import QuoteContextError
    from shared.errors import IdempotencyConflict, PermissionDenied

    failures = {
        "missing": CostingQuoteNotFoundError(),
        "corrupt": QuoteContextError("facts_corrupt"),
        "conflict": IdempotencyConflict("private source marker"),
        "permission": PermissionDenied("private source marker"),
        "storage": RuntimeError("private source marker"),
    }
    wire_case.composition.domain.costing_quotes.failure = failures[kind]
    response = await request(wire_case.app, "GET", "/policies")
    assert response.status_code == status and response.json()["code"] == code
    assert "private" not in response.text
    assert set(response.json()) == {"code", "message"}


async def test_missing_authenticated_employee_stays_401_not_dependency_failure(
    wire_case,
):
    async with AsyncClient(
        transport=ASGITransport(app=wire_case.app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/costing-quotes/issuer", headers={"X-Tenant-Id": str(TENANT)}
        )
    assert response.status_code == 401
    assert not wire_case.calls


@pytest.mark.parametrize(
    "path",
    [
        "/quotes/quo_test/files",
        "/quotes/quo_test/files/qfl_test",
        "/quotes/quo_test/files/qfl_test/history",
    ],
)
async def test_file_get_rejects_hidden_body_before_any_port(wire_case, path):
    response = await request(
        wire_case.app, "GET", path, body={"actor_id": "other", "history": True}
    )
    assert response.status_code == 400 and not wire_case.calls


@pytest.mark.parametrize("field", ["quote", "file"])
async def test_file_resource_identity_uses_canonical_public_dto_before_invoke(
    wire_case, field
):
    quote = "quo_test" if field == "quote" else wire_case.file.quote_id
    file = "qfl_test" if field == "file" else wire_case.file.file_id
    response = await request(wire_case.app, "GET", f"/quotes/{quote}/files/{file}")
    assert response.status_code == 400 and not wire_case.calls


@pytest.mark.parametrize("path", ["/quotes/quo_test/submit", "/quotes/quo_test/files"])
async def test_empty_command_rejects_explicit_json_null(wire_case, path):
    async with AsyncClient(
        transport=ASGITransport(app=wire_case.app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/costing-quotes" + path,
            content="null",
            headers={
                "X-Tenant-Id": str(TENANT),
                "X-Employee-Id": str(EMPLOYEE),
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 400 and not wire_case.calls


async def test_scope_read_rejects_different_real_sheet(wire_case):
    response = await request(
        wire_case.app, "GET", "/cost-sheets/cost_wrong/scope-confirmations/scp_test"
    )
    assert response.status_code == 404
    assert [entry[0] for entry in wire_case.calls] == ["get_scope"]


@pytest.mark.parametrize("change", ["opportunity", "replacement", "version"])
async def test_create_and_revision_path_binding_rejects_before_creation(
    wire_case, change
):
    from tests.unit.test_quotation_contracts import draft

    body = draft().model_dump(mode="json")
    path = (
        "/opportunities/opp_wrong/quotes"
        if change == "opportunity"
        else "/quotes/quo_previous/revisions"
    )
    if change == "version":
        body["replaces_quote_id"] = "quo_previous"
    response = await request(wire_case.app, "POST", path, body=body, key="binding-key")
    assert response.status_code == 400 and not wire_case.calls


def test_openapi_exposes_required_original_idempotency_header_only_on_confirmations():
    app, _ = _app()
    paths = app.openapi()["paths"]
    for path in [
        "/policies",
        "/issuer",
        "/quote-fx",
        "/price-evidence",
        "/cost-sheets/{sheet_id}/coverage",
        "/cost-sheets/{sheet_id}/scope-confirmations",
        "/needs/{need_id}/unit-confirmations",
        "/opportunities/{opportunity_id}/quotes",
        "/quotes/{quote_id}/revisions",
    ]:
        headers = [
            value
            for value in paths["/costing-quotes" + path]["post"].get("parameters", [])
            if value["in"] == "header" and value["name"] == "Idempotency-Key"
        ]
        assert len(headers) == 1 and headers[0]["required"] is True
    for path in [
        "/quotes/{quote_id}/submit",
        "/quotes/{quote_id}/files",
        "/cost-sheets/{sheet_id}/calculate",
        "/evidence/preview",
        "/evidence/locator",
        "/quotes/{quote_id}/files/reconcile",
    ]:
        assert not any(
            value["name"] == "Idempotency-Key"
            for value in paths["/costing-quotes" + path]["post"].get("parameters", [])
        )


@pytest.mark.parametrize("path", ["/policies", "/quote-fx/fx_missing"])
async def test_new_costing_missing_error_keeps_fixed_chinese_message(wire_case, path):
    from domains.costing.service import CostingQuoteNotFoundError

    wire_case.composition.domain.costing_quotes.failure = CostingQuoteNotFoundError()
    response = await request(wire_case.app, "GET", path)
    assert response.json() == {
        "code": "record_not_found",
        "message": "成本报价记录不存在",
    }


async def test_changed_cost_coverage_is_not_reported_as_a_reused_key(wire_case):
    from domains.costing.errors import CostCoverageConflict

    wire_case.composition.domain.costing_quotes.failure = CostCoverageConflict(
        "private sheet marker"
    )
    response = await request(wire_case.app, "GET", "/policies")
    assert response.status_code == 409 and response.json()["code"] == "coverage_stale"
    assert "private" not in response.text


@pytest.mark.parametrize("code", ["policy_missing", "fx_missing"])
async def test_calculation_missing_prerequisite_remains_business_conflict(
    wire_case, code
):
    from domains.costing.errors import CostFreezeError

    wire_case.composition.domain.preparation.failure = CostFreezeError(code)
    response = await request(
        wire_case.app,
        "POST",
        "/cost-sheets/cost_test/calculate",
        body={
            "mode": "manual",
            "unit_price": {"amount": "3.10", "currency": "USD"},
            "rounding": {
                "unit_places": 2,
                "total_places": 2,
                "strategy": "ROUND_HALF_UP",
            },
            "quote_fx_ref": None,
            "algorithm_version": "costing-v1",
        },
    )
    assert response.status_code == 409 and response.json()["code"] == code
