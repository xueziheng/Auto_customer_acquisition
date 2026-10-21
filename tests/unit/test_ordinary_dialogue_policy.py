"""普通对话范围必须独立批准；空范围不改变旧审批语义。"""
from dataclasses import replace
from datetime import timedelta

import pytest

from apps.api.routers.campaigns import _approval_change
from domains.outreach import schemas
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from shared.schemas.identifiers import new_id
from tests.unit.test_outreach_service import NOW, TENANT, _request, _service


def policy_input():
    policy_type = getattr(schemas, "OrdinaryDialoguePolicyInput", None)
    assert policy_type is not None, "缺少独立普通对话批准范围"
    return policy_type(
        owner_employee_id=new_id("emp"), assessment_ref="lia:business-replies",
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
