"""失败调用结束后的显式新尝试；旧调用、步骤与预算均只读。"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.conversations.service import InboxActor
from infra.db.qq_trial_tables import QQTrialRow
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import (
    ConversationClassificationRow,
    ConversationRow,
    EmployeeRow,
    MessageRow,
    ModelInvocationRow,
    ModelSlotReleaseRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import MessageId, TenantId, new_id
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from workflows.reply_qualification.retry import ReplyFailedRetryCommand, ReplyRetryView

if TYPE_CHECKING:
    from infra.db.reply_retry import SqlReplyRetryRepository


def identity_of(row: ModelInvocationRow) -> InvocationIdentity:
    return InvocationIdentity.model_validate(
        {name: getattr(row, name) for name in InvocationIdentity.model_fields}
    )


async def stopped_history(
    db: AsyncSession,
    tenant: TenantId,
    run: str,
    *,
    before_sequence: int,
) -> list[ModelInvocationRow]:
    calls = list(
        (
            await db.scalars(
                select(ModelInvocationRow)
                .where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                    ModelInvocationRow.sequence < before_sequence,
                )
                .order_by(ModelInvocationRow.sequence)
            )
        ).all()
    )
    if not calls or calls[0].sequence != 0:
        raise ValidationError("原始调用不存在")
    present = {call.sequence for call in calls}
    for sequence in set(range(before_sequence)) - present:
        attempt = await db.scalar(
            select(ReplyRetryRow).where(
                ReplyRetryRow.tenant_id == tenant,
                ReplyRetryRow.run_id == run,
                ReplyRetryRow.sequence == sequence,
            )
        )
        if (
            attempt is None
            or attempt.state != "failed"
            or attempt.provider_pending
            or attempt.provider_requests != 0
            or attempt.input_bytes != 0
            or attempt.output_tokens != 0
        ):
            raise ValidationError("历史尝试缺少确定未派发的失败记录")
    root = identity_of(calls[0])
    for call in calls:
        if (
            call.state not in {"rejected", "invalid", "unknown", "succeeded"}
            or not call.slot_released
            or call.finished_at is None
            or (call.state == "rejected" and call.dispatched_at is not None)
            or (call.state == "succeeded" and call.dispatched_at is None)
            or call.capability != "reply_qualification"
            or identity_of(call).model_copy(update={"sequence": 0}) != root
            or call.model != calls[0].model
        ):
            raise ValidationError("历史调用未结束或身份已改变")
        if call.state == "unknown":
            audit = await db.scalar(
                select(ModelSlotReleaseRow).where(
                    ModelSlotReleaseRow.tenant_id == tenant,
                    ModelSlotReleaseRow.invocation_id == call.invocation_id,
                    ModelSlotReleaseRow.reason == "operator_confirmed_stopped",
                )
            )
            if audit is None:
                raise ValidationError("未知调用尚未核实停止")
    return calls


async def eligible(
    repo: "SqlReplyRetryRepository",
    db: AsyncSession,
    tenant: TenantId,
    message: MessageId,
    run: WorkflowRunRow,
    current: ReplyRetryRow | None,
) -> tuple[int, list[ModelInvocationRow]]:
    if run.status != "failed":
        raise ValidationError("识别状态已变化，请刷新")
    await SqlReplyRunBindingReader(repo.factory)._validate(
        db, tenant, run, allowed_statuses={"failed"}
    )
    if current:
        bound = (
            await db.execute(
                select(MessageRow, ConversationRow.account_id)
                .join(
                    ConversationRow,
                    (ConversationRow.tenant_id == MessageRow.tenant_id)
                    & (ConversationRow.conversation_id == MessageRow.conversation_id),
                )
                .where(
                    MessageRow.tenant_id == tenant,
                    MessageRow.message_id == message,
                    ConversationRow.tenant_id == tenant,
                )
            )
        ).one_or_none()
        if bound is None:
            raise ValidationError("原回信关联不存在")
        original, account_id = bound
        immutable = {
            "account_id": account_id,
            "conversation_id": original.conversation_id,
            "raw_artifact_ref": original.raw_artifact_ref,
            "outbound_message_id": original.outbound_message_id,
        }
        if any(current.binding.get(key) != value for key, value in immutable.items()):
            raise ValidationError("原回信关联已变化")
    sequence = current.sequence if current else 0
    if current and (
        current.state not in {"failed", "running"} or current.provider_pending
    ):
        raise ValidationError("上次识别尚未结束")
    calls = await stopped_history(db, tenant, run.run_id, before_sequence=sequence + 1)
    all_calls = (
        await db.scalars(
            select(ModelInvocationRow).where(
                ModelInvocationRow.tenant_id == tenant,
                ModelInvocationRow.run_id == run.run_id,
            )
        )
    ).all()
    steps = (
        await db.scalars(
            select(WorkflowStepRow).where(
                WorkflowStepRow.tenant_id == tenant,
                WorkflowStepRow.run_id == run.run_id,
            )
        )
    ).all()
    classified = await db.scalar(
        select(ConversationClassificationRow.message_id).where(
            ConversationClassificationRow.tenant_id == tenant,
            ConversationClassificationRow.message_id == message,
        )
    )
    trials = (
        await db.scalars(
            select(QQTrialRow).where(
                QQTrialRow.tenant_id == tenant, QQTrialRow.message_id == message
            )
        )
    ).all()
    if (
        len(all_calls) != len(calls)
        or classified is not None
        or len(steps) != sequence + 1
        or any(s.step_name != "classify" or s.status != "failed" for s in steps)
        or any(t.provider_pending or t.state != "stopped" for t in trials)
    ):
        raise ValidationError("回复仍有未完成执行或已有分类")
    view = await repo._model(identity_of(calls[0]))
    if view.model != calls[0].model:
        raise ModelGenerationError("configuration")
    assert view.limits is not None
    if not await repo._quota(db, identity_of(calls[0]), view.limits):
        raise ValidationError("当前模型额度不足")
    return sequence, calls


async def request_failed(
    repo: "SqlReplyRetryRepository",
    tenant: TenantId,
    message: MessageId,
    actor: InboxActor,
    key: str,
    command: ReplyFailedRetryCommand,
) -> ReplyRetryView:
    if not command.acknowledge_cost:
        raise ValidationError("请确认重新识别可能再次产生模型费用")
    async with repo.factory.begin() as db:
        run = await SqlReplyRunBindingReader(repo.factory)._canonical(
            db, tenant, message
        )
        steps = (
            await db.scalars(
                select(WorkflowStepRow)
                .where(
                    WorkflowStepRow.tenant_id == tenant,
                    WorkflowStepRow.run_id == run.run_id,
                )
                .order_by(WorkflowStepRow.created_at, WorkflowStepRow.step_id)
                .with_for_update()
            )
        ).all()
        locked = await db.scalar(
            select(WorkflowRunRow)
            .where(
                WorkflowRunRow.tenant_id == tenant,
                WorkflowRunRow.run_id == run.run_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked is None:
            raise ValidationError("回复流程不存在")
        run = locked
        _, snapshot = await repo._message(db, tenant, message, actor, lock=True)
        current = await repo._existing(db, tenant, message)
        same = await db.scalar(
            select(ReplyRetryRow).where(
                ReplyRetryRow.tenant_id == tenant, ReplyRetryRow.idempotency_key == key
            )
        )
        if same:
            if (
                same.message_id != message
                or same.sequence != command.expected_sequence + 1
                or same.binding["operator"]["employee_id"] != actor.employee_id
                or same.binding.get("retry_after_sequence") != command.expected_sequence
            ):
                raise ValidationError("原请求与当前命令不一致")
            return await repo._view(db, message, current or same, run, actor)
        sequence, calls = await eligible(repo, db, tenant, message, run, current)
        if sequence != command.expected_sequence:
            raise ValidationError("识别状态已变化，请刷新")
        employee = await db.scalar(
            select(EmployeeRow).where(
                EmployeeRow.tenant_id == tenant,
                EmployeeRow.employee_id == actor.employee_id,
            )
        )
        if employee is None or not employee.user_id:
            raise PermissionDenied("操作者已失效")
        stamp = repo.now()
        row = ReplyRetryRow(
            tenant_id=tenant,
            message_id=message,
            sequence=sequence + 1,
            run_id=run.run_id,
            original_invocation_id=calls[0].invocation_id,
            original_step_id=steps[0].step_id,
            idempotency_key=key,
            binding={
                **snapshot,
                "identity": identity_of(calls[0]).model_dump(mode="json"),
                "model": calls[0].model,
                "operator": {
                    "employee_id": str(actor.employee_id),
                    "user_id": employee.user_id,
                    "role": actor.role,
                    "scope": actor.scope.value,
                    "allowed_owner_ids": sorted(actor.allowed_owner_ids),
                },
                "retry_after_sequence": sequence,
                "acknowledge_cost": True,
                "prior_invocation_ids": [call.invocation_id for call in calls],
            },
            created_at=stamp,
            expires_at=None,
            state="queued",
            classifications=0,
            provider_requests=0,
            input_bytes=0,
            output_tokens=0,
            provider_pending=False,
        )
        db.add(row)
        db.add(
            WorkflowStepRow(
                tenant_id=tenant,
                step_id=new_id("wfs"),
                run_id=run.run_id,
                step_name="classify",
                status="pending",
                data={"planned_at": stamp.isoformat()},
                attempt=0,
                due_at=stamp,
                created_at=stamp,
                updated_at=stamp,
                idempotency_key=f"{tenant}:wfstep:{run.run_id}:classify:manual:{sequence + 1}",
            )
        )
        run.status, run.next_poll_at = "running", stamp
        await db.flush()
        return await repo._view(db, message, row, run, actor)
