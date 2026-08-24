"""Settings Playbook API 的身份、Decimal、审批关联与安全响应。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError as PydanticValidationError

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.routers.settings import PlaybookOverview
from domains.approvals.schemas import ApprovalView
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
    EmployeeId,
    PlaybookVersionId,
    RunId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

TENANT = TenantId("tenant-settings-router")
BOSS = EmployeeId("emp_01K00000000000000000000000")
EMPLOYEE = EmployeeId("emp_01K00000000000000000000001")
VERSION_ID = PlaybookVersionId("pbv_01K00000000000000000000000")
CONTENT_HASH = "a" * 64
CHANGE_SET = f"playbook:{VERSION_ID}:{CONTENT_HASH}"
NOW = datetime(2026, 8, 24, 16, tzinfo=UTC)


def _identity(role: str = "boss") -> RequestIdentity:
    employee_id = BOSS if role == "boss" else EMPLOYEE
    employee = EmployeeView(
        employee_id=employee_id,
        tenant_id=TENANT,
        name="老板" if role == "boss" else "员工",
        role=role,
    )
    return RequestIdentity(
        tenant_id=TENANT,
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
    def __init__(self, view: ApprovalView | None = None) -> None:
        self.view = view

    async def get_by_change_set(self, tenant_id, change_set_ref):
        del tenant_id, change_set_ref
        return self.view


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
    organization: object | None = None,
    approvals: object | None = None,
    workflow: object | None = None,
):
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=19
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: _identity(role)
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        organization=organization,
        approvals=approvals,
        workflow_engine=workflow or _Workflow(),
    )
    return app


def _request(
    app: object,
    method: str,
    path: str,
    *,
    json: dict[str, object] | None = None,
    idempotency_key: str | None = None,
) -> Response:
    headers = {
        "X-Tenant-Id": str(TENANT),
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
