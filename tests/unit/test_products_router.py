"""Product & Supply Center HTTP 的安全列表和视图隔离。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.approvals.schemas import CatalogApprovalLinkState
from domains.approvals.service import ApprovalState
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.errors import ProductNotFoundError
from domains.products.schemas import (
    CatalogBlockedFactsInput,
    CatalogCultivationCaseView,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    CatalogProposalRuleResult,
)
from shared.errors import TransientError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    RunId,
    TenantId,
)

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
OTHER_TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0Y")
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
POLICY_ID = CatalogProposalPolicyVersionId("cpv_01K39P9M5D6K4A91YEQ80EJZ0X")
EVALUATION_ID = CatalogProposalEvaluationId("cpe_01K39P9M5D6K4A91YEQ80EJZ0X")
PROPOSAL_ID = CatalogProductProposalId("cpr_01K39P9M5D6K4A91YEQ80EJZ0X")
CASE_ID = CatalogCultivationCaseId("ccc_01K39P9M5D6K4A91YEQ80EJZ0X")
OTHER_EVALUATION_ID = CatalogProposalEvaluationId(
    "cpe_01K39P9M5D6K4A91YEQ80EJZ0Y"
)
OTHER_PROPOSAL_ID = CatalogProductProposalId("cpr_01K39P9M5D6K4A91YEQ80EJZ0Y")
OTHER_CASE_ID = CatalogCultivationCaseId("ccc_01K39P9M5D6K4A91YEQ80EJZ0Y")
APPROVAL_ID = ApprovalId("apr_01K39P9M5D6K4A91YEQ80EJZ0X")
POLICY_APPROVAL_ID = ApprovalId("apr_01K39P9M5D6K4A91YEQ80EJZ0Y")
OTHER_APPROVAL_ID = ApprovalId("apr_01K39P9M5D6K4A91YEQ80EJZ0Z")
CLUSTER_ID = NeedClusterId("ncl_01K39P9M5D6K4A91YEQ80EJZ0X")
RUN_ID = RunId("run_01K39P9M5D6K4A91YEQ80EJZ0X")
HASH = "a" * 64


class _Products:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_supply_cards(self, tenant_id, *, actor, source_only, limit):
        self.calls.append((tenant_id, actor, source_only, limit))
        return []

    async def get_internal_view(self, tenant_id, product_id, *, actor):
        raise ProductNotFoundError("产品不存在或租户不匹配")

    async def get_sales_view(self, tenant_id, product_id, *, actor):
        raise ProductNotFoundError("产品不存在或租户不匹配")

    async def get_customer_view(self, tenant_id, product_id, *, actor):
        raise ProductNotFoundError("产品不存在或租户不匹配")


def _identity(role: str) -> RequestIdentity:
    employee = EmployeeView(
        employee_id=EMPLOYEE,
        tenant_id=TENANT,
        name="测试员工",
        role=role,
    )
    return RequestIdentity(
        tenant_id=TENANT,
        employee=employee,
        employee_actor=EmployeeActor(
            str(EMPLOYEE),
            EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF,
            role,
        ),
        opportunity_actor=OpportunityActor(str(EMPLOYEE), OpportunityScope(), role),
    )


def _app(role: str) -> tuple[object, _Products]:
    products = _Products()
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        products=products,
    )
    return app, products


def _get(app: object, path: str) -> Response:
    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.get(
                path,
                headers={
                    "X-Employee-Id": str(EMPLOYEE),
                    "X-Tenant-Id": str(TENANT),
                },
            )

    return asyncio.run(run())


def _request(
    app: object,
    method: str,
    path: str,
    *,
    json: object | None = None,
    headers: list[tuple[str, str]] | None = None,
) -> Response:
    async def run() -> Response:
        request_headers = [
            ("X-Employee-Id", str(EMPLOYEE)),
            ("X-Tenant-Id", str(TENANT)),
            *(headers or []),
        ]
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.request(
                method, path, json=json, headers=request_headers
            )

    return asyncio.run(run())


def _policy(*, approval_id: ApprovalId | None = POLICY_APPROVAL_ID):
    content = CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    return CatalogProposalPolicyView(
        policy_version_id=POLICY_ID,
        content=content,
        content_hash=catalog_policy_content_hash(content),
        base_active_version_id=None,
        proposed_by=EMPLOYEE,
        approval_id=approval_id,
        state="pending_approval",
        created_at=NOW,
        activated_at=None,
        terminal_at=None,
    )


def _evaluation():
    facts = CatalogBlockedFactsInput(
        tenant_id=TENANT,
        cluster_id=CLUSTER_ID,
        facts_hash=HASH,
    )
    required = {
        "membership_integrity": True,
        "distinct_accounts": 3,
        "recurring_accounts": None,
        "distinct_countries": None,
        "quantity_unit_coverage": None,
        "unified_unit": None,
    }
    rules = tuple(
        CatalogProposalRuleResult(
            rule=rule,
            status="unknown",
            actual_value=None,
            required_value=required[rule],
            explanation_code="目录事实损坏，评估已阻断",
        )
        for rule in (
            "membership_integrity",
            "distinct_accounts",
            "recurring_accounts",
            "distinct_countries",
            "quantity_unit_coverage",
            "unified_unit",
        )
    )
    return CatalogProposalEvaluationView(
        evaluation_id=EVALUATION_ID,
        cluster_id=CLUSTER_ID,
        policy_version_id=POLICY_ID,
        facts_hash=HASH,
        facts=facts,
        rule_results=rules,
        overall_passed=False,
        blocked_reason="catalog_facts_invalid",
        proposed_by_run=RUN_ID,
        created_at=NOW,
    )


def _proposal():
    return CatalogProductProposalView(
        proposal_id=PROPOSAL_ID,
        evaluation_id=EVALUATION_ID,
        cluster_id=CLUSTER_ID,
        policy_version_id=POLICY_ID,
        facts_hash=HASH,
        owner_employee=EMPLOYEE,
        proposed_by_run=RUN_ID,
        approval_id=APPROVAL_ID,
        state="pending_review",
        created_at=NOW,
        updated_at=NOW,
    )


def _cultivation():
    return CatalogCultivationCaseView(
        cultivation_case_id=CASE_ID,
        proposal_id=PROPOSAL_ID,
        approval_id=APPROVAL_ID,
        cluster_id=CLUSTER_ID,
        policy_version_id=POLICY_ID,
        facts_hash=HASH,
        evidence_refs=("evidence:1",),
        queued_at=NOW,
    )


class _CatalogProducts:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.failure: Exception | None = None
        self.active_policy = _policy()
        self.policies = (_policy(),)
        self.evaluation = _evaluation()
        self.evaluations = (_evaluation(),)
        self.proposal = _proposal()
        self.proposals = (_proposal(),)
        self.case = _cultivation()
        self.cases = (_cultivation(),)

    async def _value(self, name, value, tenant_id, actor, limit=None):
        self.calls.append((name, tenant_id, actor, limit))
        if self.failure is not None:
            raise self.failure
        return value

    async def get_active_policy(self, tenant_id, *, actor):
        return await self._value("active", self.active_policy, tenant_id, actor)

    async def list_policy_versions(self, tenant_id, *, actor, limit):
        return await self._value("policies", self.policies, tenant_id, actor, limit)

    async def get_policy_change_snapshot(self, tenant_id, policy_id, *, actor):
        assert policy_id == POLICY_ID
        from domains.products.schemas import CatalogPolicyChangeSnapshot

        policy = await self._value("policy", _policy(), tenant_id, actor)
        return CatalogPolicyChangeSnapshot(
            base=None, current=None, candidate=policy, base_is_current=True
        )

    async def list_evaluations(self, tenant_id, *, actor, limit):
        return await self._value("evaluations", self.evaluations, tenant_id, actor, limit)

    async def get_evaluation(self, tenant_id, evaluation_id, *, actor):
        assert evaluation_id == EVALUATION_ID
        return await self._value("evaluation", self.evaluation, tenant_id, actor)

    async def list_proposals(self, tenant_id, *, actor, limit):
        return await self._value("proposals", self.proposals, tenant_id, actor, limit)

    async def get_proposal(self, tenant_id, proposal_id, *, actor):
        assert proposal_id == PROPOSAL_ID
        return await self._value("proposal", self.proposal, tenant_id, actor)

    async def list_cultivation_cases(self, tenant_id, *, actor, limit):
        return await self._value("cases", self.cases, tenant_id, actor, limit)

    async def get_cultivation_case(self, tenant_id, case_id, *, actor):
        assert case_id == CASE_ID
        return await self._value("case", self.case, tenant_id, actor)


class _CatalogApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.candidate = _policy()

    async def submit_policy_candidate(
        self, tenant_id, content, *, idempotency_key, actor
    ):
        self.calls.append((tenant_id, content, idempotency_key, actor))
        return self.candidate


class _Approvals:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.failure: Exception | None = None
        self.approval_type: str | None = None
        self.returned_approval_id: ApprovalId | None = None

    async def get_catalog_link_state_for_reader(
        self, tenant_id, approval_id, *, reader
    ):
        self.calls.append((tenant_id, approval_id, reader))
        if self.failure is not None:
            raise self.failure
        return CatalogApprovalLinkState(
            approval_id=self.returned_approval_id or approval_id,
            approval_type=self.approval_type
            or (
                "catalog_proposal_policy_change"
                if approval_id == POLICY_APPROVAL_ID
                else "catalog_product_cultivation"
            ),
            state=ApprovalState.PENDING,
        )


def _catalog_app(role: str):
    catalog = _CatalogProducts()
    application = _CatalogApplication()
    approvals = _Approvals()
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        catalog_products=catalog,
        catalog_product_application=application,
        approvals=approvals,
    )
    return app, catalog, application, approvals


def test_product_supply_list_is_internal_and_preserves_source_only_filter() -> None:
    allowed_app, products = _app("product")
    denied_app, denied_products = _app("sales")

    allowed = _get(allowed_app, "/products?source_only=true&limit=20")
    denied = _get(denied_app, "/products?source_only=true&limit=20")

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert products.calls[0][2:] == (True, 20)
    assert denied_products.calls == []


def test_product_views_hide_missing_or_other_tenant_product_as_not_found() -> None:
    app, _ = _app("boss")
    product_id = "prd_01K39P9M5D6K4A91YEQ80EJZ0X"

    responses = [
        _get(app, f"/products/{product_id}/internal"),
        _get(app, f"/products/{product_id}/sales"),
        _get(app, f"/products/{product_id}/customer"),
    ]

    assert [response.status_code for response in responses] == [404, 404, 404]


def test_catalog_openapi_exposes_only_the_nine_approved_operations() -> None:
    app, _, _, _ = _catalog_app("product")
    schema = app.openapi()
    expected = {
        "/products/catalog-policies": {"get", "post"},
        "/products/catalog-policies/active": {"get"},
        "/products/catalog-evaluations": {"get"},
        "/products/catalog-evaluations/{evaluation_id}": {"get"},
        "/products/catalog-proposals": {"get"},
        "/products/catalog-proposals/{proposal_id}": {"get"},
        "/products/catalog-cultivation-cases": {"get"},
        "/products/catalog-cultivation-cases/{case_id}": {"get"},
    }
    actual = {
        path: set(schema["paths"][path])
        for path in schema["paths"]
        if path.startswith("/products/catalog-")
    }
    assert actual == expected
    post = schema["paths"]["/products/catalog-policies"]["post"]
    assert any(
        parameter["name"] == "Idempotency-Key"
        and parameter["in"] == "header"
        and parameter["required"] is True
        for parameter in post["parameters"]
    )
    body_schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema.get("additionalProperties") is False


def test_catalog_policy_submit_uses_trusted_actor_and_preserves_raw_key() -> None:
    body = _policy().content.model_dump(mode="json")
    for role in ("product", "sourcing"):
        app, catalog, application, approvals = _catalog_app(role)
        response = _request(
            app,
            "POST",
            "/products/catalog-policies",
            json=body,
            headers=[("Idempotency-Key", "Case-Sensitive.Key/7")],
        )
        assert response.status_code == 202
        assert response.json()["policy"]["policy_version_id"] == POLICY_ID
        assert catalog.calls == []
        assert len(approvals.calls) == 1
        tenant_id, content, key, actor = application.calls[0]
        assert tenant_id == TENANT
        assert content == _policy().content
        assert key == "Case-Sensitive.Key/7"
        assert (actor.actor_id, actor.role.value, actor.tenant_id) == (
            str(EMPLOYEE),
            role,
            TENANT,
        )

    for role in ("boss", "finance", "sales", "manager", "viewer"):
        app, catalog, application, approvals = _catalog_app(role)
        response = _request(
            app,
            "POST",
            "/products/catalog-policies",
            json=body,
            headers=[("Idempotency-Key", "key")],
        )
        assert response.status_code == 403
        assert catalog.calls == application.calls == approvals.calls == []


def test_catalog_policy_submit_rejects_invalid_body_and_raw_headers_before_io() -> None:
    body = _policy().content.model_dump(mode="json")
    invalid_headers = (
        [],
        [("Idempotency-Key", "one"), ("Idempotency-Key", "two")],
        [("Idempotency-Key", "")],
        [("Idempotency-Key", " padded")],
        [("Idempotency-Key", "x" * 201)],
        [("Idempotency-Key", "bad\x7fkey")],
    )
    for headers in invalid_headers:
        app, catalog, application, approvals = _catalog_app("product")
        response = _request(
            app,
            "POST",
            "/products/catalog-policies",
            json=body,
            headers=headers,
        )
        assert response.status_code == 400
        assert catalog.calls == application.calls == approvals.calls == []

    injected = {
        "tenant_id": str(TENANT),
        "actor_role": "boss",
        "proposed_by": str(EMPLOYEE),
        "approval_id": str(APPROVAL_ID),
        "state": "active",
        "facts_hash": HASH,
        "policy_version_id": str(POLICY_ID),
        "evidence_refs": ["secret"],
    }
    for field, value in injected.items():
        app, catalog, application, approvals = _catalog_app("product")
        response = _request(
            app,
            "POST",
            "/products/catalog-policies",
            json={**body, field: value},
            headers=[("Idempotency-Key", "key")],
        )
        assert response.status_code == 422
        assert catalog.calls == application.calls == approvals.calls == []


def test_catalog_policy_submit_rejects_malformed_application_policy_id() -> None:
    app, catalog, application, approvals = _catalog_app("product")
    application.candidate = _policy().model_copy(
        update={"policy_version_id": CatalogProposalPolicyVersionId("cpv_bad")}
    )

    response = _request(
        app,
        "POST",
        "/products/catalog-policies",
        json=_policy().content.model_dump(mode="json"),
        headers=[("Idempotency-Key", "malformed-application-id")],
    )

    assert response.status_code == 503
    assert "cpv_bad" not in response.text
    assert catalog.calls == []
    assert approvals.calls == []


def test_catalog_read_routes_are_role_gated_join_approval_and_bound_limits() -> None:
    paths = (
        "/products/catalog-policies/active",
        "/products/catalog-policies?limit=7",
        "/products/catalog-evaluations?limit=7",
        f"/products/catalog-evaluations/{EVALUATION_ID}",
        "/products/catalog-proposals?limit=7",
        f"/products/catalog-proposals/{PROPOSAL_ID}",
        "/products/catalog-cultivation-cases?limit=7",
        f"/products/catalog-cultivation-cases/{CASE_ID}",
    )
    for role in ("boss", "product", "sourcing", "finance"):
        app, catalog, _, approvals = _catalog_app(role)
        responses = [_get(app, path) for path in paths]
        assert [response.status_code for response in responses] == [200] * len(paths)
        assert {call[0] for call in catalog.calls} == {
            "active",
            "policies",
            "evaluations",
            "evaluation",
            "proposals",
            "proposal",
            "cases",
            "case",
        }
        assert all(call[3] == 7 for call in catalog.calls if call[3] is not None)
        assert approvals.calls
        assert all(call[2].role == role for call in approvals.calls)

    for role in ("sales", "manager", "viewer", "customer"):
        app, catalog, _, approvals = _catalog_app(role)
        assert all(_get(app, path).status_code == 403 for path in paths)
        assert catalog.calls == approvals.calls == []

    for limit in (0, 201):
        app, catalog, _, approvals = _catalog_app("boss")
        assert _get(app, f"/products/catalog-proposals?limit={limit}").status_code == 400
        assert catalog.calls == approvals.calls == []


def test_catalog_details_validate_ids_and_map_product_missing_before_join() -> None:
    app, catalog, _, approvals = _catalog_app("boss")
    for segment in ("catalog-evaluations", "catalog-proposals", "catalog-cultivation-cases"):
        assert _get(app, f"/products/{segment}/bad").status_code == 400
    assert catalog.calls == approvals.calls == []

    catalog.failure = ProductNotFoundError("other tenant secret")
    response = _get(app, f"/products/catalog-proposals/{PROPOSAL_ID}")
    assert response.status_code == 404
    assert "secret" not in response.text
    assert approvals.calls == []


def test_catalog_transient_unknown_and_approval_join_failures_are_redacted_503() -> None:
    for failure in (TransientError("database host"), RuntimeError("socket secret")):
        app, catalog, _, approvals = _catalog_app("boss")
        catalog.failure = failure
        response = _get(app, "/products/catalog-proposals")
        assert response.status_code == 503
        assert "database host" not in response.text
        assert "socket secret" not in response.text
        assert approvals.calls == []

    for failure in (ProductNotFoundError("approval missing"), RuntimeError("approval db")):
        app, _, _, approvals = _catalog_app("boss")
        approvals.failure = failure
        response = _get(app, f"/products/catalog-proposals/{PROPOSAL_ID}")
        assert response.status_code == 503
        assert "approval" not in response.text.lower()


def test_catalog_responses_are_strict_safe_projections() -> None:
    app, _, _, _ = _catalog_app("finance")
    responses = (
        _get(app, "/products/catalog-policies"),
        _get(app, "/products/catalog-evaluations"),
        _get(app, "/products/catalog-proposals"),
        _get(app, "/products/catalog-cultivation-cases"),
    )
    assert all(response.status_code == 200 for response in responses)
    rendered = " ".join(response.text for response in responses).lower()
    for forbidden in (
        "idempotency_key",
        "request_hash",
        "workflow_context",
        "claim_token",
        "decision_note",
        "supplier_price",
        "probability",
    ):
        assert forbidden not in rendered
    approval = responses[2].json()[0]["approval"]
    assert set(approval) == {"approval_id", "approval_type", "state"}


def test_catalog_active_none_and_real_empty_lists_remain_successful() -> None:
    app, catalog, _, approvals = _catalog_app("boss")
    catalog.active_policy = None
    catalog.policies = ()
    catalog.evaluations = ()
    catalog.proposals = ()
    catalog.cases = ()

    active = _get(app, "/products/catalog-policies/active")
    lists = [
        _get(app, path)
        for path in (
            "/products/catalog-policies",
            "/products/catalog-evaluations",
            "/products/catalog-proposals",
            "/products/catalog-cultivation-cases",
        )
    ]

    assert active.status_code == 200 and active.json() is None
    assert all(response.status_code == 200 and response.json() == [] for response in lists)
    assert approvals.calls == []


def test_catalog_malformed_domain_or_wrong_approval_type_is_redacted_503() -> None:
    app, catalog, _, approvals = _catalog_app("boss")
    catalog.proposals = (
        _proposal().model_copy(update={"facts_hash": "private-invalid"}),
    )
    malformed = _get(app, "/products/catalog-proposals")
    assert malformed.status_code == 503
    assert "private-invalid" not in malformed.text
    assert approvals.calls == []

    app, _, _, approvals = _catalog_app("boss")
    approvals.approval_type = "catalog_proposal_policy_change"
    mismatched = _get(app, f"/products/catalog-proposals/{PROPOSAL_ID}")
    assert mismatched.status_code == 503
    assert "catalog_proposal_policy_change" not in mismatched.text


def test_catalog_evaluations_bind_nested_facts_to_request_tenant() -> None:
    app, catalog, _, approvals = _catalog_app("boss")
    other_tenant_facts = _evaluation().facts.model_copy(
        update={"tenant_id": OTHER_TENANT}
    )
    catalog.evaluation = _evaluation().model_copy(
        update={"facts": other_tenant_facts}
    )
    catalog.evaluations = (catalog.evaluation,)

    responses = (
        _get(app, "/products/catalog-evaluations"),
        _get(app, f"/products/catalog-evaluations/{EVALUATION_ID}"),
    )

    assert [response.status_code for response in responses] == [503, 503]
    assert all(str(OTHER_TENANT) not in response.text for response in responses)
    assert approvals.calls == []


def test_catalog_detail_responses_bind_canonical_id_to_path_id() -> None:
    app, catalog, _, approvals = _catalog_app("boss")
    catalog.evaluation = _evaluation().model_copy(
        update={"evaluation_id": OTHER_EVALUATION_ID}
    )
    catalog.proposal = _proposal().model_copy(
        update={"proposal_id": OTHER_PROPOSAL_ID}
    )
    catalog.case = _cultivation().model_copy(
        update={"cultivation_case_id": OTHER_CASE_ID}
    )

    responses = (
        _get(app, f"/products/catalog-evaluations/{EVALUATION_ID}"),
        _get(app, f"/products/catalog-proposals/{PROPOSAL_ID}"),
        _get(app, f"/products/catalog-cultivation-cases/{CASE_ID}"),
    )

    assert [response.status_code for response in responses] == [503, 503, 503]
    assert all("01K39P9M5D6K4A91YEQ80EJZ0Y" not in response.text for response in responses)
    assert approvals.calls == []


def test_catalog_approval_link_binds_returned_id_and_exact_type() -> None:
    app, _, _, approvals = _catalog_app("boss")
    approvals.returned_approval_id = OTHER_APPROVAL_ID

    response = _get(app, f"/products/catalog-proposals/{PROPOSAL_ID}")

    assert response.status_code == 503
    assert str(OTHER_APPROVAL_ID) not in response.text


def test_catalog_lists_reject_backend_overrun_before_any_approval_join() -> None:
    scenarios = (
        ("/products/catalog-policies?limit=1", "policies", (_policy(), _policy())),
        (
            "/products/catalog-evaluations?limit=1",
            "evaluations",
            (_evaluation(), _evaluation()),
        ),
        (
            "/products/catalog-proposals?limit=1",
            "proposals",
            (_proposal(), _proposal()),
        ),
        (
            "/products/catalog-cultivation-cases?limit=1",
            "cases",
            (_cultivation(), _cultivation()),
        ),
    )
    for path, attribute, values in scenarios:
        app, catalog, _, approvals = _catalog_app("boss")
        setattr(catalog, attribute, values)

        response = _get(app, path)

        assert response.status_code == 503
        assert response.json()["code"] == "service_unavailable"
        assert approvals.calls == []
