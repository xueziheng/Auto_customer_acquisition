"""真实 PG 恢复事务：失败历史、当前授权、并发与新身份。外部模型为受控端口。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from domains.conversations.service import InboxActor, InboxScope
from infra.db.reply_retry import SqlReplyRetryRepository
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import (
    EmployeeRow,
    ModelConfigurationHeadRow,
    ModelConfigurationVersionRow,
    ModelInvocationRow,
    OwnershipLockRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import ModelGenerationError
from tests.integration.test_model_usage import limits
from tests.integration.test_reply_model_binding import NOW, seed_binding


class Configuration:
    def __init__(self):
        self.version = "retry-v1"

    async def authorize(self, actor, version, *, probe):
        if version != self.version:
            raise ModelGenerationError("configuration")

    async def get_public(self, actor):
        return SimpleNamespace(
            model="test-model",
            status="verified",
            configuration_version=self.version,
            limits=limits(
                max_input_bytes=32768, max_output_tokens=512, timeout_seconds=60
            ),
        )


async def seed(engine):
    sessions, tenant, run, message = await seed_binding(engine)
    employee, user, step, invocation = (
        new_id(p) for p in ("emp", "usr", "wfs", "minv")
    )
    config = Configuration()
    async with sessions.begin() as db:
        r = await db.scalar(
            select(WorkflowRunRow).where(
                WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run
            )
        )
        r.status, r.last_error = "failed", "step classify failed: ModelGenerationError"
        db.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=employee,
                user_id=user,
                name="fixture",
                role="boss",
                is_active=True,
            )
        )
        db.add(
            WorkflowStepRow(
                tenant_id=tenant,
                run_id=run,
                step_id=step,
                step_name="classify",
                status="failed",
                error="ModelGenerationError",
                data={"planned_at": NOW.isoformat()},
                attempt=0,
                due_at=NOW,
                idempotency_key="classify",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.add(
            ModelInvocationRow(
                tenant_id=tenant,
                invocation_id=invocation,
                user_id=user,
                employee_id=employee,
                run_id=run,
                capability="reply_qualification",
                configuration_version=config.version,
                sequence=0,
                request_hmac="a" * 64,
                provider="deepseek",
                model="test-model",
                state="rejected",
                slot_released=True,
                created_at=NOW,
                finished_at=NOW,
            )
        )
        db.add(
            ModelConfigurationVersionRow(
                tenant_id=tenant,
                version=config.version,
                model="test-model",
                limits=limits(
                    max_input_bytes=32768, max_output_tokens=512, timeout_seconds=60
                ).model_dump(),
                export_enabled=True,
                created_by=employee,
                created_at=NOW,
            )
        )
        await db.flush()
        db.add(ModelConfigurationHeadRow(tenant_id=tenant, version=config.version))
    actor = InboxActor(tenant, employee, "boss", InboxScope.TENANT)
    return (
        sessions,
        tenant,
        run,
        message,
        actor,
        config,
        SqlReplyRetryRepository(
            sessions, config, now=lambda: NOW + timedelta(seconds=5)
        ),
    )


async def test_concurrent_same_command_one_step_preserves_failure(integration_engine):
    sessions, tenant, run, message, actor, config, repo = await seed(integration_engine)
    assert (await repo.read(tenant, message, actor)).can_retry
    results = await asyncio.gather(
        *[repo.request(tenant, message, actor, "retry-key-1") for _ in range(2)]
    )
    assert all(r.state == "queued" for r in results)
    async with sessions() as db:
        steps = (
            await db.scalars(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.tenant_id == tenant, WorkflowStepRow.run_id == run
                )
            )
        ).all()
        assert sorted(s.status for s in steps) == ["failed", "pending"]
        assert (
            next(s for s in steps if s.status == "failed").error
            == "ModelGenerationError"
        )
        calls = (
            await db.scalars(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                )
            )
        ).all()
        assert (
            len(calls) == 1
            and calls[0].state == "rejected"
            and calls[0].dispatched_at is None
        )
    with pytest.raises(ValidationError):
        await repo.request(tenant, message, actor, "different-key")
    fresh = SqlReplyRetryRepository(sessions, config, now=repo.now)
    identity = await fresh.retry_identity(tenant, message, config.version)
    assert identity.sequence == 1 and identity.run_id == run
    assert (await fresh.authorize_identity(identity)).sequence == 0
    with pytest.raises(ModelGenerationError):
        await fresh.authorize_identity(identity.model_copy(update={"sequence": 2}))


@pytest.mark.parametrize(
    "patch",
    [
        {"state": "unknown"},
        {"state": "succeeded"},
        {"dispatched_at": NOW},
        {"sequence": 1},
    ],
)
async def test_nonzero_or_unknown_dispatch_never_restored(integration_engine, patch):
    sessions, tenant, run, message, actor, _config, repo = await seed(
        integration_engine
    )
    async with sessions.begin() as db:
        await db.execute(
            update(ModelInvocationRow)
            .where(
                ModelInvocationRow.tenant_id == tenant, ModelInvocationRow.run_id == run
            )
            .values(**patch)
        )
    assert not (await repo.read(tenant, message, actor)).can_retry
    with pytest.raises(ValidationError):
        await repo.request(tenant, message, actor, "retry-key-1")


async def test_current_owner_and_actor_change_block_before_dispatch(integration_engine):
    sessions, tenant, _run, message, actor, config, repo = await seed(
        integration_engine
    )
    await repo.request(tenant, message, actor, "retry-key-1")
    async with sessions.begin() as db:
        await db.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == tenant,
                EmployeeRow.employee_id == actor.employee_id,
            )
            .values(is_active=False)
        )
    with pytest.raises(PermissionDenied):
        await repo.retry_identity(tenant, message, config.version)
    with pytest.raises(PermissionDenied):
        await repo.read("tn_other", message, actor)


async def test_queue_wait_has_no_deadline_but_configuration_is_current(integration_engine):
    sessions, tenant, _run, message, actor, config, repo = await seed(
        integration_engine
    )
    await repo.request(tenant, message, actor, "retry-key-1")
    config.version = "changed"
    with pytest.raises(ModelGenerationError):
        await repo.retry_identity(tenant, message, "retry-v1")
    config.version = "retry-v1"
    late = SqlReplyRetryRepository(
        sessions, config, now=lambda: NOW + timedelta(minutes=6)
    )
    assert (await late.read(tenant, message, actor)).state == "queued"
    assert (await late.retry_identity(tenant, message, config.version)).sequence == 1


async def test_recovery_budget_cannot_repeat_classification_or_unknown_provider(
    integration_engine,
):
    sessions, tenant, _run, message, actor, _config, repo = await seed(
        integration_engine
    )
    await repo.request(tenant, message, actor, "retry-key-1")
    from infra.db.reply_retry_budget import SqlReplyRetryBudget

    gate = SqlReplyRetryBudget(repo, tenant, message)
    await gate.start()
    with pytest.raises(ModelGenerationError):
        await gate.start()
    await gate.reserve_round(100)
    with pytest.raises(ModelGenerationError):
        await gate.reserve_round(100)
    async with sessions() as db:
        row = await db.scalar(
            select(ReplyRetryRow).where(
                ReplyRetryRow.tenant_id == tenant, ReplyRetryRow.message_id == message
            )
        )
        assert (
            row.classifications == 1
            and row.provider_requests == 1
            and row.provider_pending
        )


@pytest.mark.parametrize("changed", ["owner", "raw", "classified", "quota"])
async def test_persisted_changes_are_rechecked(integration_engine, changed):
    from infra.db.tables import ConversationClassificationRow, MessageRow

    sessions, tenant, run, message, actor, config, repo = await seed(integration_engine)
    if changed in {"owner", "raw"}:
        await repo.request(tenant, message, actor, "retry-key-1")
    async with sessions.begin() as db:
        r = await db.scalar(
            select(WorkflowRunRow).where(
                WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run
            )
        )
        if changed == "owner":
            db.add(
                OwnershipLockRow(
                    tenant_id=tenant,
                    lock_id=new_id("own"),
                    account_id=r.context["account_id"],
                    owner=actor.employee_id,
                    locked_at=NOW,
                    locked_by_rule="fixture",
                )
            )
        elif changed == "raw":
            await db.execute(
                update(MessageRow)
                .where(MessageRow.tenant_id == tenant, MessageRow.message_id == message)
                .values(raw_artifact_ref="raw_changed")
            )
        elif changed == "classified":
            db.add(
                ConversationClassificationRow(
                    tenant_id=tenant,
                    message_id=message,
                    category="willing_to_continue",
                    classified_by="test-model",
                    classified_at=NOW,
                    candidate_fields=[],
                )
            )
        else:
            db.add(
                ModelInvocationRow(
                    tenant_id=tenant,
                    invocation_id=new_id("minv"),
                    user_id="usr_quota",
                    employee_id=actor.employee_id,
                    run_id=new_id("run"),
                    capability="model_probe",
                    configuration_version=config.version,
                    sequence=0,
                    request_hmac="b" * 64,
                    provider="deepseek",
                    model="test-model",
                    state="succeeded",
                    slot_released=True,
                    created_at=NOW,
                    finished_at=NOW,
                )
            )
    if changed in {"owner", "raw"}:
        with pytest.raises(ModelGenerationError):
            await repo.retry_identity(tenant, message, config.version)
    else:
        assert not (await repo.read(tenant, message, actor)).can_retry
        with pytest.raises(ValidationError):
            await repo.request(tenant, message, actor, "retry-key-1")
