"""自动搜索引用必须经当前员工与原 Campaign 公共服务核验。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.api.composition.inventory import CurrentCustomerAgentAuthority
from domains.outreach.customer_agent_policy import CustomerAgentKnowledgeRef
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CustomerAgentPolicyInput,
)
from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from shared.errors import PermissionDenied
from shared.schemas.identifiers import ApprovalId, EmployeeId, new_id
from tests.unit.test_outreach_service import NOW, TENANT, _request, _service


@pytest.mark.parametrize(
    "failure",
    [None, "employee", "owner", "approval", "version", "market", "category", "refs"],
)
async def test_current_scope_authority(failure):
    service, _, _, approvals, _ = _service()
    boss_id = EmployeeId(new_id("emp"))
    owner_id = EmployeeId(new_id("emp"))
    actor = Actor(boss_id, OutreachScope(level=ScopeLevel.TENANT), "boss")
    view = await service.create_campaign(
        TENANT,
        replace(
            _request(),
            customer_agent_policy=CustomerAgentPolicyInput(
                owner_employee_id=owner_id,
                assessment_ref="lia:corporate",
                confirmed_refs=()
                if failure == "refs"
                else (
                    CustomerAgentKnowledgeRef(
                        document_id="doc:test", revision_id="rev:test"
                    ),
                ),
            ),
        ),
        actor=actor,
    )
    await service.submit_campaign(TENANT, view.campaign_id, actor=actor)
    approved = CampaignApprovalSnapshot(
        TENANT,
        view.campaign_id,
        1,
        ApprovalId(new_id("apr")),
        CampaignApprovalState.APPROVED,
        boss_id,
        NOW,
    )
    approvals.values[(view.campaign_id, 1)] = approved
    await service.activate_campaign(TENANT, view.campaign_id, actor=actor)

    class Employees:
        async def get_employee(self, tenant_id, employee_id, *, actor):
            return SimpleNamespace(
                tenant_id=tenant_id,
                employee_id=employee_id,
                user_id="user:test",
                is_active=not (failure == "owner" and employee_id == owner_id),
                role="sales" if failure == "employee" else "boss",
            )

    class Approvals:
        async def get_by_change_set(self, tenant_id, ref):
            if failure == "approval":
                return None
            return SimpleNamespace(
                change_set_ref=ref,
                approval_type="campaign_boundary_change",
                state="approved",
                expires_at=NOW + timedelta(days=1000),
                approval_id=approved.approval_id,
                decided_by_employee=boss_id,
                decided_at=NOW,
            )

    @asynccontextmanager
    async def employees(tenant):
        yield Employees()

    class Knowledge:
        async def get_detail(self, tenant, actor, document_id):
            return SimpleNamespace(
                document=SimpleNamespace(
                    tenant_id=tenant,
                    document_id=document_id,
                    status="confirmed",
                    current_revision_id="rev:test",
                    source=SimpleNamespace(artifact_id="art:test", sha256="a" * 64),
                ),
                revision=SimpleNamespace(
                    revision_id="rev:test",
                    document_id=document_id,
                    confirmed_by=boss_id,
                    confirmed_at=NOW,
                    analysis=SimpleNamespace(facts=[], inferences=[]),
                    fact_provenance=[],
                ),
            )

    dependencies = SimpleNamespace(
        employees=employees,
        employee_lookup_actor=object(),
        outreach=service,
        approvals=Approvals(),
        enterprise_knowledge=SimpleNamespace(service=Knowledge()),
    )
    authority = CurrentCustomerAgentAuthority(dependencies)
    scope = InventoryFollowupScope(
        campaign_id=view.campaign_id, campaign_version=2 if failure == "version" else 1
    )
    query = InventoryQuery(
        country="DE" if failure == "market" else "US",
        product_tags=("other" if failure == "category" else "hardware",),
    )
    if failure:
        with pytest.raises(PermissionDenied):
            await authority.authorize(TENANT, boss_id, scope, query)
    else:
        result = await authority.authorize(TENANT, boss_id, scope, query)
        assert result.owner_employee_id == owner_id
        assert result.actor_employee_id == boss_id
        assert result.actor_user_id == "user:test"
        assert result.scope == scope
