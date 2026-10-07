"""国家政策 Settings API 经真实 Postgres 合规服务保持租户隔离。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.compliance.permissions import (
    ComplianceActor,
    ComplianceScope,
    Phase1ComplianceAuthorizer,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyApprovalFact,
    CountryPolicyProposalCreate,
)
from domains.compliance.service_impl import ComplianceServiceImpl
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from infra.db.session import create_engine_from
from shared.schemas.identifiers import ApprovalId, EmployeeId, IdempotencyKey, TenantId
from shared.schemas.provenance import SourceType
from tests.provider_readiness_fakes import provider_readiness_dependencies

NOW = datetime(2026, 8, 24, 18, tzinfo=UTC)
TENANT_A = TenantId("tenant-country-policy-api-a")
TENANT_B = TenantId("tenant-country-policy-api-b")
BOSS_A = EmployeeId("emp_country_policy_api_boss_a")
BOSS_B = EmployeeId("emp_country_policy_api_boss_b")


def _actor(tenant_id: TenantId, employee_id: EmployeeId) -> ComplianceActor:
    return ComplianceActor(
        actor_id=str(employee_id),
        tenant_id=tenant_id,
        scope=ComplianceScope.TENANT,
        role="boss",
    )


def _system(tenant_id: TenantId) -> ComplianceActor:
    return ComplianceActor(
        actor_id="system:country-policy-api-test",
        tenant_id=tenant_id,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )


def _identity() -> RequestIdentity:
    employee = EmployeeView(
        employee_id=BOSS_A,
        tenant_id=TENANT_A,
        name="租户 A 老板",
        role="boss",
        is_active=True,
    )
    return RequestIdentity(
        tenant_id=TENANT_A,
        employee=employee,
        employee_actor=EmployeeActor(
            actor_id=str(BOSS_A),
            scope=EmployeeScope.TENANT,
            role="boss",
        ),
        opportunity_actor=OpportunityActor(
            actor_id=str(BOSS_A),
            scope=OpportunityScope(),
            role="boss",
        ),
    )


def _proposal() -> CountryPolicyProposalCreate:
    return CountryPolicyProposalCreate.model_validate(
        {
            "country": "Tenant B Synthetic Market",
            "public_research_allowed": True,
            "contact_enrichment_allowed": True,
            "cold_b2b_email_allowed": False,
            "personal_data_basis_required": True,
            "subject_type_affects_judgment": True,
            "contact_type_affects_judgment": True,
            "opt_out_deadline_days": 30,
            "local_representative_required": False,
            "requirements": ["honor_opt_out"],
            "notes": "Synthetic cross-tenant API fixture.",
            "field_sources": {
                field: {
                    "source_type": SourceType.EMPLOYEE_INPUT,
                    "source_id": f"assessment:{field.value}",
                }
                for field in DECISION_FIELDS
            },
        }
    )


class _UnusedApprovals:
    async def get_by_change_set(self, tenant_id, change_set_ref):
        raise AssertionError((tenant_id, change_set_ref))


@pytest.mark.asyncio
async def test_cross_tenant_version_is_not_visible(db_url: str) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )

    def service(tenant_id: TenantId) -> ComplianceServiceImpl:
        return ComplianceServiceImpl(
            lambda requested_tenant: SqlAlchemyComplianceUnitOfWork(
                factory, requested_tenant, now=lambda: NOW
            ),
            Phase1ComplianceAuthorizer(tenant_id),
            now=lambda: NOW,
        )

    tenant_b_service = service(TENANT_B)
    proposal = await tenant_b_service.propose_country_policy(
        TENANT_B,
        _proposal(),
        actor=_actor(TENANT_B, BOSS_B),
        idempotency_key=IdempotencyKey("country-policy-api-tenant-b"),
    )
    await tenant_b_service.activate_country_policy(
        TENANT_B,
        proposal.country_policy_version_id,
        CountryPolicyApprovalFact(
            approval_id=ApprovalId("apr_country_policy_api_tenant_b"),
            approval_type="country_policy_change",
            change_set_ref=proposal.change_set_ref,
            decided_by=EmployeeId("emp_country_policy_api_approver_b"),
            decided_at=NOW,
        ),
        actor=_system(TENANT_B),
    )

    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT_A), dev_mode=True, retry_after_seconds=19
        )
    )
    app.dependency_overrides[get_request_identity] = _identity
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        compliance=service(TENANT_A),
        approvals=_UnusedApprovals(),
        workflow_engine=object(),
        **provider_readiness_dependencies(TENANT_A),
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {
                "X-Tenant-Id": str(TENANT_A),
                "X-Employee-Id": str(BOSS_A),
            }
            versions = await client.get(
                "/settings/country-policies/versions",
                params={"country": "Tenant B Synthetic Market"},
                headers=headers,
            )
            overview = await client.get("/settings/country-policies", headers=headers)

        assert versions.status_code == 200
        assert versions.json() == []
        assert overview.status_code == 200
        assert overview.json()["active_policies"] == []
        assert overview.json()["coverage"] == {
            "active_policy_count": 0,
            "contact_enrichment_allowed_count": 0,
        }
    finally:
        await engine.dispose()
