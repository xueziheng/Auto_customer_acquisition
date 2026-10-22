"""已结束失败的显式重试：保留旧调用、预算及步骤，拒绝重复派发。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from infra.db.reply_retry_budget import SqlReplyRetryBudget
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import (
    MessageRow,
    ModelInvocationRow,
    ModelSlotReleaseRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import new_id
from tests.integration.test_reply_retry import seed
from workflows.reply_qualification.retry import ReplyFailedRetryCommand


async def failed_retry(engine, *, invocation_state="unknown"):
    values = await seed(engine)
    sessions, tenant, run, message, actor, config, repo = values
    await repo.request(tenant, message, actor, "first-retry-key")
    identity = await repo.retry_identity(tenant, message, config.version)
    budget = SqlReplyRetryBudget(repo, tenant, message)
    await budget.start()
    await budget.reserve_round(21425)
    await budget.finish_round(28, 512)
    if invocation_state == "unknown":
        await budget.fail()
    async with sessions.begin() as db:
        call_id = new_id("minv")
        db.add(
            ModelInvocationRow(
                **identity.model_dump(),
                invocation_id=call_id,
                request_hmac="b" * 64,
                provider="deepseek",
                model="test-model",
                state=invocation_state,
                slot_released=True,
                created_at=repo.now(),
                dispatched_at=repo.now(),
                finished_at=repo.now(),
            )
        )
        await db.flush()
        db.add(
            ModelSlotReleaseRow(
                tenant_id=tenant,
                invocation_id=call_id,
                operator_id=identity.user_id,
                reason="operator_confirmed_stopped",
                created_at=repo.now(),
            )
        )
        current = await db.scalar(
            select(WorkflowRunRow).where(
                WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run
            )
        )
        current.status = "failed"
        steps = (
            await db.scalars(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.tenant_id == tenant, WorkflowStepRow.run_id == run
                )
            )
        ).all()
        for step in steps:
            if step.status == "pending":
                step.status, step.error = "failed", "ModelGenerationError"
    identity_time = repo.now()
    repo.now = lambda: identity_time + timedelta(days=1)
    return values


async def test_stopped_failed_retry_can_be_requested_again(integration_engine):
    values = await failed_retry(integration_engine)
    sessions, tenant, run, message, actor, config, repo = values
    view = await repo.read(tenant, message, actor)
    assert view.can_retry, "已结束且已释放名额的失败必须允许显式重试"

    assert view.expected_sequence == 1 and view.requires_cost_confirmation
    command = ReplyFailedRetryCommand(expected_sequence=1, acknowledge_cost=True)
    results = await asyncio.gather(
        *[
            repo.retry_failed(tenant, message, actor, "retry-failed-key", command)
            for _ in range(2)
        ]
    )
    assert all(result.state == "queued" for result in results)
    assert (await repo.retry_identity(tenant, message, config.version)).sequence == 2
    async with sessions() as db:
        rows = (
            await db.scalars(
                select(ReplyRetryRow)
                .where(
                    ReplyRetryRow.tenant_id == tenant,
                    ReplyRetryRow.message_id == message,
                )
                .order_by(ReplyRetryRow.sequence)
            )
        ).all()
        assert [
            (
                r.sequence,
                r.state,
                r.classifications,
                r.provider_requests,
                r.input_bytes,
                r.output_tokens,
            )
            for r in rows
        ] == [(1, "failed", 1, 1, 21425, 28), (2, "queued", 0, 0, 0, 0)]
        calls = (
            await db.scalars(
                select(ModelInvocationRow)
                .where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                )
                .order_by(ModelInvocationRow.sequence)
            )
        ).all()
        assert [(c.sequence, c.state) for c in calls] == [
            (0, "rejected"),
            (1, "unknown"),
        ]
        steps = (
            await db.scalars(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.tenant_id == tenant, WorkflowStepRow.run_id == run
                )
            )
        ).all()
        assert sorted(s.status for s in steps) == ["failed", "failed", "pending"]
    with pytest.raises(ValidationError):
        await repo.retry_failed(tenant, message, actor, "another-retry-key", command)


@pytest.mark.parametrize(
    "case",
    ["no-consent", "stale", "in-flight", "pending", "no-stop-proof", "changed-raw"],
)
async def test_failed_retry_requires_current_consent_and_stopped_proof(
    integration_engine, case
):
    sessions, tenant, run, message, actor, _config, repo = await failed_retry(
        integration_engine
    )
    async with sessions.begin() as db:
        if case == "in-flight":
            await db.execute(
                update(ModelInvocationRow)
                .where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                    ModelInvocationRow.sequence == 1,
                )
                .values(slot_released=False)
            )
        elif case == "pending":
            await db.execute(
                update(ReplyRetryRow)
                .where(
                    ReplyRetryRow.tenant_id == tenant,
                    ReplyRetryRow.message_id == message,
                )
                .values(provider_pending=True)
            )
        elif case == "changed-raw":
            await db.execute(
                update(MessageRow)
                .where(MessageRow.tenant_id == tenant, MessageRow.message_id == message)
                .values(raw_artifact_ref="s3://different-original")
            )
        elif case == "no-stop-proof":
            # This fixture's audit mutation is intentionally invalid current proof.
            await db.execute(
                update(ModelSlotReleaseRow)
                .where(ModelSlotReleaseRow.tenant_id == tenant)
                .values(reason="not_confirmed")
            )
    command = ReplyFailedRetryCommand(
        expected_sequence=0 if case == "stale" else 1,
        acknowledge_cost=case != "no-consent",
    )
    with pytest.raises(ValidationError):
        await repo.retry_failed(tenant, message, actor, "rejected-failed-key", command)
    async with sessions() as db:
        rows = (
            await db.scalars(
                select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
            )
        ).all()
        assert len(rows) == 1 and rows[0].state == "failed"


async def test_finished_classification_failure_preserves_billing_and_allows_next(
    integration_engine,
):
    sessions, tenant, run, message, actor, _config, repo = await failed_retry(
        integration_engine, invocation_state="succeeded"
    )
    view = await repo.read(tenant, message, actor)
    assert view.can_retry and view.expected_sequence == 1
    await repo.retry_failed(
        tenant,
        message,
        actor,
        "after-classify-failure",
        ReplyFailedRetryCommand(expected_sequence=1, acknowledge_cost=True),
    )
    async with sessions() as db:
        previous = await db.scalar(
            select(ReplyRetryRow).where(
                ReplyRetryRow.tenant_id == tenant,
                ReplyRetryRow.message_id == message,
                ReplyRetryRow.sequence == 1,
            )
        )
        assert (previous.state, previous.input_bytes, previous.output_tokens) == (
            "running",
            21425,
            28,
        )
        call = await db.scalar(
            select(ModelInvocationRow).where(
                ModelInvocationRow.tenant_id == tenant,
                ModelInvocationRow.run_id == run,
                ModelInvocationRow.sequence == 1,
            )
        )
        assert call.state == "succeeded"


async def test_quota_rejection_before_invocation_can_retry_without_refunding(
    integration_engine,
):
    sessions, tenant, run, message, actor, config, repo = await seed(integration_engine)
    await repo.request(tenant, message, actor, "first-quota-retry")
    budget = SqlReplyRetryBudget(repo, tenant, message)
    await budget.start()
    await budget.fail()
    async with sessions.begin() as db:
        await db.execute(
            update(WorkflowRunRow)
            .where(WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run)
            .values(status="failed")
        )
        await db.execute(
            update(WorkflowStepRow)
            .where(WorkflowStepRow.tenant_id == tenant, WorkflowStepRow.run_id == run)
            .values(status="failed")
        )
    assert (await repo.read(tenant, message, actor)).can_retry
    await repo.retry_failed(
        tenant,
        message,
        actor,
        "after-quota-rejection",
        ReplyFailedRetryCommand(expected_sequence=1, acknowledge_cost=True),
    )
    identity = await repo.retry_identity(tenant, message, config.version)
    assert identity.sequence == 2
    await repo.authorize_identity(identity)
    async with sessions() as db:
        previous = await db.scalar(
            select(ReplyRetryRow).where(
                ReplyRetryRow.tenant_id == tenant,
                ReplyRetryRow.message_id == message,
                ReplyRetryRow.sequence == 1,
            )
        )
        assert (
            previous.state,
            previous.classifications,
            previous.provider_requests,
        ) == ("failed", 1, 0)
