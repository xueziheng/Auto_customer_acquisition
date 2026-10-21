"""普通对话范围必须独立批准；空范围不改变旧审批语义。"""
from dataclasses import replace
from datetime import timedelta

import pytest

from apps.api.routers.campaigns import _approval_change
from domains.outreach import schemas
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from shared.schemas.identifiers import new_id
from tests.unit.test_outreach_service import NOW, SENDER, TENANT, _request, _service


def policy_input():
    policy_type = getattr(schemas, "OrdinaryDialoguePolicyInput", None)
    assert policy_type is not None, "缺少独立普通对话批准范围"
    return policy_type(
        owner_employee_id=new_id("emp"), assessment_ref="lia:business-replies",
        reply_sender_identity_ids=(SENDER,),
        product_refs=[{"product_id": new_id("prd"), "customer_view_sha256": "a" * 64}],
        expires_at=NOW + timedelta(days=7), max_rounds=3,
        max_model_calls=4, max_no_progress_rounds=2,
        max_input_bytes=16000, max_output_tokens=1000,
    )


@pytest.mark.asyncio
async def test_dialogue_scope_is_exact_approved_version_and_legacy_omits_it():
    service, *_ = _service()
    actor = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    legacy = await service.create_campaign(TENANT, _request(), actor=actor)
    before = _approval_change(legacy)
    assert "ordinary_dialogue_policy" not in before
    request = replace(_request(), ordinary_dialogue_policy=policy_input())
    revised = await service.revise_campaign(TENANT, legacy.campaign_id, request, actor=actor)
    assert revised.version == 2 and revised.approval_id is None
    policy = revised.boundary.ordinary_dialogue_policy
    assert policy.confirmed_by == actor.actor_id and policy.confirmed_at == NOW
    assert _approval_change(revised)["ordinary_dialogue_policy"]["max_rounds"] == 3
    assert _approval_change(legacy) == before
    removed = await service.revise_campaign(TENANT, legacy.campaign_id,
        replace(request, ordinary_dialogue_policy=None), actor=actor)
    assert "ordinary_dialogue_policy" not in _approval_change(removed)


@pytest.mark.parametrize("field,value", [
    ("max_rounds", 0), ("max_model_calls", 0), ("max_no_progress_rounds", 0),
    ("product_refs", []), ("confirmed_by", "emp:forged"),
    ("expires_at", NOW.replace(tzinfo=None)),
])
def test_dialogue_rejects_unbounded_or_forged_scope(field, value):
    from pydantic import ValidationError
    policy = policy_input()
    with pytest.raises(ValidationError):
        type(policy).model_validate(policy.model_dump() | {field: value})


@pytest.mark.asyncio
async def test_expired_scope_cannot_create_campaign():
    from shared.errors import ValidationError
    policy = policy_input().model_copy(update={"expires_at": NOW})
    service, *_ = _service()
    actor = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    with pytest.raises(ValidationError):
        await service.create_campaign(TENANT, replace(_request(), ordinary_dialogue_policy=policy), actor=actor)


def test_dialogue_policy_accepts_http_timestamp_without_accepting_naive_time():
    policy = policy_input()
    parsed = type(policy).model_validate(policy.model_dump(mode="json"))
    assert parsed.expires_at == policy.expires_at


async def test_current_dialogue_scope_requires_live_exact_approval_and_owner():
    from domains.outreach.schemas import CampaignApprovalSnapshot, CampaignApprovalState
    from shared.errors import ValidationError
    service, _, store, approvals, _ = _service()
    actor = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    policy = policy_input().model_copy(update={"owner_employee_id": actor.actor_id})
    created = await service.create_campaign(TENANT, replace(_request(), ordinary_dialogue_policy=policy), actor=actor)
    await service.submit_campaign(TENANT, created.campaign_id, actor=actor)
    approval = CampaignApprovalSnapshot(TENANT, created.campaign_id, 1, new_id("apr"),
        CampaignApprovalState.APPROVED, new_id("emp"), NOW)
    approvals.values[(created.campaign_id, 1)] = approval
    await service.activate_campaign(TENANT, created.campaign_id, actor=actor)
    async def current(owner=actor.actor_id, version=1):
        return await service.get_ordinary_dialogue_scope(TENANT, created.campaign_id,
            version=version, owner_employee_id=owner, actor=actor)
    assert (await current()).approval_id == approval.approval_id
    assert not store.enrollments
    with pytest.raises(ValidationError):
        await current(owner=new_id("emp"))
    from domains.outreach.errors import CampaignApprovalRequiredError
    with pytest.raises(CampaignApprovalRequiredError):
        await current(version=2)
    approvals.values[(created.campaign_id, 1)] = replace(approval, state=CampaignApprovalState.EXPIRED,
        approved_by=None, approved_at=None)
    from domains.outreach.errors import CampaignApprovalRequiredError
    with pytest.raises(CampaignApprovalRequiredError):
        await current()


async def test_primary_business_requires_independent_reply_identity_scope_and_never_cold_scope():
    from domains.outreach.errors import SendingIdentityUnavailableError
    from domains.outreach.schemas import OutreachSenderRole
    from tests.outreach_fakes import FakeSenders
    from tests.unit.test_outreach_service import SENDER, _sender
    service, trace, _, _, _ = _service()
    primary = new_id("sid")
    service._senders = FakeSenders({SENDER: _sender(),
        primary: replace(_sender(role=OutreachSenderRole.PRIMARY_BUSINESS), identity_id=primary)}, trace)
    actor = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    policy = policy_input().model_copy(update={"reply_sender_identity_ids": (primary,)})
    created = await service.create_campaign(TENANT, replace(_request(), ordinary_dialogue_policy=policy), actor=actor)
    assert _approval_change(created)["ordinary_dialogue_policy"]["reply_sender_identity_ids"] == [primary]
    assert created.boundary.sender_identity_ids == (SENDER,)
    with pytest.raises(SendingIdentityUnavailableError):
        await service.create_campaign(TENANT, replace(_request(), sender_identity_ids=(primary,), ordinary_dialogue_policy=policy), actor=actor)
    service._senders = FakeSenders({SENDER: _sender(),
        primary: replace(_sender(role=OutreachSenderRole.TRANSACTIONAL), identity_id=primary)}, trace)
    with pytest.raises(SendingIdentityUnavailableError):
        await service.create_campaign(TENANT, replace(_request(), ordinary_dialogue_policy=policy), actor=actor)
