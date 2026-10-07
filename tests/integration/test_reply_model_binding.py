"""真实 Postgres 关联读取；错配和跨租户不得获得模型调用身份。"""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.tables import (
    ConversationRow,
    MessageRow,
    ModelInvocationRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    WorkflowRunRow,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from shared.schemas.model_invocation import ModelGenerationError
from tests.integration.test_reply_qualification_workflow import _seed_campaign

NOW = datetime(2026, 10, 5, tzinfo=UTC)
OUTBOUND = "<binding." + "a" * 64 + "@messages.tradeos.invalid>"


async def seed_binding(engine, *, case="valid"):
    """受控存储事实，不发信；关联读取器保持真实 SQL 和独立连接。"""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    message, run, enrollment = new_id("msg"), new_id("run"), new_id("enr")
    conversation = new_id("conv")
    account, contact = new_id("acc"), new_id("cp")
    sender = SendingIdentityId(new_id("sid"))
    await _seed_campaign(sessions, tenant, campaign, sender, ApprovalId(new_id("apr")))
    context = {
        "message_id": message,
        "outbound_message_id": OUTBOUND,
        "enrollment_id": enrollment,
        "account_id": account,
        "contact_point_id": contact,
    }
    if case.startswith("context_"):
        context[case.removeprefix("context_")] = "forged"
    run_values = {
        "tenant_id": tenant,
        "run_id": run,
        "workflow_type": "reply_qualification",
        "workflow_version": 1,
        "subject_ref": message,
        "current_step": "classify",
        "status": "pending",
        "context": context,
        "idempotency_key": f"reply:{message}",
    }
    for field, value in [
        ("workflow_type", "assistant"),
        ("current_step", "apply_actions"),
        ("status", "completed"),
        ("subject_ref", "forged"),
        ("idempotency_key", "forged"),
    ]:
        if case == field:
            run_values[field] = value
    async with sessions.begin() as db:
        db.add(
            ConversationRow(
                tenant_id=tenant,
                conversation_id=conversation,
                account_id="acc_other" if case == "conversation_account" else account,
                channel="email",
                created_at=NOW,
            )
        )
        await db.flush()
        if case != "missing_message":
            db.add(
                MessageRow(
                    tenant_id=tenant,
                    message_id=message,
                    conversation_id=conversation,
                    direction="outbound" if case == "direction" else "inbound",
                    sent_at=NOW,
                    raw_artifact_ref="raw_binding",
                    external_message_id="<reply@example.test>",
                    outbound_message_id=None
                    if case == "missing_outbound"
                    else OUTBOUND,
                )
            )
        if case != "missing_enrollment":
            db.add(
                OutreachEnrollmentRow(
                    tenant_id=tenant,
                    enrollment_id=enrollment,
                    campaign_id=campaign,
                    campaign_version=1,
                    account_id=account,
                    contact_point_id=contact,
                    sending_identity_id=sender,
                    state="in_sequence",
                    current_step=1,
                    enrolled_at=NOW,
                    idempotency_key="enrollment",
                )
            )
            await db.flush()
            if case != "missing_attempt":
                db.add(
                    OutreachMessageAttemptRow(
                        tenant_id=tenant,
                        attempt_id=new_id("att"),
                        message_id=new_id("out"),
                        campaign_id=campaign,
                        enrollment_id=enrollment,
                        campaign_version=1,
                        step_number=1,
                        sending_identity_id=sender,
                        idempotency_key="attempt",
                        state="sent",
                        provider_ref="controlled",
                        created_at=NOW,
                        updated_at=NOW,
                        deterministic_message_id=OUTBOUND,
                        idempotency_header="binding." + "a" * 64,
                    )
                )
        db.add(WorkflowRunRow(**run_values))
        if case == "duplicate_subject":
            db.add(
                WorkflowRunRow(
                    **{
                        **run_values,
                        "run_id": new_id("run"),
                        "idempotency_key": "noncanonical",
                    }
                )
            )
    return sessions, tenant, run, message


def reader_for(sessions):
    from infra.db.reply_model_binding import SqlReplyRunBindingReader

    return SqlReplyRunBindingReader(sessions)


async def test_resolves_only_canonical_reply_run(integration_engine):
    sessions, tenant, run, message = await seed_binding(integration_engine)
    reader = reader_for(sessions)
    assert await reader.resolve(tenant, message) == run
    assert await reader.validate(tenant, run) == message
    assert await reader.prior(tenant, run) == ()
    with pytest.raises(ModelGenerationError):
        await reader.resolve(TenantId("tn_other"), message)
    with pytest.raises(ModelGenerationError):
        await reader.validate(TenantId("tn_other"), run)


@pytest.mark.parametrize(
    "case",
    [
        "workflow_type",
        "current_step",
        "status",
        "subject_ref",
        "idempotency_key",
        "context_message_id",
        "context_outbound_message_id",
        "context_enrollment_id",
        "context_account_id",
        "context_contact_point_id",
        "direction",
        "missing_message",
        "missing_outbound",
        "missing_attempt",
        "missing_enrollment",
        "duplicate_subject",
        "conversation_account",
    ],
)
async def test_rejects_cross_tenant_or_changed_reply_association(
    integration_engine, case
):
    sessions, tenant, run, message = await seed_binding(integration_engine, case=case)
    reader = reader_for(sessions)
    with pytest.raises(ModelGenerationError):
        await reader.resolve(tenant, message)
    with pytest.raises(ModelGenerationError):
        await reader.validate(tenant, run)


async def test_reads_binding_while_engine_holds_run_lock(integration_engine):
    sessions, tenant, run, message = await seed_binding(integration_engine)
    async with sessions.begin() as holder:
        await holder.scalar(
            select(WorkflowRunRow)
            .where(WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run)
            .with_for_update()
        )
        assert (
            await asyncio.wait_for(reader_for(sessions).validate(tenant, run), 2)
            == message
        )


async def test_prior_reads_all_versions_and_states_only_for_same_binding(
    integration_engine,
):
    sessions, tenant, run, _message = await seed_binding(integration_engine)
    async with sessions.begin() as db:
        for index, state in enumerate(
            ["reserved", "dispatched", "succeeded", "rejected", "invalid", "unknown"]
        ):
            db.add(
                ModelInvocationRow(
                    tenant_id=tenant,
                    invocation_id=new_id("mi"),
                    user_id="usr_binding",
                    employee_id="emp_binding",
                    run_id=run,
                    turn_id=None,
                    capability="reply_qualification",
                    configuration_version=f"v{index}",
                    sequence=0,
                    request_hmac="a" * 64,
                    provider="deepseek",
                    model="test-model",
                    state=state,
                    slot_released=False,
                    created_at=NOW,
                )
            )
        for patch in [
            {"tenant_id": "tn_other"},
            {"run_id": "run_other"},
            {"capability": "research"},
            {"sequence": 1},
        ]:
            values = {
                "tenant_id": tenant,
                "invocation_id": new_id("mi"),
                "user_id": "usr_other",
                "employee_id": "emp_other",
                "run_id": run,
                "turn_id": None,
                "capability": "reply_qualification",
                "configuration_version": "excluded",
                "sequence": 0,
                "request_hmac": "b" * 64,
                "provider": "deepseek",
                "model": "wrong-model",
                "state": "unknown",
                "slot_released": False,
                "created_at": NOW,
            }
            db.add(ModelInvocationRow(**(values | patch)))
    prior = await reader_for(sessions).prior(tenant, run)
    assert len(prior) == 6
    assert {identity.configuration_version for identity, _ in prior} == {
        "v0",
        "v1",
        "v2",
        "v3",
        "v4",
        "v5",
    }
    assert all(
        identity.tenant_id == tenant
        and identity.run_id == run
        and model == "test-model"
        for identity, model in prior
    )
