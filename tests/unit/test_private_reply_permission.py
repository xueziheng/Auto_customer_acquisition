"""私有测试用途不能放宽普通未验证联系人或自动发送边界。"""
from dataclasses import replace

import pytest

from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    EnrollmentExecutionMode,
    PrivateReplyTestPermission,
)
from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import new_id
from tests.unit.test_outreach_enrollment_service import _build, _contact


@pytest.mark.parametrize("invalid", ["ordinary", "automatic", "hypothesis", "other_owner",
    "other_sender", "many_senders", "many_steps", "no_consent", "verified", "unapproved", "foreign_tenant", "foreign_contact"])
async def test_private_permission_is_exact_and_fail_closed(invalid):
    h = _build()
    owner = new_id("emp")
    actor = Actor(owner, OutreachScope(level=ScopeLevel.TENANT), "boss")
    permission = PrivateReplyTestPermission(h.sender_a, owner)
    snapshot = _contact(h.tenant, h.contact, h.account,
        verification=ContactVerificationStatus.UNVERIFIED, verified_at=None,
        legal_basis=ContactLegalBasis.CONSENT, private_reply_test=permission)
    v = h.store.versions[(h.campaign_id, 1)]
    boundary = replace(v.boundary, steps=v.boundary.steps[:1], sender_identity_ids=(h.sender_a,))
    mode, hypothesis = EnrollmentExecutionMode.MANUAL, None
    if invalid == "ordinary": snapshot = replace(snapshot, private_reply_test=None)
    if invalid == "automatic": mode = EnrollmentExecutionMode.AUTOMATIC
    if invalid == "hypothesis": hypothesis = new_id("hyp")
    if invalid == "other_owner": actor = replace(actor, actor_id=new_id("emp"))
    if invalid == "other_sender": boundary = replace(boundary, sender_identity_ids=(h.sender_b,))
    if invalid == "many_senders": boundary = replace(boundary, sender_identity_ids=(h.sender_a, h.sender_b))
    if invalid == "many_steps": boundary = replace(boundary, steps=v.boundary.steps)
    if invalid == "no_consent": snapshot = replace(snapshot, legal_basis=ContactLegalBasis.LEGITIMATE_INTEREST)
    if invalid == "verified":
        from tests.unit.test_outreach_enrollment_service import NOW
        snapshot = replace(snapshot, verification=ContactVerificationStatus.VERIFIED, verified_at=NOW)
    if invalid == "unapproved": h.approvals.values.clear()
    if invalid == "foreign_tenant": snapshot = replace(snapshot, tenant_id=new_id("tn"))
    if invalid == "foreign_contact": snapshot = replace(snapshot, contact_point_id=new_id("cp"))
    h.store.versions[(h.campaign_id, 1)] = replace(v, boundary=boundary)
    h.contacts.snapshots[(h.contact, h.account)] = snapshot
    request = EnrollmentCreateRequest(h.account, h.contact, "private-fixture",
        execution_mode=mode, source_hypothesis_id=hypothesis)
    with pytest.raises(TradeOSError):
        await h.service.enroll(h.tenant, h.campaign_id, request, actor=actor)
    assert not h.store.enrollments

def test_private_permission_requires_typed_internal_snapshot():
    h = _build()
    with pytest.raises(ValidationError):
        _contact(h.tenant, h.contact, h.account, private_reply_test={"owner_id":"forged"})

@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_private_prepare_requires_explicit_boolean_consent(value):
    from domains.prospecting.private_reply_test import PrivateReplyTestPrepare
    with pytest.raises(ValueError):
        PrivateReplyTestPrepare(campaign_id=new_id("cmp"), sending_identity_id=new_id("sid"),
            confirm_controlled_recipient=value)
