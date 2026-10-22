"""过期但零开始的恢复只能追加一次授权；真实PG，不触碰原截止时间。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import DBAPIError

from infra.db.reply_resume_tables import ReplyResumeRow
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import ModelInvocationRow, WorkflowRunRow, WorkflowStepRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.model_invocation import ModelGenerationError
from tests.integration.test_reply_retry import NOW, seed


async def legacy_request(repo, tenant, message, actor, key="original-request"):
    # 在插入时构造旧版记录；不关闭或绕过数据库不可变触发器。
    def legacy_expiry(_mapper, _connection, row):
        row.expires_at = row.created_at + timedelta(minutes=5)

    event.listen(ReplyRetryRow, "before_insert", legacy_expiry)
    try:
        await repo.request(tenant, message, actor, key)
    finally:
        event.remove(ReplyRetryRow, "before_insert", legacy_expiry)


async def expired(engine):
    result = await seed(engine)
    sessions, tenant, _run, message, actor, _config, repo = result
    await legacy_request(repo, tenant, message, actor)
    async with sessions.begin() as db:
        workflow = await db.scalar(
            select(WorkflowRunRow).where(WorkflowRunRow.tenant_id == tenant)
        )
        workflow.status = "pending"
    repo.now = lambda: NOW + timedelta(minutes=6)
    return result


async def test_resume_once_keeps_original_expiry_budget_and_steps(integration_engine):
    sessions, tenant, _run, message, actor, _, repo = await expired(integration_engine)
    view = await repo.read(tenant, message, actor)
    assert view.state == "expired" and view.can_resume and not view.can_retry
    async with sessions() as db:
        original = await db.scalar(
            select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
        )
        original_expiry = original.expires_at
    views = await asyncio.gather(
        *[repo.resume(tenant, message, actor, "resume-request") for _ in range(2)]
    )
    assert all(v.state == "queued" and v.resumed and not v.can_resume for v in views)
    async with sessions() as db:
        row = await db.scalar(
            select(ReplyResumeRow).where(ReplyResumeRow.tenant_id == tenant)
        )
        original = await db.scalar(
            select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
        )
        workflow = await db.scalar(
            select(WorkflowRunRow).where(WorkflowRunRow.tenant_id == tenant)
        )
        steps = (
            await db.scalars(
                select(WorkflowStepRow).where(WorkflowStepRow.tenant_id == tenant)
            )
        ).all()
        assert original.expires_at == original_expiry
        assert original.classifications == original.provider_requests == 0
        assert row.created_at == repo.now()
        assert row.original_idempotency_key == "original-request"
        assert workflow.status == "running"
        assert len(steps) == 2 and sorted(s.status for s in steps) == [
            "failed",
            "pending",
        ]
    assert (await repo.retry_identity(tenant, message, "retry-v1")).sequence == 1
    with pytest.raises(ValidationError):
        await repo.resume(tenant, message, actor, "different-request")
    repo.now = lambda: NOW + timedelta(minutes=12)
    view = await repo.read(tenant, message, actor)
    assert view.state == "queued" and view.resumed and not view.can_resume
    await repo.require_message(tenant, message)
    with pytest.raises(DBAPIError):
        async with sessions.begin() as db:
            row = await db.scalar(
                select(ReplyResumeRow).where(ReplyResumeRow.tenant_id == tenant)
            )
            row.created_at += timedelta(minutes=5)


@pytest.mark.parametrize(
    "change",
    [
        "not-expired",
        "started",
        "classification-budget",
        "provider",
        "pending-provider",
        "unknown-model",
        "new-sequence",
        "config",
        "raw",
        "wrong-key",
        "owner",
        "inactive",
        "other-tenant",
    ],
)
async def test_resume_rejects_nonzero_or_changed_facts(integration_engine, change):
    sessions, tenant, _run, message, actor, config, repo = await expired(
        integration_engine
    )
    if change == "not-expired":
        repo.now = lambda: NOW + timedelta(seconds=6)
    if change == "config":
        config.version = "different"
    async with sessions.begin() as db:
        row = await db.scalar(
            select(ReplyRetryRow).where(ReplyRetryRow.tenant_id == tenant)
        )
        if change == "started":
            row.model_started_at = NOW
        if change == "classification-budget":
            row.classifications = 1
        if change == "provider":
            row.provider_requests = 1
        if change == "pending-provider":
            row.provider_pending = True
        if change in {"unknown-model", "new-sequence"}:
            call = await db.scalar(
                select(ModelInvocationRow).where(ModelInvocationRow.tenant_id == tenant)
            )
            if change == "unknown-model":
                call.state = "unknown"
            else:
                call.sequence = 1
        if change == "raw":
            from infra.db.tables import MessageRow

            msg = await db.scalar(
                select(MessageRow).where(
                    MessageRow.tenant_id == tenant, MessageRow.message_id == message
                )
            )
            msg.raw_artifact_ref = "artifact:changed"
    if change == "inactive":
        from infra.db.tables import EmployeeRow

        async with sessions.begin() as db:
            row = await db.scalar(
                select(EmployeeRow).where(
                    EmployeeRow.tenant_id == tenant,
                    EmployeeRow.employee_id == actor.employee_id,
                )
            )
            row.is_active = False
    if change == "owner":
        from infra.db.tables import OwnershipLockRow
        from shared.schemas.identifiers import new_id
        async with sessions.begin() as db:
            workflow = await db.scalar(select(WorkflowRunRow).where(WorkflowRunRow.tenant_id == tenant))
            db.add(OwnershipLockRow(tenant_id=tenant,lock_id=new_id("own"),account_id=workflow.context["account_id"],owner=actor.employee_id,locked_at=NOW,locked_by_rule="fixture"))
    if change == "other-tenant":
        tenant = "tn_other"
    if change == "wrong-key":
        await repo.resume(tenant, message, actor, "first-resume")
    with pytest.raises((ValidationError, PermissionDenied, ModelGenerationError)):
        await repo.resume(tenant, message, actor, "another-resume")
