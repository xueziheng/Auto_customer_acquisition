"""私有轮次只能排除可信旧轮次；正常客户、未知关联与本轮回复继续拦截。"""
from dataclasses import replace

import pytest

from domains.outreach.errors import ReplyAlreadyReceivedError
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.private_reply_round import private_round_has_reply
from domains.outreach.schemas import (
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    EnrollmentExecutionMode,
    PrivateReplyTestHistory,
    PrivateReplyTestMessage,
    PrivateReplyTestPermission,
    ReplyState,
    ReplyStatusSnapshot,
)
from domains.outreach.service_impl import OutreachServiceImpl
from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import new_id
from tests.outreach_fakes import FakeAuthorizer, FakeUowFactory
from tests.unit.test_outreach_enrollment_service import NOW, _build, _contact


class History:
    def __init__(self, value):
        self.value, self.calls = value, 0

    async def read(self, tenant_id, contact_point_id, account_id):
        self.calls += 1
        return self.value


def setup():
    h = _build()
    owner = new_id("emp")
    actor = Actor(owner, OutreachScope(level=ScopeLevel.TENANT), "boss")
    v = h.store.versions[(h.campaign_id, 1)]
    h.store.versions[(h.campaign_id, 1)] = replace(v, boundary=replace(
        v.boundary, steps=v.boundary.steps[:1], sender_identity_ids=(h.sender_a,),
    ))
    h.contacts.snapshots[(h.contact, h.account)] = _contact(
        h.tenant, h.contact, h.account,
        verification=ContactVerificationStatus.UNVERIFIED, verified_at=None,
        legal_basis=ContactLegalBasis.CONSENT,
        private_reply_test=PrivateReplyTestPermission(h.sender_a, owner),
    )
    original = ReplyStatusSnapshot(h.tenant, h.contact, h.account, ReplyState.REPLIED, NOW, NOW)
    h.replies.snapshots[(h.contact, h.account)] = original
    history = History(PrivateReplyTestHistory(
        h.tenant, h.contact, h.account,
        (PrivateReplyTestMessage(new_id("msg"), "willing_to_continue", new_id("cmp"), True),),
        NOW,
    ))
    h.service = OutreachServiceImpl(
        FakeUowFactory(h.store, h.trace), h.contacts, h.senders,
        h.approvals, h.replies, FakeAuthorizer(h.trace), h.audit,
        now=lambda: NOW, private_reply_history=history,
    )
    request = EnrollmentCreateRequest(
        h.account, h.contact, "new-private-round", execution_mode=EnrollmentExecutionMode.MANUAL,
    )
    return h, actor, history, request, original


async def test_old_private_reply_allows_new_round_and_rechecks_before_send():
    h, actor, history, request, _ = setup()
    enrollment = await h.service.enroll(h.tenant, h.campaign_id, request, actor=actor)
    same = await h.service.enroll(h.tenant, h.campaign_id, request, actor=actor)
    assert same.enrollment_id == enrollment.enrollment_id
    prepared = await h.service.prepare_message_attempt(h.tenant, enrollment.enrollment_id, actor=actor)
    await h.service.preflight_message_send(h.tenant, prepared.attempt_id, actor=actor)
    history.value = replace(history.value, messages=(
        replace(history.value.messages[0], source_campaign_id=h.campaign_id),
    ))
    with pytest.raises(ReplyAlreadyReceivedError):
        await h.service.preflight_message_send(h.tenant, prepared.attempt_id, actor=actor)
    assert len(h.store.enrollments) == 1


async def test_new_round_reply_still_stops_prepare():
    h, actor, history, request, _ = setup()
    enrollment = await h.service.enroll(h.tenant, h.campaign_id, request, actor=actor)
    history.value = replace(history.value, messages=(
        replace(history.value.messages[0], source_campaign_id=h.campaign_id),
    ))
    with pytest.raises(ReplyAlreadyReceivedError):
        await h.service.prepare_message_attempt(h.tenant, enrollment.enrollment_id, actor=actor)
    assert not h.store.attempts


@pytest.mark.parametrize("invalid", [
    "ordinary", "automatic", "owner", "consent", "unapproved",
    "unlinked", "not_sent", "unknown", "needs_review", "unsubscribe",
    "complaint", "bounce", "rejection", "current", "empty",
    "foreign_tenant", "foreign_account", "foreign_contact",
])
async def test_repeat_round_does_not_bypass_other_guards(invalid):
    h, actor, history, request, _ = setup()
    contact = h.contacts.snapshots[(h.contact, h.account)]
    record = history.value.messages[0]
    if invalid == "ordinary":
        h.contacts.snapshots[(h.contact, h.account)] = _contact(h.tenant, h.contact, h.account)
    elif invalid == "automatic":
        request = replace(request, execution_mode=EnrollmentExecutionMode.AUTOMATIC)
    elif invalid == "owner":
        actor = replace(actor, actor_id=new_id("emp"))
    elif invalid == "consent":
        h.contacts.snapshots[(h.contact, h.account)] = replace(contact, legal_basis=ContactLegalBasis.LEGITIMATE_INTEREST)
    elif invalid == "unapproved":
        h.approvals.values.clear()
    elif invalid in {"foreign_tenant", "foreign_account", "foreign_contact"}:
        field, prefix = {
            "foreign_tenant": ("tenant_id", "tn"),
            "foreign_account": ("account_id", "acc"),
            "foreign_contact": ("contact_point_id", "cp"),
        }[invalid]
        history.value = replace(history.value, **{field: new_id(prefix)})
    elif invalid == "empty":
        history.value = replace(history.value, messages=())
    else:
        if invalid == "unlinked": record = replace(record, source_campaign_id=None)
        elif invalid == "not_sent": record = replace(record, source_is_private_sent=False)
        elif invalid == "current": record = replace(record, source_campaign_id=h.campaign_id)
        else: record = replace(record, category=None if invalid == "unknown" else invalid)
        history.value = replace(history.value, messages=(record,))
    with pytest.raises(TradeOSError):
        await h.service.enroll(h.tenant, h.campaign_id, request, actor=actor)
    assert not h.store.enrollments
    if invalid == "ordinary":
        assert history.calls == 0


def test_automatic_reply_does_not_stop_new_private_round():
    h, _, history, _, original = setup()
    current_auto = replace(history.value.messages[0], message_id=new_id("msg"), source_campaign_id=h.campaign_id, category="auto_reply")
    value = replace(history.value, messages=history.value.messages + (current_auto,))
    assert private_round_has_reply(original, value, h.campaign_id) is False


def test_history_requires_typed_records():
    _h, _, history, _, _ = setup()
    with pytest.raises(ValidationError):
        replace(history.value, messages=({"source_is_private_sent": True},))
