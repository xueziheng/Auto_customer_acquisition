"""真实PG验证手动客户入组隔离自动扫描；Provider事实为受控夹具，无外发。"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from apps.api.dependencies import get_api_dependencies
from apps.composition_support.outreach_fact_readers import ProspectingDemandAccountNames
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service_impl import DemandServiceImpl
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import EnrollmentCreateRequest
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.tables import OutreachEnrollmentRow
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    new_id,
)
from tests.integration.test_customer_assignment_bridge import (
    bridge_inventory as bridge_inventory,  # noqa: PLC0414
)
from tests.integration.test_customer_followup_api import app_fixture
from tests.integration.test_outreach_concurrency import _snapshot
from tests.integration.test_outreach_enrollment_lifecycle import (
    NOW,
    MutableClock,
    _seed_active_campaign,
    _service,
)


async def ready_customer(bridge_inventory):
    app, allocation, employee, _ = await app_fixture(bridge_inventory)
    _, _, factory, tenant, _, _, _ = bridge_inventory
    campaign, approval, sender, contact = (
        CampaignId(new_id("cmp")),
        ApprovalId(new_id("apr")),
        SendingIdentityId(new_id("sid")),
        ContactPointId(new_id("cp")),
    )
    await _seed_active_campaign(factory, tenant, campaign, (sender,), approval)
    clock = MutableClock(NOW)
    contacts = {
        (contact, allocation.account_id): _snapshot(
            tenant, contact, allocation.account_id
        )
    }
    outreach = _service(factory, tenant, campaign, approval, (sender,), contacts, clock)
    application = app.dependency_overrides[get_api_dependencies]().customer_followup
    demand = DemandServiceImpl(
        lambda t: SqlAlchemyDemandUnitOfWork(factory, t, now=clock.now),
        now=clock.now,
        account_names=ProspectingDemandAccountNames(application._prospecting),
    )
    signal = await demand.capture_signal(
        tenant,
        SignalCaptureRequest(
            "product_line_expansion",
            "Fixture company",
            "new facility",
            NOW,
            "employee_input",
            "manual-test-source",
            "human",
        ),
    )
    hypothesis = await demand.create_hypothesis(
        tenant,
        allocation.account_id,
        "hardware",
        [str(signal)],
        "Facility may need components",
        "test-model",
    )
    application._outreach, application._demand = outreach, demand
    body = {
        "campaign_id": campaign,
        "contact_point_id": contact,
        "source_hypothesis_id": hypothesis,
        "campaign_version": 1,
        "execution_mode": "manual",
    }
    return SimpleNamespace(
        app=app,
        allocation=allocation,
        employee=employee,
        tenant=tenant,
        factory=factory,
        outreach=outreach,
        contacts=contacts,
        body=body,
        clock=clock,
        campaign=campaign,
        approval=approval,
        sender=sender,
    )


def client_for(h):
    return AsyncClient(
        transport=ASGITransport(app=h.app),
        base_url="http://test",
        headers={"X-Tenant-Id": h.tenant, "Idempotency-Key": "manual-first"},
    )


async def test_manual_enrollment_with_automation_disabled_is_durable_and_never_due(
    bridge_inventory,
):
    h = await ready_customer(bridge_inventory)
    async with client_for(h) as client:
        path = f"/prospects/customers/{h.allocation.id}/follow-up"
        automatic_options = await client.get(path)
        assert automatic_options.json()["execution_mode"] == "automatic"
        assert (
            "automatic_followup_disabled"
            in automatic_options.json()["blocking_reasons"]
        )
        manual_options = await client.get(path, params={"execution_mode": "manual"})
        assert manual_options.json()["execution_mode"] == "manual"
        assert (
            "automatic_followup_disabled"
            not in manual_options.json()["blocking_reasons"]
        )
        assert (
            await client.get(path, params={"execution_mode": "unknown"})
        ).status_code == 400
        assert (
            await client.post(path, json={**h.body, "execution_mode": "automatic"})
        ).status_code == 503
        response = await client.post(path, json=h.body)
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["execution_mode"] == "manual"
        assert (await client.post(path, json=h.body)).json() == first
        switched = await client.post(
            path, json={**h.body, "execution_mode": "automatic"}
        )
        assert switched.status_code == 400

    boss = Actor("boss:test", OutreachScope(level=ScopeLevel.TENANT), "boss")
    account, contact = ProspectAccountId(new_id("acc")), ContactPointId(new_id("cp"))
    h.contacts[(contact, account)] = _snapshot(h.tenant, contact, account)
    h.outreach._replies.snapshots[(contact, account)] = replace(
        next(iter(h.outreach._replies.snapshots.values())),
        contact_point_id=contact,
        account_id=account,
    )
    automatic = await h.outreach.enroll(
        h.tenant,
        h.campaign,
        EnrollmentCreateRequest(account, contact, IdempotencyKey("legacy-default")),
        actor=boss,
    )
    due = await h.outreach.list_due_sequence_enrollments(
        h.tenant, limit=100, actor=boss
    )
    assert [item.enrollment_id for item in due] == [automatic.enrollment_id]
    assert automatic.execution_mode.value == "automatic"

    system = Actor(
        "system:test",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({first["enrollment_id"]}),
        ),
        "system",
    )
    attempt = await h.outreach.prepare_message_attempt(
        h.tenant, first["enrollment_id"], actor=system
    )
    await h.outreach.claim_message_send(h.tenant, attempt.attempt_id, actor=system)
    await h.outreach.record_sent(
        h.tenant, attempt.attempt_id, "controlled-provider-ref", actor=system
    )
    h.clock.value += timedelta(days=2)
    assert [
        item.enrollment_id
        for item in await h.outreach.list_due_sequence_enrollments(
            h.tenant, limit=100, actor=boss
        )
    ] == [automatic.enrollment_id]
    async with h.factory() as session:
        row = await session.get(
            OutreachEnrollmentRow, (h.tenant, first["enrollment_id"])
        )
        assert row.execution_mode == "manual" and row.state == "in_sequence"


@pytest.mark.parametrize(
    "gate",
    [
        "unverified",
        "approval",
        "sender",
        "reply",
        "version",
        "suppression",
        "quota",
        "owner",
    ],
)
async def test_manual_enrollment_keeps_existing_admission_gates(bridge_inventory, gate):
    from domains.outreach.schemas import (
        CampaignApprovalState,
        ContactVerificationStatus,
        ReplyState,
    )

    h = await ready_customer(bridge_inventory)
    key = (h.body["contact_point_id"], h.allocation.account_id)
    if gate == "unverified":
        h.contacts[key] = replace(
            h.contacts[key],
            verification=ContactVerificationStatus.UNVERIFIED,
            verified_at=None,
        )
    elif gate == "approval":
        h.outreach._approvals.values[(h.campaign, 1)] = replace(
            h.outreach._approvals.values[(h.campaign, 1)],
            state=CampaignApprovalState.PENDING,
            approved_by=None,
            approved_at=None,
        )
    elif gate == "sender":
        h.outreach._senders.snapshots[h.sender] = replace(
            h.outreach._senders.snapshots[h.sender], sendable=False
        )
    elif gate == "reply":
        h.outreach._replies.snapshots[key] = replace(
            h.outreach._replies.snapshots[key], state=ReplyState.REPLIED, replied_at=NOW
        )
    elif gate == "version":
        h.body["campaign_version"] = 2
    elif gate == "suppression":
        from domains.outreach.schemas import SuppressionRequest, SuppressionTarget
        from domains.outreach.service import SuppressionReason

        await h.outreach.add_suppression(
            h.tenant,
            SuppressionRequest(
                SuppressionTarget(account_id=h.allocation.account_id),
                SuppressionReason.MANUAL_BLOCK,
                NOW,
                "manual-test",
                IdempotencyKey("suppress-test"),
            ),
            actor=Actor("boss:test", OutreachScope(level=ScopeLevel.TENANT), "boss"),
        )
    elif gate == "quota":
        from infra.db.tables import OutreachDailyQuotaRow

        async with h.factory() as session, session.begin():
            session.add(
                OutreachDailyQuotaRow(
                    tenant_id=h.tenant,
                    campaign_id=h.campaign,
                    on_day=NOW.date(),
                    new_contacts_reserved=5,
                    messages_reserved=0,
                )
            )
    else:
        from sqlalchemy import update

        from infra.db.tables import OwnershipLockRow

        async with h.factory() as session, session.begin():
            await session.execute(
                update(OwnershipLockRow)
                .where(
                    OwnershipLockRow.tenant_id == h.tenant,
                    OwnershipLockRow.account_id == h.allocation.account_id,
                )
                .values(owner=bridge_inventory[-1])
            )
    async with client_for(h) as client:
        response = await client.post(
            f"/prospects/customers/{h.allocation.id}/follow-up", json=h.body
        )
        assert response.status_code in {400, 409, 403}
        assert response.json()["message"] != "请求参数无效"
    async with h.factory() as session:
        assert not (
            await session.scalars(
                select(OutreachEnrollmentRow).where(
                    OutreachEnrollmentRow.tenant_id == h.tenant
                )
            )
        ).all()
