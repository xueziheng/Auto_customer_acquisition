"""本人客户触达权限与旧开发任务配置权限的隔离。"""

import pytest

from domains.outreach.permissions import (
    Actor,
    OutreachAction,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import CampaignId, ProspectAccountId, TenantId

T = TenantId("tn_customer")
A = ProspectAccountId("acc_01K39P9M5D6K4A91YEQ80EJZ0X")
C = CampaignId("cmp_01K39P9M5D6K4A91YEQ80EJZ0X")


@pytest.mark.parametrize(
    "role,level", [("sales", ScopeLevel.SELF), ("manager", ScopeLevel.MANAGER)]
)
def test_customer_actions_require_exact_account_scope(role, level):
    authorizer = Phase1OutreachAuthorizer(T)
    scope = OutreachScope(
        level=level,
        allowed_account_ids=frozenset({A}),
        allowed_campaign_ids=frozenset({C}),
    )
    actor = Actor("emp_self", scope, role)
    for action in (OutreachAction.ENROLLMENT_CREATE, OutreachAction.ENROLLMENT_STOP):
        authorizer.require(actor, action, scope, T, campaign_id=C, account_id=A)
        with pytest.raises(PermissionDenied):
            authorizer.require(
                actor,
                action,
                scope,
                T,
                campaign_id=C,
                account_id=ProspectAccountId("acc_01K39P9M5D6K4A91YEQ80EJZ0Y"),
            )
        broad = OutreachScope(level=level, allowed_campaign_ids=frozenset({C}))
        with pytest.raises(PermissionDenied):
            authorizer.preauthorize(Actor("emp_self", broad, role), action, broad, T)


@pytest.mark.parametrize(
    "action",
    [
        OutreachAction.CAMPAIGN_SUBMIT,
        OutreachAction.CAMPAIGN_REVISE,
        OutreachAction.CAMPAIGN_PAUSE,
        OutreachAction.CAMPAIGN_CANCEL,
    ],
)
def test_manager_cannot_modify_approved_campaign(action):
    scope = OutreachScope(level=ScopeLevel.MANAGER, allowed_campaign_ids=frozenset({C}))
    with pytest.raises(PermissionDenied):
        Phase1OutreachAuthorizer(T).preauthorize(
            Actor("emp_manager", scope, "manager"), action, scope, T
        )


async def test_readiness_is_real_and_does_not_reserve_or_enroll():
    from dataclasses import replace

    from domains.outreach.schemas import ContactVerificationStatus
    from tests.unit.test_outreach_enrollment_service import _build

    h = _build()
    reasons = await h.service.assess_enrollment(
        h.tenant, h.campaign_id, h.account, h.contact, campaign_version=1, actor=h.boss
    )
    assert reasons == ()
    assert not h.store.enrollments and not h.store.quotas
    h.contacts.snapshots[(h.contact, h.account)] = replace(
        h.contacts.snapshots[(h.contact, h.account)],
        verification=ContactVerificationStatus.UNVERIFIED,
        verified_at=None,
    )
    reasons = await h.service.assess_enrollment(
        h.tenant, h.campaign_id, h.account, h.contact, campaign_version=1, actor=h.boss
    )
    assert "contact_not_eligible" in reasons


@pytest.mark.parametrize(
    "gate", ["approval", "sender", "reply", "contact_account", "version"]
)
async def test_readiness_blocks_real_missing_facts(gate):
    from dataclasses import replace

    from tests.unit.test_outreach_enrollment_service import _build

    h = _build()
    expected = {
        "approval": "campaign_not_approved",
        "sender": "sending_identity_unavailable",
        "reply": "reply_already_received",
        "contact_account": "contact_not_eligible",
        "version": "campaign_version_changed",
    }[gate]
    if gate == "approval":
        h.approvals.values.clear()
    elif gate == "sender":
        h.senders.snapshots = {
            i: replace(s, sendable=False) for i, s in h.senders.snapshots.items()
        }
    elif gate == "reply":
        from domains.outreach.schemas import ReplyState

        h.replies.snapshots[(h.contact, h.account)] = replace(
            h.replies.snapshots[(h.contact, h.account)],
            state=ReplyState.REPLIED,
            replied_at=h.replies.snapshots[(h.contact, h.account)].observed_at,
        )
    elif gate == "contact_account":
        h.contacts.snapshots[(h.contact, h.account)] = replace(
            h.contacts.snapshots[(h.contact, h.account)],
            contact_belongs_to_account=False,
        )
    reasons = await h.service.assess_enrollment(
        h.tenant,
        h.campaign_id,
        h.account,
        h.contact,
        campaign_version=2 if gate == "version" else 1,
        actor=h.boss,
    )
    assert expected in reasons
    assert not h.store.enrollments and not h.store.quotas


def test_manager_identity_never_inherits_direct_report_customers():
    from apps.api.identity import _opportunity_actor
    from domains.employees.schemas import EmployeeView
    from shared.schemas.identifiers import EmployeeId

    manager = EmployeeView(
        employee_id=EmployeeId("emp_manager"), tenant_id=T, name="经理", role="manager"
    )
    report = EmployeeView(
        employee_id=EmployeeId("emp_report"),
        tenant_id=T,
        name="员工",
        role="sales",
        manager_id=manager.employee_id,
    )
    actor = _opportunity_actor(manager, [manager, report])
    assert actor.scope.allowed_owners == frozenset({manager.employee_id})


async def test_enrollment_rechecks_current_approval_instead_of_trusting_active_state():
    from domains.outreach.errors import CampaignApprovalRequiredError
    from domains.outreach.schemas import EnrollmentCreateRequest
    from shared.schemas.identifiers import IdempotencyKey
    from tests.unit.test_outreach_enrollment_service import _build

    h = _build()
    h.approvals.values.clear()
    with pytest.raises(CampaignApprovalRequiredError):
        await h.service.enroll(
            h.tenant,
            h.campaign_id,
            EnrollmentCreateRequest(h.account, h.contact, IdempotencyKey("unapproved")),
            actor=h.boss,
        )
    assert not h.store.enrollments and not h.store.quotas
