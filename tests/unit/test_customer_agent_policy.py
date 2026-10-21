"""自动准备授权必须进入精确 Campaign 版本且不能改变旧审批包。"""
from dataclasses import replace

import pytest

from apps.api.routers.campaigns import _approval_change
from domains.outreach import schemas
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from shared.schemas.identifiers import EmployeeId, new_id
from tests.unit.test_outreach_service import NOW, TENANT, _request, _service


@pytest.mark.asyncio
async def test_legacy_campaign_approval_omits_absent_customer_agent_policy():
    service, *_ = _service()
    actor = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    view = await service.create_campaign(TENANT, _request(), actor=actor)
    assert "customer_agent_policy" not in _approval_change(view)


@pytest.mark.asyncio
async def test_policy_is_bound_to_authenticated_actor_and_exact_version():
    policy_type = getattr(schemas, "CustomerAgentPolicyInput", None)
    assert policy_type is not None, "缺少可审批的自动客户准备政策"
    service, *_ = _service()
    actor = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    policy = policy_type(owner_employee_id=EmployeeId(new_id("emp")), assessment_ref="lia:corporate-2026")
    request = replace(_request(), customer_agent_policy=policy)
    view = await service.create_campaign(TENANT, request, actor=actor)
    persisted = view.boundary.customer_agent_policy
    assert persisted.confirmed_by == actor.actor_id
    assert persisted.confirmed_at == NOW
    assert persisted.owner_employee_id == policy.owner_employee_id
    assert _approval_change(view)["customer_agent_policy"]["assessment_ref"] == policy.assessment_ref
    revised = await service.revise_campaign(TENANT, view.campaign_id, replace(request, customer_agent_policy=None), actor=actor)
    assert revised.version == 2
    assert revised.approval_id is None
    assert revised.boundary.customer_agent_policy is None
    assert view.boundary.customer_agent_policy == persisted


def test_policy_rejects_claimed_confirmation_and_personal_mailbox_scope():
    from pydantic import ValidationError
    policy_type = schemas.CustomerAgentPolicyInput
    base = {"owner_employee_id": EmployeeId(new_id("emp")), "assessment_ref": "lia:corporate-2026"}
    for extra in (
        {"confirmed_by": new_id("emp")},
        {"confirmed_at": NOW},
        {"contact_type": "personal"},
        {"subject_type": "unknown"},
        {"basis": "consent"},
        {"assessment_ref": " "},
    ):
        with pytest.raises(ValidationError):
            policy_type(**(base | extra))
