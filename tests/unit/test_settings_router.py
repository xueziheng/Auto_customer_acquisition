"""Settings Playbook API 的身份、Decimal、审批关联与安全响应。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiErrorResponse, ApiSettings
from apps.api.routers.settings import (
    CountryPolicyVersionStatusView,
    PlaybookOverview,
    _SettingsRoute,
)
from domains.approvals.schemas import ApprovalView
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyActivationView,
    CountryPolicyActiveView,
    CountryPolicyCoverage,
    CountryPolicyProposalResult,
    CountryPolicyVersionView,
    normalize_country_key,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.organization.errors import PlaybookNotConfiguredError
from domains.organization.schemas import (
    PlaybookProposalResult,
    PlaybookVersionView,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    PlaybookVersionId,
    RunId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderId,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
)

TENANT = TenantId("tenant-settings-router")
OTHER_TENANT = TenantId("tenant-settings-router-other")
BOSS = EmployeeId("emp_01K00000000000000000000000")
EMPLOYEE = EmployeeId("emp_01K00000000000000000000001")
VERSION_ID = PlaybookVersionId("pbv_01K00000000000000000000000")
CONTENT_HASH = "a" * 64
CHANGE_SET = f"playbook:{VERSION_ID}:{CONTENT_HASH}"
NOW = datetime(2026, 8, 24, 16, tzinfo=UTC)
COUNTRY_VERSION_ID = CountryPolicyVersionId("cpp_01K00000000000000000000000")
_UNSET = object()


class _SyntheticValidationBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    value: int


def _identity(
    role: str = "boss",
    *,
    active: bool = True,
    tenant_id: TenantId = TENANT,
) -> RequestIdentity:
    employee_id = BOSS if role == "boss" else EMPLOYEE
    employee = EmployeeView(
        employee_id=employee_id,
        tenant_id=tenant_id,
        name="老板" if role == "boss" else "员工",
        role=role,
        is_active=active,
    )
    return RequestIdentity(
        tenant_id=tenant_id,
        employee=employee,
        employee_actor=EmployeeActor(
            str(employee_id),
            EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF,
            role,
        ),
        opportunity_actor=OpportunityActor(
            str(employee_id), OpportunityScope(), role
        ),
    )


def _country_policy_version(
    *,
    suffix: str = "0",
    country: str = "Synthetic Market",
    enrichment_allowed: bool = False,
) -> CountryPolicyVersionView:
    version_id = CountryPolicyVersionId(f"cpp_01K0000000000000000000000{suffix}")
    content_hash = (
        ({"0": "a", "1": "b", "2": "c", "3": "d", "4": "e", "5": "f"}[suffix]) * 64
    )
    provenance = {
        field: Provenance(
            SourceType.EMPLOYEE_INPUT,
            f"assessment:{field.value}",
            f"human:{BOSS}",
            NOW,
            confirmed_by=BOSS,
            confirmed_at=NOW,
        )
        for field in DECISION_FIELDS
    }
    return CountryPolicyVersionView(
        country_policy_version_id=version_id,
        country=country,
        country_key=normalize_country_key(country),
        version_number=int(suffix) + 1,
        content_hash=content_hash,
        base_version_id=None,
        base_content_hash=None,
        public_research_allowed=True,
        contact_enrichment_allowed=enrichment_allowed,
        cold_b2b_email_allowed=False,
        personal_data_basis_required=True,
        subject_type_affects_judgment=True,
        contact_type_affects_judgment=True,
        opt_out_deadline_days=30,
        local_representative_required=False,
        requirements=("honor_opt_out",),
        notes="Synthetic policy fixture.",
        field_provenance=provenance,
        proposed_by=BOSS,
        proposed_at=NOW,
        change_set_ref=f"country_policy:{version_id}:{content_hash}",
    )


def _country_policy_active(
    version: CountryPolicyVersionView,
) -> CountryPolicyActiveView:
    return CountryPolicyActiveView(
        version=version,
        activation=CountryPolicyActivationView(
            activation_id=CountryPolicyActivationId(
                "cpa_01K00000000000000000000000"
            ),
            activation_sequence=1,
            country_key=version.country_key,
            country_policy_version_id=version.country_policy_version_id,
            content_hash=version.content_hash,
            approval_id=ApprovalId("apr_01K00000000000000000000000"),
            change_set_ref=version.change_set_ref,
            approved_by=EMPLOYEE,
            approved_at=NOW,
            activated_by="system:country-policy-change",
            activated_at=NOW,
        ),
    )


def _version() -> PlaybookVersionView:
    return PlaybookVersionView(
        playbook_version_id=VERSION_ID,
        version_number=1,
        content_hash=CONTENT_HASH,
        base_version_id=None,
        base_content_hash=None,
        company_type="trading_company",
        minimum_deal_amount="10000",
        minimum_deal_currency="USD",
        excluded_categories=("adult",),
        sourcing_regions=("guangdong",),
        excluded_countries=("north korea",),
        monthly_budget_credits=500,
        approval_requirements=("catalog_reference_price",),
        supply_capabilities_note="Hardware sourcing.",
        proposed_by=BOSS,
        proposed_at=NOW,
        content_provenance=Provenance(
            SourceType.EMPLOYEE_INPUT,
            str(VERSION_ID),
            f"human:{BOSS}",
            NOW,
        ),
        change_set_ref=CHANGE_SET,
    )


def _active() -> SimpleNamespace:
    provenance = Provenance(
        SourceType.EMPLOYEE_INPUT,
        str(VERSION_ID),
        f"human:{BOSS}",
        NOW,
        confirmed_by=EMPLOYEE,
        confirmed_at=NOW + timedelta(minutes=1),
    )
    return SimpleNamespace(
        tenant_id=TENANT,
        playbook_version_id=VERSION_ID,
        activation_id="pba_01K00000000000000000000000",
        version_number=1,
        content_hash=CONTENT_HASH,
        base_version_id=None,
        base_content_hash=None,
        approval_id="apr_01K00000000000000000000000",
        change_set_ref=CHANGE_SET,
        company_type="trading_company",
        minimum_deal_value=Money(Decimal("10000.00"), CurrencyCode("USD")),
        proposed_by=BOSS,
        proposed_at=NOW,
        approved_by=EMPLOYEE,
        approved_at=NOW + timedelta(minutes=1),
        activated_by="system:playbook-change",
        activated_at=NOW + timedelta(minutes=2),
        content_provenance=provenance,
        excluded_categories=("adult",),
        sourcing_regions=("guangdong",),
        excluded_countries=("north korea",),
        monthly_budget_credits=500,
        approval_requirements=("catalog_reference_price",),
        supply_capabilities_note="Hardware sourcing.",
    )


def _approval(**overrides: object) -> ApprovalView:
    values: dict[str, object] = {
        "approval_id": "apr_01K00000000000000000000000",
        "approval_type": "playbook_change",
        "type_label": "Company Playbook 变更",
        "title": "激活 Company Playbook v1",
        "reason": "老板提交了经营边界。",
        "proposed_change_display": {},
        "affected_entities": [f"Company Playbook {VERSION_ID}"],
        "if_approved": "激活",
        "if_rejected": "保持",
        "reversible": True,
        "state": "pending",
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=7),
        "change_set_ref": CHANGE_SET,
    }
    values.update(overrides)
    return ApprovalView(**values)  # type: ignore[arg-type]


class _Organization:
    def __init__(
        self,
        *,
        configured: bool = False,
        versions: list[PlaybookVersionView] | None = None,
    ) -> None:
        self.configured = configured
        self.versions = list(versions or [])
        self.proposals: list[tuple[Any, ...]] = []

    async def get_playbook(self, tenant_id, *, actor):
        del tenant_id, actor
        if not self.configured:
            raise PlaybookNotConfiguredError("missing")
        return _active()

    async def list_versions(self, tenant_id, *, actor, limit=50):
        del tenant_id, actor
        return self.versions[:limit]

    async def propose_playbook(
        self, tenant_id, command, *, actor, idempotency_key
    ):
        self.proposals.append((tenant_id, command, actor, idempotency_key))
        return PlaybookProposalResult(
            playbook_version_id=VERSION_ID,
            version_number=1,
            content_hash=CONTENT_HASH,
            change_set_ref=CHANGE_SET,
        )


class _Approvals:
    def __init__(
        self,
        view: ApprovalView | None = None,
        *,
        by_change_set: dict[str, ApprovalView | None] | None = None,
    ) -> None:
        self.view = view
        self.by_change_set = by_change_set

    async def get_by_change_set(self, tenant_id, change_set_ref):
        del tenant_id
        if self.by_change_set is not None:
            return self.by_change_set.get(change_set_ref)
        return self.view


class _Compliance:
    def __init__(
        self,
        *,
        active_policies: (
            list[CountryPolicyVersionView | CountryPolicyActiveView] | None
        ) = None,
        versions: list[CountryPolicyVersionView] | None = None,
        coverage: CountryPolicyCoverage | None = None,
    ) -> None:
        self.active_policies = [
            policy
            if isinstance(policy, CountryPolicyActiveView)
            else _country_policy_active(policy)
            for policy in active_policies or []
        ]
        self.versions = list(versions or [])
        self.coverage = coverage or CountryPolicyCoverage(
            active_policy_count=len(self.active_policies),
            contact_enrichment_allowed_count=sum(
                policy.version.contact_enrichment_allowed
                for policy in self.active_policies
            ),
        )
        self.proposals: list[tuple[Any, ...]] = []
        self.version_queries: list[tuple[Any, ...]] = []

    async def list_active_policies(self, tenant_id, *, actor, limit=50):
        del tenant_id, actor
        return self.active_policies[:limit]

    async def list_versions(self, tenant_id, country, *, actor, limit=50):
        self.version_queries.append((tenant_id, country, actor, limit))
        country_key = normalize_country_key(country)
        return [
            version for version in self.versions if version.country_key == country_key
        ][:limit]

    async def get_coverage(self, tenant_id, *, actor):
        del tenant_id, actor
        return self.coverage

    async def propose_country_policy(
        self, tenant_id, command, *, actor, idempotency_key
    ):
        self.proposals.append((tenant_id, command, actor, idempotency_key))
        version = _country_policy_version(enrichment_allowed=True)
        return CountryPolicyProposalResult(
            country_policy_version_id=version.country_policy_version_id,
            country_key=version.country_key,
            version_number=version.version_number,
            content_hash=version.content_hash,
            change_set_ref=version.change_set_ref,
        )


class _ProviderReadiness:
    def __init__(
        self,
        state: ProviderReadinessState = ProviderReadinessState.RUNTIME_NOT_COMPOSED,
        *,
        result: object = _UNSET,
        error: Exception | None = None,
    ) -> None:
        self.state = state
        self.result = result
        self.error = error
        self.calls: list[tuple[object, object, object]] = []

    async def get_snapshot(self, tenant_id, capabilities, *, actor):
        self.calls.append((tenant_id, capabilities, actor))
        if self.error is not None:
            raise self.error
        if self.result is not _UNSET:
            return self.result
        return ProviderReadinessSnapshot(
            tenant_id=tenant_id,
            provider=ProviderId.HUNTER,
            capabilities=HUNTER_CONTACT_CAPABILITIES,
            configuration=None,
            state=self.state,
            failure_code=None,
            events=(),
        )


def _provider_readiness_actor(
    tenant_id: TenantId = TENANT,
) -> ProviderReadinessActor:
    return ProviderReadinessActor(
        actor_id="system:api-provider-readiness",
        tenant_id=tenant_id,
        permissions=frozenset({ProviderReadinessPermission.READ}),
    )


class _Workflow:
    def __init__(self) -> None:
        self.starts: list[tuple[Any, ...]] = []

    async def start(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        initial_context,
        idempotency_key,
        *,
        scheduled_at=None,
    ):
        self.starts.append(
            (
                tenant_id,
                workflow_type,
                subject_ref,
                initial_context,
                idempotency_key,
                scheduled_at,
            )
        )
        return RunId("run_01K00000000000000000000000")


def _app(
    *,
    role: str = "boss",
    active: bool = True,
    identity_tenant: TenantId = TENANT,
    organization: object | None = None,
    approvals: object | None = None,
    compliance: object | None = _UNSET,
    workflow: object | None = None,
    provider_readiness: object | None = None,
    provider_readiness_actor: ProviderReadinessActor | None = None,
):
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=19
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(
        role, active=active, tenant_id=identity_tenant
    )
    resolved_compliance = _Compliance() if compliance is _UNSET else compliance
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        organization=organization,
        approvals=approvals,
        compliance=resolved_compliance,
        workflow_engine=workflow or _Workflow(),
        provider_readiness=provider_readiness or _ProviderReadiness(),
        provider_readiness_actor=(
            provider_readiness_actor or _provider_readiness_actor()
        ),
    )
    return app


def _validation_contract_app():
    app = _app()
    synthetic_router = APIRouter(route_class=_SettingsRoute)

    @synthetic_router.post(
        "/explicit-api-error",
        responses={422: {"model": ApiErrorResponse}},
    )
    async def explicit_api_error(
        body: _SyntheticValidationBody,
    ) -> _SyntheticValidationBody:
        return body

    @synthetic_router.post("/default-validation")
    async def default_validation(
        body: _SyntheticValidationBody,
    ) -> _SyntheticValidationBody:
        return body

    @synthetic_router.post(
        "/wrong-error-model",
        responses={422: {"model": _SyntheticValidationBody}},
    )
    async def wrong_error_model(
        body: _SyntheticValidationBody,
    ) -> _SyntheticValidationBody:
        return body

    app.include_router(synthetic_router, prefix="/settings/_test")
    return app


def _request(
    app: object,
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    tenant_header: str = str(TENANT),
) -> Response:
    headers = {
        "X-Tenant-Id": tenant_header,
        "X-Employee-Id": str(BOSS),
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key

    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.request(method, path, headers=headers, json=json)

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        ("/settings/_test/explicit-api-error", 422, "http_error"),
        ("/settings/_test/default-validation", 400, "validation_error"),
        ("/settings/_test/wrong-error-model", 400, "validation_error"),
    ],
)
def test_settings_runtime_validation_follows_explicit_api_error_contract(
    path: str,
    expected_status: int,
    expected_code: str,
) -> None:
    response = _request(
        _validation_contract_app(),
        "POST",
        path,
        json={"value": "not-an-integer"},
    )

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code


@pytest.fixture
def valid_proposal() -> dict[str, object]:
    return {
        "company_type": "trading_company",
        "minimum_deal_amount": "10000.00",
        "minimum_deal_currency": "USD",
        "excluded_categories": ["adult"],
        "sourcing_regions": ["guangdong"],
        "excluded_countries": ["north korea"],
        "monthly_budget_credits": 500,
        "approval_requirements": ["catalog_reference_price"],
        "supply_capabilities_note": "Hardware sourcing.",
    }


@pytest.fixture
def valid_country_policy_proposal() -> dict[str, object]:
    return {
        "country": "Synthetic Market",
        "public_research_allowed": True,
        "contact_enrichment_allowed": True,
        "cold_b2b_email_allowed": False,
        "personal_data_basis_required": True,
        "subject_type_affects_judgment": True,
        "contact_type_affects_judgment": True,
        "opt_out_deadline_days": 30,
        "local_representative_required": False,
        "requirements": ["honor_opt_out"],
        "notes": "Synthetic policy fixture.",
        "field_sources": {
            field.value: {
                "source_type": "employee_input",
                "source_id": f"assessment:{field.value}",
            }
            for field in DECISION_FIELDS
        },
    }


def test_missing_playbook_is_explicit_and_has_no_default() -> None:
    response = _request(
        _app(organization=_Organization(), approvals=_Approvals()),
        "GET",
        "/settings/playbook",
    )

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "active_version": None,
        "contact_enrichment": {
            "state": "blocked",
            "reason_code": "COUNTRY_POLICY_NOT_CONFIGURED",
        },
    }


def test_active_playbook_maps_one_consistent_version_and_activation() -> None:
    response = _request(
        _app(
            organization=_Organization(configured=True), approvals=_Approvals()
        ),
        "GET",
        "/settings/playbook",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["active_version"]["version"]["minimum_deal_amount"] == "10000"
    assert body["active_version"]["activation"]["playbook_version_id"] == str(
        VERSION_ID
    )


def test_playbook_overview_consumes_truthful_country_policy_readiness() -> None:
    response = _request(
        _app(
            organization=_Organization(configured=True),
            approvals=_Approvals(),
            compliance=_Compliance(
                active_policies=[
                    _country_policy_version(enrichment_allowed=True)
                ]
            ),
            provider_readiness=_ProviderReadiness(
                ProviderReadinessState.RUNTIME_NOT_COMPOSED
            ),
        ),
        "GET",
        "/settings/playbook",
    )

    assert response.status_code == 200
    assert response.json()["contact_enrichment"] == {
        "state": "blocked",
        "reason_code": "CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED",
    }


def test_settings_has_no_direct_update_or_force_route() -> None:
    app = _app(organization=_Organization(), approvals=_Approvals())

    assert _request(app, "PUT", "/settings/playbook", json={}).status_code == 405
    assert (
        _request(
            app, "POST", "/settings/playbook/apply", json={"force": True}
        ).status_code
        == 404
    )


def test_employee_cannot_read_full_playbook() -> None:
    response = _request(
        _app(
            role="sales", organization=_Organization(), approvals=_Approvals()
        ),
        "GET",
        "/settings/playbook",
    )
    assert response.status_code == 403


@pytest.mark.parametrize("amount", [10.5, 10, True, "NaN", "Infinity"])
def test_proposal_rejects_non_decimal_json_amount(
    valid_proposal: dict[str, object], amount: object
) -> None:
    response = _request(
        _app(organization=_Organization(), approvals=_Approvals()),
        "POST",
        "/settings/playbook/proposals",
        json={**valid_proposal, "minimum_deal_amount": amount},
        idempotency_key="settings-submit-1",
    )
    assert response.status_code == 400


def test_proposal_rejects_body_idempotency_field(
    valid_proposal: dict[str, object],
) -> None:
    response = _request(
        _app(organization=_Organization(), approvals=_Approvals()),
        "POST",
        "/settings/playbook/proposals",
        json={**valid_proposal, "idempotency_key": "body-key"},
        idempotency_key="settings-submit-1",
    )
    assert response.status_code == 400


def test_proposal_requires_idempotency_header(
    valid_proposal: dict[str, object],
) -> None:
    response = _request(
        _app(organization=_Organization(), approvals=_Approvals()),
        "POST",
        "/settings/playbook/proposals",
        json=valid_proposal,
    )
    assert response.status_code == 400


def test_missing_feature_composition_returns_retryable_503() -> None:
    missing_organization = _request(
        _app(organization=None, approvals=_Approvals()),
        "GET",
        "/settings/playbook",
    )
    missing_approvals = _request(
        _app(organization=_Organization(versions=[_version()]), approvals=None),
        "GET",
        "/settings/playbook/versions",
    )

    assert missing_organization.status_code == 503
    assert missing_organization.headers["Retry-After"] == "19"
    assert missing_approvals.status_code == 503
    assert missing_approvals.headers["Retry-After"] == "19"


def test_versions_reject_mismatched_approval_fact_as_integrity_failure() -> None:
    response = _request(
        _app(
            organization=_Organization(versions=[_version()]),
            approvals=_Approvals(_approval(approval_type="quote_send")),
        ),
        "GET",
        "/settings/playbook/versions",
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "19"


def test_version_status_never_exposes_arbitrary_application_error() -> None:
    response = _request(
        _app(
            organization=_Organization(versions=[_version()]),
            approvals=_Approvals(
                _approval(
                    state="apply_failed",
                    application_error_code="raw database password",
                )
            ),
        ),
        "GET",
        "/settings/playbook/versions",
    )

    assert response.status_code == 200
    assert response.json()[0]["approval_state"] == "apply_failed"
    assert response.json()[0]["application_error_code"] is None


def test_version_without_approval_is_pending_workflow_submission() -> None:
    response = _request(
        _app(
            organization=_Organization(versions=[_version()]),
            approvals=_Approvals(None),
        ),
        "GET",
        "/settings/playbook/versions",
    )
    assert response.status_code == 200
    assert response.json()[0]["approval_state"] == "proposal_pending_submission"


def test_proposal_starts_id_only_workflow_context(
    valid_proposal: dict[str, object],
) -> None:
    organization = _Organization()
    workflow = _Workflow()
    response = _request(
        _app(
            organization=organization,
            approvals=_Approvals(),
            workflow=workflow,
        ),
        "POST",
        "/settings/playbook/proposals",
        json=valid_proposal,
        idempotency_key="settings-submit-1",
    )

    assert response.status_code == 202
    assert response.json() == {
        "playbook_version_id": str(VERSION_ID),
        "run_id": "run_01K00000000000000000000000",
        "change_set_ref": CHANGE_SET,
    }
    assert workflow.starts[0][3] == {
        "playbook_version_id": str(VERSION_ID),
        "content_hash": CONTENT_HASH,
        "change_set_ref": CHANGE_SET,
        "proposed_by": str(BOSS),
    }


def test_overview_model_enforces_configured_active_pair() -> None:
    with pytest.raises(PydanticValidationError):
        PlaybookOverview(configured=True, active_version=None)


def test_only_active_boss_can_read_or_propose_country_policy(
    valid_country_policy_proposal: dict[str, object],
) -> None:
    compliance = _Compliance()
    active_boss_app = _app(
        organization=_Organization(),
        approvals=_Approvals(),
        compliance=compliance,
    )

    assert (
        _request(active_boss_app, "GET", "/settings/country-policies").status_code
        == 200
    )
    assert (
        _request(
            active_boss_app,
            "POST",
            "/settings/country-policies/proposals",
            json=valid_country_policy_proposal,
            idempotency_key="country-policy-submit-1",
        ).status_code
        == 202
    )

    for app in (
        _app(role="manager", compliance=_Compliance()),
        _app(role="sales", compliance=_Compliance()),
        _app(role="boss", active=False, compliance=_Compliance()),
    ):
        assert _request(app, "GET", "/settings/country-policies").status_code == 403
        assert (
            _request(
                app,
                "POST",
                "/settings/country-policies/proposals",
                json=valid_country_policy_proposal,
                idempotency_key="country-policy-submit-denied",
            ).status_code
            == 403
        )

    other_tenant = _request(
        active_boss_app,
        "GET",
        "/settings/country-policies",
        tenant_header=str(OTHER_TENANT),
    )
    assert other_tenant.status_code == 403
    assert other_tenant.json() == {
        "code": "tenant_forbidden",
        "message": "租户身份不匹配",
    }


@pytest.mark.parametrize("forbidden_field", ["tenant_id", "actor", "activation"])
def test_request_body_cannot_supply_tenant_actor_or_activation(
    valid_country_policy_proposal: dict[str, object], forbidden_field: str
) -> None:
    compliance = _Compliance()
    response = _request(
        _app(compliance=compliance),
        "POST",
        "/settings/country-policies/proposals",
        json={**valid_country_policy_proposal, forbidden_field: "untrusted"},
        idempotency_key="country-policy-submit-extra",
    )

    assert response.status_code == 422
    assert compliance.proposals == []


def test_proposal_requires_idempotency_header_and_starts_exact_workflow(
    valid_country_policy_proposal: dict[str, object],
) -> None:
    compliance = _Compliance()
    workflow = _Workflow()
    app = _app(compliance=compliance, workflow=workflow)

    missing = _request(
        app,
        "POST",
        "/settings/country-policies/proposals",
        json=valid_country_policy_proposal,
    )
    accepted = _request(
        app,
        "POST",
        "/settings/country-policies/proposals",
        json=valid_country_policy_proposal,
        idempotency_key="country-policy-submit-exact",
    )

    assert missing.status_code == 422
    assert accepted.status_code == 202
    assert accepted.json() == {
        "country_policy_version_id": str(COUNTRY_VERSION_ID),
        "run_id": "run_01K00000000000000000000000",
        "change_set_ref": f"country_policy:{COUNTRY_VERSION_ID}:{'a' * 64}",
    }
    assert len(compliance.proposals) == 1
    proposal_tenant, _, proposal_actor, proposal_key = compliance.proposals[0]
    assert proposal_tenant == TENANT
    assert proposal_actor.actor_id == str(BOSS)
    assert proposal_actor.tenant_id == TENANT
    assert proposal_actor.scope.value == "tenant"
    assert proposal_actor.role == "boss"
    assert str(proposal_key) == "country-policy-submit-exact"
    assert workflow.starts == [
        (
            TENANT,
            "country_policy_change",
            str(COUNTRY_VERSION_ID),
            {
                "country_policy_version_id": str(COUNTRY_VERSION_ID),
                "country_key": "synthetic market",
                "content_hash": "a" * 64,
                "change_set_ref": (f"country_policy:{COUNTRY_VERSION_ID}:{'a' * 64}"),
                "proposed_by": str(BOSS),
            },
            f"country-policy-change:{TENANT}:{COUNTRY_VERSION_ID}",
            None,
        )
    ]


def test_versions_are_country_scoped_and_join_approval_state() -> None:
    states = ("pending", "approved", "rejected", "expired", "applied", "apply_failed")
    versions = [_country_policy_version(suffix=str(index)) for index in range(6)]
    other_country = _country_policy_version(suffix="0", country="Other Market")
    approvals = _Approvals(
        by_change_set={
            version.change_set_ref: _approval(
                approval_id=f"apr_country_policy_{index}",
                approval_type="country_policy_change",
                change_set_ref=version.change_set_ref,
                state=state,
                decided_by_employee=(
                    EMPLOYEE
                    if state in {"approved", "rejected", "applied", "apply_failed"}
                    else None
                ),
                decided_at=(
                    NOW + timedelta(minutes=2)
                    if state in {"approved", "rejected", "applied", "apply_failed"}
                    else None
                ),
                application_error_code=(
                    "COUNTRY_POLICY_BASE_VERSION_CONFLICT"
                    if state == "apply_failed"
                    else None
                ),
            )
            for index, (version, state) in enumerate(zip(versions, states, strict=True))
        }
    )
    compliance = _Compliance(versions=[*versions, other_country])

    response = _request(
        _app(compliance=compliance, approvals=approvals),
        "GET",
        "/settings/country-policies/versions?country=%20%20Synthetic%20%20%20Market%20&limit=6",
    )

    assert response.status_code == 200
    assert [item["approval_state"] for item in response.json()] == list(states)
    assert [item.get("approval_decided_by") for item in response.json()] == [
        None,
        str(EMPLOYEE),
        str(EMPLOYEE),
        None,
        str(EMPLOYEE),
        str(EMPLOYEE),
    ]
    assert [item.get("approval_decided_at") for item in response.json()] == [
        None,
        (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        None,
        (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
    ]
    assert {item["version"]["country_key"] for item in response.json()} == {
        "synthetic market"
    }
    query_tenant, query_country, query_actor, query_limit = (
        compliance.version_queries[0]
    )
    assert query_tenant == TENANT
    assert query_country == "  Synthetic   Market "
    assert query_actor.actor_id == str(BOSS)
    assert query_actor.tenant_id == TENANT
    assert query_limit == 6


@pytest.mark.parametrize(
    "approval_overrides",
    [
        {"approval_type": "playbook_change"},
        {"change_set_ref": f"country_policy:{COUNTRY_VERSION_ID}:{'f' * 64}"},
    ],
)
def test_mismatched_country_policy_approval_fact_is_not_joined(
    approval_overrides: dict[str, object],
) -> None:
    version = _country_policy_version()
    approvals = _Approvals(
        by_change_set={
            version.change_set_ref: _approval(
                **{
                    "approval_type": "country_policy_change",
                    "change_set_ref": version.change_set_ref,
                    **approval_overrides,
                }
            )
        }
    )

    response = _request(
        _app(compliance=_Compliance(versions=[version]), approvals=approvals),
        "GET",
        "/settings/country-policies/versions?country=Synthetic%20Market",
    )

    assert response.status_code == 503


def test_country_policy_version_never_exposes_arbitrary_application_error() -> None:
    version = _country_policy_version()
    approvals = _Approvals(
        by_change_set={
            version.change_set_ref: _approval(
                approval_type="country_policy_change",
                change_set_ref=version.change_set_ref,
                state="apply_failed",
                decided_by_employee=EMPLOYEE,
                decided_at=NOW,
                application_error_code="raw database password",
            )
        }
    )

    response = _request(
        _app(compliance=_Compliance(versions=[version]), approvals=approvals),
        "GET",
        "/settings/country-policies/versions?country=Synthetic%20Market",
    )

    assert response.status_code == 200
    assert response.json()[0]["approval_state"] == "apply_failed"
    assert response.json()[0]["application_error_code"] is None


def test_readiness_none_active_is_country_policy_not_configured() -> None:
    provider = _ProviderReadiness()
    response = _request(
        _app(compliance=_Compliance(), provider_readiness=provider),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 200
    assert response.json() == {
        "active_policies": [],
        "coverage": {
            "active_policy_count": 0,
            "contact_enrichment_allowed_count": 0,
        },
        "contact_enrichment": {
            "state": "blocked",
            "reason_code": "COUNTRY_POLICY_NOT_CONFIGURED",
        },
    }
    assert provider.calls == []


def test_readiness_active_but_all_denied_is_contact_enrichment_not_allowed() -> None:
    policy = _country_policy_version(enrichment_allowed=False)
    provider = _ProviderReadiness()
    response = _request(
        _app(
            compliance=_Compliance(active_policies=[policy]),
            provider_readiness=provider,
        ),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"] == {
        "active_policy_count": 1,
        "contact_enrichment_allowed_count": 0,
    }
    assert body["contact_enrichment"] == {
        "state": "blocked",
        "reason_code": "CONTACT_ENRICHMENT_NOT_ALLOWED",
    }
    assert provider.calls == []


@pytest.mark.parametrize(
    ("provider_state", "reason"),
    [
        (
            ProviderReadinessState.PROVIDER_NOT_CONFIGURED,
            "CONTACT_ENRICHMENT_PROVIDER_NOT_CONFIGURED",
        ),
        (
            ProviderReadinessState.VALIDATION_NOT_RUN,
            "CONTACT_ENRICHMENT_PROVIDER_VALIDATION_PENDING",
        ),
        (
            ProviderReadinessState.VALIDATION_FAILED,
            "CONTACT_ENRICHMENT_PROVIDER_VALIDATION_FAILED",
        ),
        (
            ProviderReadinessState.VALIDATION_INCONCLUSIVE,
            "CONTACT_ENRICHMENT_PROVIDER_VALIDATION_INCONCLUSIVE",
        ),
        (
            ProviderReadinessState.RUNTIME_NOT_COMPOSED,
            "CONTACT_ENRICHMENT_RUNTIME_NOT_COMPOSED",
        ),
    ],
)
def test_settings_maps_provider_state_to_exact_reason(
    provider_state: ProviderReadinessState, reason: str
) -> None:
    policy = _country_policy_version(enrichment_allowed=True)
    provider = _ProviderReadiness(provider_state)
    response = _request(
        _app(
            compliance=_Compliance(active_policies=[policy]),
            provider_readiness=provider,
        ),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"] == {
        "active_policy_count": 1,
        "contact_enrichment_allowed_count": 1,
    }
    assert body["contact_enrichment"] == {
        "state": "blocked",
        "reason_code": reason,
    }
    assert provider.calls == [
        (TENANT, HUNTER_CONTACT_CAPABILITIES, _provider_readiness_actor())
    ]


def test_readiness_requires_allowed_policy_and_provider_ready() -> None:
    policy = _country_policy_version(enrichment_allowed=True)
    response = _request(
        _app(
            compliance=_Compliance(active_policies=[policy]),
            provider_readiness=_ProviderReadiness(ProviderReadinessState.READY),
        ),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 200
    assert response.json()["contact_enrichment"] == {
        "state": "ready",
        "reason_code": None,
    }


@pytest.mark.parametrize(
    "provider",
    [
        _ProviderReadiness(error=RuntimeError("database password raw canary")),
        _ProviderReadiness(result=object()),
    ],
)
def test_readiness_provider_reader_failure_is_sanitized(provider: object) -> None:
    policy = _country_policy_version(enrichment_allowed=True)
    response = _request(
        _app(
            compliance=_Compliance(active_policies=[policy]),
            provider_readiness=provider,
        ),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "service_unavailable",
        "message": "服务暂时不可用",
    }
    assert "password" not in response.text
    assert response.headers["Retry-After"] == "19"


def test_readiness_cross_tenant_actor_fails() -> None:
    policy = _country_policy_version(enrichment_allowed=True)
    provider = _ProviderReadiness(ProviderReadinessState.READY)
    response = _request(
        _app(
            compliance=_Compliance(active_policies=[policy]),
            provider_readiness=provider,
            provider_readiness_actor=_provider_readiness_actor(OTHER_TENANT),
        ),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "service_unavailable",
        "message": "服务暂时不可用",
    }
    assert provider.calls == []


def test_active_policy_api_exposes_version_and_activation_audit() -> None:
    policy = _country_policy_version(enrichment_allowed=True)
    response = _request(
        _app(compliance=_Compliance(active_policies=[policy])),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 200
    active = response.json()["active_policies"][0]
    assert active["version"]["proposed_by"] == str(BOSS)
    assert active["activation"]["approved_by"] == str(EMPLOYEE)
    assert active["activation"]["activated_at"] == NOW.isoformat().replace(
        "+00:00", "Z"
    )


def test_country_policy_version_status_accepts_only_consistent_decision_pair() -> None:
    version = _country_policy_version()
    valid = CountryPolicyVersionStatusView(
        version=version,
        approval_id=ApprovalId("apr_country_policy_valid_pair"),
        application_error_code=None,
        approval_state="approved",
        approval_decided_by=EMPLOYEE,
        approval_decided_at=NOW,
    )
    assert valid.approval_decided_by == EMPLOYEE
    assert valid.approval_decided_at == NOW

    for values in (
        {"approval_decided_by": EMPLOYEE, "approval_decided_at": None},
        {"approval_decided_by": None, "approval_decided_at": NOW},
        {"approval_decided_by": EMPLOYEE, "approval_decided_at": NOW},
    ):
        with pytest.raises(PydanticValidationError):
            CountryPolicyVersionStatusView(
                version=version,
                approval_id=ApprovalId("apr_country_policy_invalid_pair"),
                application_error_code=None,
                approval_state="pending",
                **values,
            )


def test_country_policy_approval_decision_pair_mismatch_is_transient() -> None:
    version = _country_policy_version()
    approvals = _Approvals(
        by_change_set={
            version.change_set_ref: _approval(
                approval_type="country_policy_change",
                change_set_ref=version.change_set_ref,
                state="approved",
                decided_by_employee=EMPLOYEE,
                decided_at=None,
            )
        }
    )

    response = _request(
        _app(compliance=_Compliance(versions=[version]), approvals=approvals),
        "GET",
        "/settings/country-policies/versions?country=Synthetic%20Market",
    )

    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


def test_missing_compliance_is_transient_not_policy_not_configured() -> None:
    response = _request(
        _app(compliance=None),
        "GET",
        "/settings/country-policies",
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "19"


def test_router_has_no_country_policy_activate_update_or_delete_route() -> None:
    schema = _app(compliance=_Compliance()).openapi()
    country_policy_methods = {
        path: set(path_item) & {"get", "post", "put", "patch", "delete"}
        for path, path_item in schema["paths"].items()
        if path.startswith("/settings/country-policies")
    }

    assert country_policy_methods == {
        "/settings/country-policies": {"get"},
        "/settings/country-policies/versions": {"get"},
        "/settings/country-policies/proposals": {"post"},
    }


def test_country_policy_openapi_exposes_safe_source_type() -> None:
    schema = _app(compliance=_Compliance()).openapi()
    components = schema["components"]["schemas"]

    assert components["CountryPolicyFieldSourceInput"]["properties"][
        "source_type"
    ].get("enum") == ["web_page", "upload", "employee_input"]


def test_country_policy_openapi_exposes_audit_fields() -> None:
    schema = _app(compliance=_Compliance()).openapi()
    components = schema["components"]["schemas"]

    assert set(components["CountryPolicyActiveView"]["properties"]) == {
        "version",
        "activation",
    }
    assert {
        "approval_decided_by",
        "approval_decided_at",
    } <= set(components["CountryPolicyVersionStatusView"]["properties"])
