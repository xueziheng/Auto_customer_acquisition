"""跟进应用重查当前身份、真实归属和同客户证据，不伪造就绪。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.api.composition.customer_followup import CustomerFollowupApplication
from domains.prospecting.customer_followup import (
    CustomerContext,
    CustomerFollowupCommand,
)
from domains.prospecting.inventory import CandidateAllocation
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import ProspectAccountId
from tests.unit.test_prospect_inventory import candidate
from tests.unit.test_prospect_inventory_api import EMPLOYEE, TENANT, setup

A = ProspectAccountId("acc_01K39P9M5D6K4A91YEQ80EJZ0X")
H = "hyp_01K39P9M5D6K4A91YEQ80EJZ0X"
C = "cmp_01K39P9M5D6K4A91YEQ80EJZ0X"
P = "cp_01K39P9M5D6K4A91YEQ80EJZ0X"


def fixture(role="sales", owner=EMPLOYEE, account=A, active=True, preparation=None):
    from tests.unit.test_prospect_inventory_api import NOW

    app, _ = setup(role=role)
    identity = app.dependency_overrides[
        next(
            k for k in app.dependency_overrides if k.__name__ == "get_request_identity"
        )
    ]()
    if hasattr(identity, "__await__"):
        raise AssertionError("fixture身份应同步")
    allocation = CandidateAllocation(
        tenant_id=TENANT,
        id="cal_test",
        employee_id=owner,
        candidate=candidate(),
        allocated_at=NOW,
        account_id=account,
        request_key="private-key",
    )
    ctx = CustomerContext(
        TENANT, EMPLOYEE, identity.employee.user_id, role, active, allocation, owner, preparation
    )

    class Reader:
        @asynccontextmanager
        async def guard(self, tenant, employee, allocation_id):
            yield ctx

        async def active_campaign_ids(self, tenant):
            return (C,)

    prospecting = SimpleNamespace(
        get_account=AsyncMock(return_value=SimpleNamespace(entity_type=None)),
        list_contacts_for_account=AsyncMock(return_value=[]),
    )
    demand = SimpleNamespace(
        get_hypothesis=AsyncMock(
            return_value=SimpleNamespace(
                account_id=A, status="inferred", evidence=["real"], category="hinges"
            )
        ),
        list_account_outreach_hypotheses=AsyncMock(return_value=[]),
    )
    outreach = SimpleNamespace(
        replay_enrollment=AsyncMock(return_value=None),
        enroll=AsyncMock(),
        stop_enrollment=AsyncMock(),
        get_campaign=AsyncMock(
            return_value=SimpleNamespace(
                state=SimpleNamespace(value="active"),
                version=1,
                boundary=SimpleNamespace(allowed_categories=("hinges",)),
            )
        ),
    )
    return (
        CustomerFollowupApplication(
            Reader(), prospecting, demand, outreach, automatic_enabled=True
        ),
        identity,
        prospecting,
        demand,
        outreach,
    )


@pytest.mark.parametrize("role", ["sales", "manager"])
async def test_other_employee_and_inactive_cannot_read_customer(role):
    application, identity, prospecting, _, _ = fixture(role=role, owner="emp_other")
    with pytest.raises(PermissionDenied):
        await application.detail(identity, "cal_test")
    prospecting.get_account.assert_not_called()
    application, identity, prospecting, _, _ = fixture(role=role, active=False)
    with pytest.raises(PermissionDenied):
        await application.detail(identity, "cal_test")
    prospecting.get_account.assert_not_called()


async def test_foreign_hypothesis_rejected_before_outreach():
    application, identity, _, demand, outreach = fixture()
    demand.get_hypothesis.return_value.account_id = "acc_other"
    with pytest.raises(ValidationError):
        await application.start(
            identity,
            "cal_test",
            CustomerFollowupCommand(
                campaign_id=C,
                contact_point_id=P,
                source_hypothesis_id=H,
                campaign_version=1,
            ),
            "original-key",
        )
    outreach.enroll.assert_not_called()


async def test_historical_assigned_record_stays_pending_without_side_effects():
    application, identity, prospecting, demand, outreach = fixture(account=None)
    detail = await application.detail(identity, "cal_test")
    assert detail.account is None and detail.owner == EMPLOYEE
    assert "account_pending" in detail.blocking_reasons
    assert "request_key" not in detail.model_dump()["allocation"]
    prospecting.get_account.assert_not_called()
    demand.get_hypothesis.assert_not_called()
    outreach.enroll.assert_not_called()


async def test_real_enrollment_keeps_reply_source_and_replays_original_key():
    from apps.api.composition.customer_followup import _actor
    from domains.outreach.permissions import Phase1OutreachAuthorizer
    from tests.unit.test_outreach_enrollment_service import _build

    h = _build()
    h.service._authorizer = Phase1OutreachAuthorizer(h.tenant)
    application, identity, _, demand, _ = fixture()
    identity = replace(identity, tenant_id=h.tenant)
    from tests.unit.test_prospect_inventory_api import NOW

    allocation = CandidateAllocation(
        tenant_id=h.tenant,
        id="cal_actual",
        employee_id=EMPLOYEE,
        candidate=candidate(),
        allocated_at=NOW,
        account_id=h.account,
        request_key="internal",
    )
    context = CustomerContext(
        h.tenant,
        EMPLOYEE,
        identity.employee.user_id,
        "sales",
        True,
        allocation,
        EMPLOYEE,
    )

    class Reader:
        @asynccontextmanager
        async def guard(self, *args):
            yield context

    demand.get_hypothesis.return_value.account_id = h.account
    demand.get_hypothesis.return_value.category = "hardware"
    demand.get_hypothesis_for_discovery = AsyncMock()
    application._reader, application._outreach = Reader(), h.service
    command = CustomerFollowupCommand(
        campaign_id=h.campaign_id,
        contact_point_id=h.contact,
        source_hypothesis_id=H,
        campaign_version=1,
    )
    first = await application.start(
        identity, allocation.id, command, "original-unknown-result-key"
    )
    again = await application.start(
        identity, allocation.id, command, "original-unknown-result-key"
    )
    assert again == first and len(h.store.enrollments) == 1
    assert first.source_hypothesis_id == H
    source = await h.service.get_enrollment(
        h.tenant, first.enrollment_id, actor=_actor(context, h.account)
    )
    assert source.source_hypothesis_id == H and source.account_id == h.account
    stopped = await application.stop(identity, allocation.id, first.enrollment_id)
    assert stopped.state == "stopped_manual" and stopped.source_hypothesis_id == H
    assert (
        await application.stop(identity, allocation.id, first.enrollment_id) == stopped
    )


async def test_stop_foreign_enrollment_and_campaign_category_rejected():
    application, identity, _, demand, outreach = fixture()
    outreach.get_enrollment = AsyncMock(
        return_value=SimpleNamespace(account_id="acc_foreign")
    )
    with pytest.raises(PermissionDenied):
        await application.stop(identity, "cal_test", "enr_test")
    outreach.stop_enrollment.assert_not_called()
    demand.get_hypothesis_for_discovery = AsyncMock()
    demand.get_hypothesis.return_value.category = "packaging"
    from shared.errors import PolicyViolation

    with pytest.raises(PolicyViolation):
        await application.start(
            identity,
            "cal_test",
            CustomerFollowupCommand(
                campaign_id=C,
                contact_point_id=P,
                source_hypothesis_id=H,
                campaign_version=1,
            ),
            "key",
        )
    outreach.enroll.assert_not_called()


async def test_transferred_real_owner_blocks_old_allocation_owner_private_reads():
    from dataclasses import replace

    application, identity, prospecting, _, outreach = fixture()
    async with application._reader.guard(TENANT, EMPLOYEE, "cal_test") as context:
        transferred = replace(context, ownership_owner="emp_new_owner")

    class Reader:
        @asynccontextmanager
        async def guard(self, *args):
            yield transferred

    application._reader = Reader()
    with pytest.raises(PermissionDenied):
        await application.detail(identity, "cal_test")
    with pytest.raises(PermissionDenied):
        await application.options(identity, "cal_test")
    prospecting.get_account.assert_not_called()
    outreach.enroll.assert_not_called()


async def test_customer_list_invalid_cursor_is_a_client_error():
    from httpx import ASGITransport, AsyncClient

    from apps.api.dependencies import get_api_dependencies

    app, inventory = setup(role="sales")
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        customer_followup=SimpleNamespace(access=AsyncMock())
    )
    inventory.list_allocations = AsyncMock(side_effect=ValueError("无效的历史游标"))
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get("/prospects/customers?cursor=missing")
    assert response.status_code == 400


@pytest.mark.parametrize("execution_mode", ["automatic", "manual"])
async def test_options_can_start_only_after_real_current_gates_pass(execution_mode):
    from dataclasses import replace

    from domains.outreach.permissions import Phase1OutreachAuthorizer
    from domains.prospecting.schemas import (
        ContactPointDetailView,
        ContactPointKind,
        ContactPointView,
        ContactType,
        LegalBasisType,
        ProspectAccountView,
        ProspectContactDetailView,
        ProspectContactView,
        SubjectType,
        VerificationStatus,
    )
    from shared.schemas.identifiers import new_id
    from tests.unit.test_outreach_enrollment_service import NOW, _build

    h = _build()
    h.service._authorizer = Phase1OutreachAuthorizer(h.tenant)
    application, identity, prospecting, demand, _ = fixture()
    identity = replace(
        identity,
        tenant_id=h.tenant,
        employee=replace(identity.employee, tenant_id=h.tenant),
    )
    allocation = CandidateAllocation(
        tenant_id=h.tenant,
        id="cal_readiness",
        employee_id=EMPLOYEE,
        candidate=candidate(),
        allocated_at=NOW,
        account_id=h.account,
        request_key="internal",
    )
    context = CustomerContext(
        h.tenant,
        EMPLOYEE,
        identity.employee.user_id,
        "sales",
        True,
        allocation,
        EMPLOYEE,
    )

    class Reader:
        @asynccontextmanager
        async def guard(self, *args):
            yield context

        async def active_campaign_ids(self, tenant_id):
            return (h.campaign_id,)

    application._reader, application._outreach = Reader(), h.service
    prospecting.get_account.return_value = ProspectAccountView(
        h.account, h.tenant, "测试客户", "US", NOW, entity_type="importer"
    )
    contact_id = new_id("pc")
    contact = ProspectContactView(contact_id, h.tenant, h.account, NOW)
    cp = ContactPointView(
        h.contact,
        h.tenant,
        contact_id,
        h.account,
        ContactPointKind.EMAIL,
        "buyer@example.test",
        VerificationStatus.VERIFIED,
        NOW,
        verified_at=NOW,
    )
    detail = ContactPointDetailView(
        cp,
        LegalBasisType.LEGITIMATE_INTEREST,
        SubjectType.LEGAL_ENTITY,
        ContactType.PERSONAL_BUSINESS,
        "test-reviewed-basis",
        NOW,
        assessment_ref="reviewed-test-lia",
    )
    prospecting.list_contacts_for_account.return_value = [
        ProspectContactDetailView(contact, (detail,))
    ]
    demand.list_account_outreach_hypotheses.return_value = [
        SimpleNamespace(
            hypothesis_id=H,
            account_id=h.account,
            category="hardware",
            status="inferred",
            evidence=["controlled-real-source"],
        )
    ]
    application._automatic_enabled = execution_mode == "automatic"
    options = await application.options(identity, allocation.id, execution_mode=execution_mode)
    assert options.campaigns[0].can_start and options.campaigns[
        0
    ].eligible_contact_point_ids == (h.contact,)
    assert (
        options.blocking_reasons == ()
        and not h.store.enrollments
        and not h.store.quotas
    )
    h.approvals.values.clear()
    options = await application.options(identity, allocation.id, execution_mode=execution_mode)
    assert (
        not options.campaigns[0].can_start
        and "campaign_not_approved" in options.campaigns[0].blocking_reasons
    )

@pytest.mark.parametrize("reply_fact", ["replied", "unknown", "unavailable"])
async def test_customer_start_rechecks_reply_and_preserves_original_key_replay(reply_fact):
    from domains.outreach.errors import (
        OutreachProviderUnavailableError,
        ReplyAlreadyReceivedError,
    )
    from domains.outreach.permissions import Phase1OutreachAuthorizer
    from domains.outreach.schemas import ReplyState
    from shared.errors import TransientError
    from tests.unit.test_outreach_enrollment_service import NOW, _build
    h = _build()
    h.service._authorizer = Phase1OutreachAuthorizer(h.tenant)
    application, identity, _, demand, _ = fixture()
    identity = replace(identity, tenant_id=h.tenant, employee=replace(identity.employee, tenant_id=h.tenant))
    allocation = CandidateAllocation(tenant_id=h.tenant, id="cal_reply_guard", employee_id=EMPLOYEE, candidate=candidate(), allocated_at=NOW, account_id=h.account, request_key="internal")
    context = CustomerContext(h.tenant, EMPLOYEE, identity.employee.user_id, "sales", True, allocation, EMPLOYEE)
    class Reader:
        @asynccontextmanager
        async def guard(self, *args):
            yield context
    application._reader, application._outreach = Reader(), h.service
    demand.get_hypothesis.return_value.account_id = h.account
    demand.get_hypothesis.return_value.category = "hardware"
    demand.get_hypothesis_for_discovery = AsyncMock()
    command = CustomerFollowupCommand(campaign_id=h.campaign_id, contact_point_id=h.contact, source_hypothesis_id=H, campaign_version=1)
    original_snapshot = h.replies.snapshots[(h.contact, h.account)]
    original_reader = h.replies.get_reply_status
    async def unavailable(*args):
        raise TransientError("当前回复等待分类" if reply_fact == "unknown" else "controlled unreadable reply")
    def block_reply():
        if reply_fact in {"unknown", "unavailable"}:
            h.replies.get_reply_status = unavailable
        else:
            h.replies.snapshots[(h.contact, h.account)] = replace(original_snapshot, state=ReplyState(reply_fact), replied_at=NOW if reply_fact == "replied" else None)
    block_reply()
    expected = ReplyAlreadyReceivedError if reply_fact == "replied" else OutreachProviderUnavailableError
    with pytest.raises(expected):
        await application.start(identity, allocation.id, command, "original-reply-key")
    assert not h.store.enrollments and not h.store.quotas
    h.replies.get_reply_status = original_reader
    h.replies.snapshots[(h.contact, h.account)] = original_snapshot
    first = await application.start(identity, allocation.id, command, "original-reply-key")
    quota = dict(h.store.quotas)
    block_reply()
    assert await application.start(identity, allocation.id, command, "original-reply-key") == first
    assert first.source_hypothesis_id == H and h.store.quotas == quota and len(h.store.enrollments) == 1


@pytest.mark.parametrize(
    "change",
    [
        "validated",
        "evidence",
        "campaign_paused",
        "campaign_version",
        "contact",
        "automatic_disabled",
        "approval",
    ],
)
async def test_original_result_survives_changed_admission_facts_but_new_key_does_not(
    change,
):
    from apps.api.composition.customer_followup import _actor
    from domains.outreach.errors import (
        CampaignApprovalRequiredError,
        CampaignNotActiveError,
        ContactNotEligibleError,
        IdempotencyConflictError,
    )
    from domains.outreach.permissions import Phase1OutreachAuthorizer
    from domains.outreach.schemas import (
        ContactVerificationStatus,
        EnrollmentCreateRequest,
    )
    from shared.errors import TransientError
    from tests.unit.test_outreach_enrollment_service import CampaignState, _build

    h = _build()
    h.service._authorizer = Phase1OutreachAuthorizer(h.tenant)
    application, identity, _, demand, _ = fixture()
    identity = replace(identity, tenant_id=h.tenant)
    from tests.unit.test_prospect_inventory_api import NOW

    allocation = CandidateAllocation(
        tenant_id=h.tenant,
        id="cal_recovery",
        employee_id=EMPLOYEE,
        candidate=candidate(),
        allocated_at=NOW,
        account_id=h.account,
        request_key="internal",
    )
    context = CustomerContext(
        h.tenant,
        EMPLOYEE,
        identity.employee.user_id,
        "sales",
        True,
        allocation,
        EMPLOYEE,
    )

    class Reader:
        @asynccontextmanager
        async def guard(self, *args):
            yield context

    application._reader, application._outreach = Reader(), h.service
    demand.get_hypothesis.return_value.account_id = h.account
    demand.get_hypothesis.return_value.category = "hardware"
    demand.get_hypothesis_for_discovery = AsyncMock()
    command = CustomerFollowupCommand(
        campaign_id=h.campaign_id,
        contact_point_id=h.contact,
        source_hypothesis_id=H,
        campaign_version=1,
    )
    # 首次响应在transport丢失的恢复依据是实际持久Enrollment，不是当前选项。
    first = await application.start(
        identity, allocation.id, command, "lost-first-response"
    )
    quotas, actions = dict(h.store.quotas), dict(h.store.actions)
    if change == "validated":
        demand.get_hypothesis.return_value.status = "validated"
    elif change == "evidence":
        demand.get_hypothesis.return_value.evidence = []
    elif change == "campaign_paused":
        h.store.campaigns[h.campaign_id].state = CampaignState.PAUSED
    elif change == "campaign_version":
        h.store.campaigns[h.campaign_id].current_version = 2
        h.store.versions[(h.campaign_id, 2)] = replace(
            h.store.versions[(h.campaign_id, 1)], version=2
        )
    elif change == "contact":
        original = h.contacts.snapshots[(h.contact, h.account)]
        h.contacts.snapshots[(h.contact, h.account)] = replace(
            original,
            verification=ContactVerificationStatus.UNVERIFIED,
            verified_at=None,
        )
    elif change == "automatic_disabled":
        application._automatic_enabled = False
    elif change == "approval":
        h.approvals.values.clear()
    assert (
        await application.start(identity, allocation.id, command, "lost-first-response")
        == first
    )
    for field, value in [
        ("source_hypothesis_id", "hyp_01K39P9M5D6K4A91YEQ80EJZ0Y"),
        ("campaign_version", 2),
        ("contact_point_id", "cp_01K39P9M5D6K4A91YEQ80EJZ0Y"),
        ("campaign_id", "cmp_01K39P9M5D6K4A91YEQ80EJZ0Y"),
    ]:
        with pytest.raises(IdempotencyConflictError):
            await application.start(
                identity,
                allocation.id,
                command.model_copy(update={field: value}),
                "lost-first-response",
            )
    expected_error = {
        "validated": ValidationError,
        "evidence": ValidationError,
        "campaign_paused": CampaignNotActiveError,
        "campaign_version": CampaignNotActiveError,
        "contact": ContactNotEligibleError,
        "automatic_disabled": TransientError,
        "approval": CampaignApprovalRequiredError,
    }[change]
    with pytest.raises(expected_error):
        await application.start(
            identity, allocation.id, command, "new-key-cannot-recover"
        )
    request = EnrollmentCreateRequest(h.account, h.contact, "lost-first-response", H, 1)
    actor = _actor(context, h.account, h.campaign_id)
    direct = await h.service.enroll(h.tenant, h.campaign_id, request, actor=actor)
    assert (
        direct.enrollment_id == first.enrollment_id
        and direct.campaign_version == 1
        and direct.source_hypothesis_id == H
    )
    for invalid_context in [
        replace(context, is_active=False),
        replace(context, ownership_owner="emp_new"),
        replace(context, tenant_id=TENANT),
        replace(context, user_id="usr_changed"),
    ]:
        original_context, context = context, invalid_context
        with pytest.raises(PermissionDenied):
            await application.start(
                identity, allocation.id, command, "lost-first-response"
            )
        context = original_context
    assert (
        len(h.store.enrollments) == 1
        and h.store.quotas == quotas
        and h.store.actions == actions
    )
    assert not h.store.attempts

async def test_preparation_projection_is_inside_existing_customer_guard():
    from domains.prospecting.customer_followup import CustomerPreparationStatus
    status = CustomerPreparationStatus(state="blocked", stage="verify", blocked_reason="external_reconciliation_required", campaign_id=C, campaign_version=1)
    application, identity, prospecting, _, _ = fixture(account=None, preparation=status)
    detail = await application.detail(identity, "cal_test")
    assert detail.preparation == status
    assert set(detail.preparation.model_dump()) == {"state", "stage", "blocked_reason", "campaign_id", "campaign_version"}
    application, identity, prospecting, _, _ = fixture(account=None, owner="emp_other", preparation=status)
    with pytest.raises(PermissionDenied):
        await application.detail(identity, "cal_test")
    prospecting.get_account.assert_not_called()
