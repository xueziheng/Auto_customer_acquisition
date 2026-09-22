"""明确独立触达任务；只调用原公开域入口与原launcher正在消费的Workflow。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from domains.approvals.service import ApprovalType, BlastRadius
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service_impl import DemandServiceImpl
from domains.outreach.errors import ContactNotEligibleError
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    CampaignCreateRequest,
    EnrollmentCreateRequest,
    SequenceStepRequest,
    StepIntent,
)
from domains.prospecting.schemas import (
    AccountResolveRequest,
    ContactCreateRequest,
    ContactPointCreateRequest,
    ContactPointKind,
    ContactType,
    LegalBasisInput,
    LegalBasisType,
    SubjectType,
    VerificationStatus,
)
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from shared.schemas.identifiers import EmployeeId, IdempotencyKey
from shared.schemas.provenance import Provenance, SourceType


async def prepare_verified_send(runtime, client, eventually):
    """人工输入企业/假设不是客户事实；验证结果只能由原VerifyContactsStep生成。"""
    deps, route, config = runtime["deps"], runtime["route"], runtime["config"]
    tenant, sid, now = route.tenant_id, route.configured_identity_id, datetime.now(UTC)
    boss_identity = config.identities[0]
    boss = Actor(
        boss_identity.employee_id, OutreachScope(level=ScopeLevel.TENANT), "boss"
    )
    provenance = Provenance(
        source_type=SourceType.EMPLOYEE_INPUT,
        source_id=boss_identity.employee_id,
        extracted_by=boss_identity.employee_id,
        extracted_at=now,
        confirmed_by=boss_identity.employee_id,
        confirmed_at=now,
    )
    account = await deps.prospecting.resolve_account(
        tenant,
        AccountResolveRequest(
            "Controlled outreach buyer",
            "KE",
            website_domain="controlled-outreach.example.com",
            entity_type="importer",
            field_provenance={key: provenance for key in ("name", "country")},
        ),
    )
    contact = await deps.prospecting.create_contact(
        tenant, ContactCreateRequest(account)
    )
    unverified = await deps.prospecting.add_contact_point(
        tenant,
        ContactPointCreateRequest(
            contact,
            ContactPointKind.EMAIL,
            "unverified@example.test",
            LegalBasisInput(
                LegalBasisType.LEGITIMATE_INTEREST,
                SubjectType.LEGAL_ENTITY,
                ContactType.PERSONAL_BUSINESS,
                "employee_input",
                now,
                assessment_ref="task12-controlled-lia",
            ),
        ),
    )
    demand = DemandServiceImpl(
        lambda t: SqlAlchemyDemandUnitOfWork(runtime["factory"], t, now=lambda: now),
        now=lambda: now,
    )
    signal = await demand.capture_signal(
        tenant,
        SignalCaptureRequest(
            "product_line_expansion",
            "Controlled outreach buyer",
            "Employee supplied controlled outreach task",
            now,
            "employee_input",
            "task12-explicit-outreach",
            "human",
        ),
    )
    hypothesis = await demand.create_hypothesis(
        tenant,
        account,
        "hinges",
        [str(signal)],
        "Employee suspects possible component demand; customer has not confirmed it.",
        "employee",
    )
    campaign = await deps.outreach.create_campaign(
        tenant,
        CampaignCreateRequest(
            "Controlled independent outreach",
            ("KE",),
            ("importer",),
            ("hinges",),
            (sid,),
            (
                SequenceStepRequest(1, StepIntent.DISCOVERY, 0),
                SequenceStepRequest(2, StepIntent.FOLLOW_UP, 7),
            ),
            5,
            5,
            (),
        ),
        actor=boss,
    )
    submitted = await deps.outreach.submit_campaign(tenant, campaign.campaign_id, actor=boss)
    assert submitted.created_by == boss_identity.employee_id
    assert submitted.version == campaign.version
    change_set_ref = f"campaign:{campaign.campaign_id}:v{submitted.version}"
    approval = await deps.approvals.submit(
        tenant,
        ApprovalType.CAMPAIGN_BOUNDARY_CHANGE,
        "受控独立触达范围",
        {"version": submitted.version},
        "核对范围",
        BlastRadius(["Campaign"], "启用", "停止", True),
        proposed_by_employee=EmployeeId(boss_identity.employee_id),
        change_set_ref=change_set_ref,
    )
    self_decision = await client.post(
        f"/approvals/{approval}/decide", json={"decision": "approve"},
    )
    assert self_decision.status_code == 400, ("campaign_self_decision", self_decision.status_code)
    assert self_decision.json()["code"] == "request_rejected"
    pending_response = await client.get(f"/approvals/{approval}")
    assert pending_response.status_code == 200
    pending = pending_response.json()
    assert pending["state"] == "pending"
    assert pending["proposed_by"] == submitted.created_by
    assert pending["decided_by_employee"] is None
    assert pending["can_current_user_decide"] is False
    decider = config.identities[1]
    assert decider.employee_id != submitted.created_by
    approved_response = await client.post(
        f"/approvals/{approval}/decide",
        json={"decision": "approve"},
        headers={"X-Employee-Id": decider.employee_id},
    )
    assert approved_response.status_code == 200
    approved = approved_response.json()
    assert approved["approval_id"] == approval
    assert approved["state"] == "approved"
    assert approved["proposed_by"] == submitted.created_by
    assert approved["decided_by_employee"] == decider.employee_id
    assert approved["change_set_ref"] == change_set_ref
    activated = await deps.outreach.activate_campaign(tenant, campaign.campaign_id, actor=boss)
    assert activated.version == submitted.version
    assert activated.approval_id == approval
    assert activated.approved_by == decider.employee_id
    runtime["campaign_approval_proof"] = {
        "campaign_id": str(activated.campaign_id),
        "campaign_version": activated.version,
        "campaign_created_by": str(activated.created_by),
        "submitter_id": boss.actor_id,
        "approval_id": approved["approval_id"],
        "change_set_ref": approved["change_set_ref"],
        "proposed_by": approved["proposed_by"],
        "decided_by": approved["decided_by_employee"],
        "self_decision_status": self_decision.status_code,
        "self_decision_code": self_decision.json()["code"],
        "state_after_self_denial": pending["state"],
        "approval_state": approved["state"],
        "campaign_approved_by": str(activated.approved_by),
    }
    assert (
        await deps.prospecting.get_contact_point(tenant, unverified)
    ).verification is VerificationStatus.UNVERIFIED
    with pytest.raises(ContactNotEligibleError):
        await deps.outreach.enroll(
            tenant,
            campaign.campaign_id,
            EnrollmentCreateRequest(
                account,
                unverified,
                IdempotencyKey("task12-unverified-denied"),
                source_hypothesis_id=hypothesis,
            ),
            actor=boss,
        )
    run_id = await deps.workflow_engine.start(
        tenant,
        "account_discovery",
        str(hypothesis),
        {
            "hypothesis_id": str(hypothesis),
            "campaign_id": str(campaign.campaign_id),
            "acting_user_id": boss_identity.user_id,
            "role_hints": ["procurement"],
            "assessment_ref": "task12-controlled-lia",
        },
        "task12-independent-contact-discovery",
    )

    async def read_run():
        return await deps.workflow_engine.get_run(tenant, run_id)

    run = await eventually(
        read_run,
        lambda r: r is not None and r.status.value in {"completed", "failed"},
        timeout=60,
    )
    assert run.status.value == "completed", (
        run.current_step,
        run.context.get("error_code"),
    )
    assert run.context["verification_counts"]["verified"] == 1
    assert len(run.context["enrollment_ids"]) == 1
    point_id = run.context["verified_contact_point_ids"][0]
    point = await deps.prospecting.get_contact_point(tenant, point_id)
    assert point.verification is VerificationStatus.VERIFIED
    enrollment_id = run.context["enrollment_ids"][0]
    system = Actor(
        "system:controlled-send",
        OutreachScope(
            level=ScopeLevel.SYSTEM, allowed_enrollment_ids=frozenset({enrollment_id})
        ),
        "system",
    )
    attempt = await deps.outreach.prepare_message_attempt(
        tenant, enrollment_id, actor=system
    )
    response = await client.post(
        f"/crm/message-attempts/{attempt.attempt_id}/send",
        json={
            "subject": "Current supply needs",
            "body": "Which components are you currently looking for?",
        },
    )
    assert response.status_code == 200
    async with runtime["factory"]() as session:
        result = (
            await session.execute(
                text(
                    "SELECT state,deterministic_message_id FROM outreach_message_attempts WHERE tenant_id=:t AND attempt_id=:a"
                ),
                {"t": tenant, "a": attempt.attempt_id},
            )
        ).one()
    assert result.state == "sent"
    runtime.update(
        outbound=result.deterministic_message_id,
        source_hypothesis_id=hypothesis,
        source_enrollment_id=enrollment_id,
        source_account_id=account,
        contact_run_id=run_id,
        contact_point_id=point_id,
        verification_provider=point.verification_provider,
        verification_status=point.verification.value,
    )
