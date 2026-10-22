"""同规范 Run 的恢复适配；不覆盖旧 step、invocation 或 QQ 试验。"""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.assistant.schemas import AssistantActor, ModelSettingsView
from domains.assistant.service import ModelConfigurationService
from domains.conversations.inbox_access import require_current_access
from domains.conversations.service import InboxAction, InboxActor, InboxScope
from infra.db.inbox_access import SqlAlchemyInboxAccessFactsReader
from infra.db.qq_trial_tables import QQTrialRow
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.reply_resume_tables import ReplyResumeRow
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import (
    ConversationClassificationRow,
    ConversationRow,
    EmployeeRow,
    MessageRow,
    ModelConfigurationHeadRow,
    ModelInvocationRow,
    OwnershipLockRow,
    WorkflowRunRow,
    WorkflowStepRow,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import MessageId, ProspectAccountId, TenantId, new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelLimits,
)
from tool_gateway.model_usage import window_start
from workflows.reply_qualification.retry import (
    ReplyFailedRetryCommand,
    ReplyRetryView,
    RetryFacts,
    require_retryable,
)


def invocation_identity(row: ModelInvocationRow) -> InvocationIdentity:
    return InvocationIdentity.model_validate(
        {name: getattr(row, name) for name in InvocationIdentity.model_fields}
    )


class SqlReplyRetryRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        configuration: ModelConfigurationService,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.factory, self.configuration, self.now = factory, configuration, now

    async def _message(
        self,
        db: AsyncSession,
        tenant: TenantId,
        message: MessageId,
        actor: InboxActor,
        *,
        lock: bool = False,
    ) -> tuple[MessageRow, dict[str, str | None]]:
        found = (
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
        if found is None:
            raise PermissionDenied("回信不存在或不可见")
        msg, account_value = found
        account = ProspectAccountId(account_value)
        facts = SqlAlchemyInboxAccessFactsReader(db, tenant)
        if lock:
            await facts.lock_account_access(tenant, account, actor.employee_id)
        await require_current_access(facts, tenant, actor, InboxAction.CORRECT, account)
        # 本入口仅当前 owner 或 boss；manager 的团队读取范围不授予模型恢复。
        owner = await db.scalar(
            select(OwnershipLockRow).where(
                OwnershipLockRow.tenant_id == tenant,
                OwnershipLockRow.account_id == account,
            )
        )
        if actor.role != "boss" and (owner is None or owner.owner != actor.employee_id):
            raise PermissionDenied("重新识别只允许当前负责人或老板")
        if msg.direction != "inbound":
            raise ValidationError("仅入站回信可以重新识别")
        snapshot = {
            "account_id": account,
            "conversation_id": msg.conversation_id,
            "raw_artifact_ref": msg.raw_artifact_ref,
            "outbound_message_id": msg.outbound_message_id,
            "owner_id": owner.owner if owner else None,
            "owner_locked_at": owner.locked_at.isoformat() if owner else None,
        }
        return msg, snapshot

    async def _model(self, identity: InvocationIdentity) -> ModelSettingsView:
        actor = AssistantActor(
            tenant_id=identity.tenant_id,
            employee_id=identity.employee_id,
            user_id=identity.user_id,
        )
        await self.configuration.authorize(
            actor, identity.configuration_version, probe=False
        )
        view = await self.configuration.get_public(actor)
        if (
            view.limits is None
            or view.model is None
            or view.status != "verified"
            or view.configuration_version != identity.configuration_version
        ):
            raise ModelGenerationError("configuration")
        return view

    async def _quota(
        self, db: AsyncSession, identity: InvocationIdentity, limits: ModelLimits
    ) -> bool:
        start = window_start(self.now(), limits.window_seconds)
        calls, employee_calls, active, employee_active = (
            await db.execute(
                select(
                    func.count().filter(ModelInvocationRow.created_at >= start),
                    func.count().filter(
                        ModelInvocationRow.created_at >= start,
                        ModelInvocationRow.employee_id == identity.employee_id,
                    ),
                    func.count().filter(ModelInvocationRow.slot_released.is_(False)),
                    func.count().filter(
                        ModelInvocationRow.slot_released.is_(False),
                        ModelInvocationRow.employee_id == identity.employee_id,
                    ),
                ).where(
                    ModelInvocationRow.tenant_id == identity.tenant_id,
                    ModelInvocationRow.state != "rejected",
                )
            )
        ).one()
        return (
            (limits.tenant_calls is None or calls < limits.tenant_calls)
            and (limits.employee_calls is None or employee_calls < limits.employee_calls)
            and active < limits.tenant_concurrency
            and employee_active < limits.employee_concurrency
        )

    async def _eligible(
        self,
        db: AsyncSession,
        tenant: TenantId,
        message: MessageId,
        run: WorkflowRunRow,
    ) -> ModelInvocationRow:
        calls = (
            await db.scalars(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run.run_id,
                    ModelInvocationRow.capability == "reply_qualification",
                )
            )
        ).all()
        original = calls[0] if len(calls) == 1 else None
        trials = (
            await db.scalars(
                select(QQTrialRow).where(
                    QQTrialRow.tenant_id == tenant, QQTrialRow.message_id == message
                )
            )
        ).all()
        classified = await db.scalar(
            select(ConversationClassificationRow.message_id).where(
                ConversationClassificationRow.tenant_id == tenant,
                ConversationClassificationRow.message_id == message,
            )
        )
        head = await db.scalar(
            select(ModelConfigurationHeadRow).where(
                ModelConfigurationHeadRow.tenant_id == tenant
            )
        )
        require_retryable(
            RetryFacts(
                run.status,
                run.current_step,
                len(calls),
                original.state if original else "",
                original is None or original.dispatched_at is not None,
                original.sequence if original else -1,
                classified is not None,
                sum(t.provider_requests for t in trials),
                any(t.provider_pending for t in trials),
                bool(
                    original and head and head.version == original.configuration_version
                ),
                True,
            )
        )
        reader = SqlReplyRunBindingReader(self.factory)
        canonical = await reader._canonical(db, tenant, message)
        if canonical.run_id != run.run_id:
            raise ValidationError("回复规范流程不一致")
        await reader._validate(db, tenant, run, allowed_statuses={"failed"})
        if original is None:
            raise ValidationError("原模型调用不存在")
        return original

    async def _existing(
        self, db: AsyncSession, tenant: TenantId, message: MessageId
    ) -> ReplyRetryRow | None:
        return await db.scalar(
            select(ReplyRetryRow)
            .where(
                ReplyRetryRow.tenant_id == tenant, ReplyRetryRow.message_id == message
            )
            .order_by(ReplyRetryRow.sequence.desc())
            .limit(1)
        )

    async def _resumption(
        self, db: AsyncSession, row: ReplyRetryRow
    ) -> ReplyResumeRow | None:
        if row.sequence != 1:
            return None
        grant = await db.scalar(
            select(ReplyResumeRow).where(
                ReplyResumeRow.tenant_id == row.tenant_id,
                ReplyResumeRow.message_id == row.message_id,
            )
        )
        if grant and (
            grant.run_id != row.run_id
            or grant.original_idempotency_key != row.idempotency_key
            or grant.operator_employee_id != row.binding["operator"]["employee_id"]
            or grant.operator_user_id != row.binding["operator"]["user_id"]
        ):
            raise ModelGenerationError("permission")
        return grant

    async def _view(
        self,
        db: AsyncSession,
        message: MessageId,
        row: ReplyRetryRow,
        run: WorkflowRunRow | None,
        actor: InboxActor,
    ) -> ReplyRetryView:
        grant = await self._resumption(db, row)
        state = row.state
        if run and run.status == "completed":
            state = "completed"
        elif run and run.status == "failed":
            state = "failed"
        elif (
            state not in {"classified", "failed"}
            and grant is None
            and row.expires_at is not None
            and self.now() >= row.expires_at
        ):
            state = "expired"
        can_failed_retry = False
        if state == "failed" and run:
            from infra.db.reply_failed_retry import eligible

            try:
                await eligible(self, db, actor.tenant_id, message, run, row)
                can_failed_retry = True
            except (ValidationError, PermissionDenied, ModelGenerationError):
                pass
        can_resume = False
        if state == "expired" and run:
            try:
                await self._resume_eligible(db, row, run, actor)
                can_resume = True
            except (ValidationError, PermissionDenied, ModelGenerationError):
                pass
        return ReplyRetryView.model_validate(
            {
                "message_id": message,
                "state": state,
                "reason": "manual_recovery",
                "expected_sequence": row.sequence if can_failed_retry else None,
                "requires_cost_confirmation": can_failed_retry,
                "can_retry": can_failed_retry
                or bool(
                    state == "queued"
                    and run
                    and run.status == "pending"
                    and row.classifications == 0
                    and row.provider_requests == 0
                    and not row.provider_pending
                ),
                "can_resume": can_resume,
                "resumed": grant is not None,
            }
        )

    async def read(
        self, tenant: TenantId, message: MessageId, actor: InboxActor
    ) -> ReplyRetryView:
        async with self.factory() as db:
            await self._message(db, tenant, message, actor)
            run = await db.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == tenant,
                    WorkflowRunRow.workflow_type == "reply_qualification",
                    WorkflowRunRow.subject_ref == message,
                )
            )
            row = await self._existing(db, tenant, message)
            if row:
                return await self._view(db, message, row, run, actor)
            if run is None:
                return ReplyRetryView(
                    message_id=message, state="unavailable", reason="not_recoverable"
                )
            try:
                original = await self._eligible(db, tenant, message, run)
            except (ValidationError, ModelGenerationError):
                from infra.db.reply_failed_retry import eligible

                try:
                    await eligible(self, db, tenant, message, run, None)
                except (ValidationError, PermissionDenied, ModelGenerationError):
                    return ReplyRetryView(
                        message_id=message,
                        state="unavailable",
                        reason="not_recoverable",
                    )
                return ReplyRetryView(
                    message_id=message,
                    state="eligible",
                    reason="stopped_failure",
                    can_retry=True,
                    expected_sequence=0,
                    requires_cost_confirmation=True,
                )
            identity = invocation_identity(original)
            try:
                view = await self._model(identity)
            except (ModelGenerationError, PermissionDenied):
                return ReplyRetryView(
                    message_id=message, state="unavailable", reason="model_unavailable"
                )
            assert view.limits is not None  # _model validates the public configuration
            if not await self._quota(db, identity, view.limits):
                return ReplyRetryView(
                    message_id=message, state="unavailable", reason="quota_exhausted"
                )
            return ReplyRetryView(
                message_id=message,
                state="eligible",
                reason="not_dispatched",
                can_retry=True,
            )

    async def retry_failed(
        self,
        tenant: TenantId,
        message: MessageId,
        actor: InboxActor,
        key: str,
        command: ReplyFailedRetryCommand,
    ) -> ReplyRetryView:
        from infra.db.reply_failed_retry import request_failed

        return await request_failed(self, tenant, message, actor, key, command)

    async def request(
        self, tenant: TenantId, message: MessageId, actor: InboxActor, key: str
    ) -> ReplyRetryView:
        async with self.factory.begin() as db:
            # 与原 poll_due 一致：先 step 后 run。旧失败 step 永远保留。
            run = await SqlReplyRunBindingReader(self.factory)._canonical(
                db, tenant, message
            )
            steps = (
                await db.scalars(
                    select(WorkflowStepRow)
                    .where(
                        WorkflowStepRow.tenant_id == tenant,
                        WorkflowStepRow.run_id == run.run_id,
                        WorkflowStepRow.step_name == "classify",
                    )
                    .order_by(WorkflowStepRow.created_at, WorkflowStepRow.step_id)
                    .with_for_update()
                )
            ).all()
            locked_run = await db.scalar(
                select(WorkflowRunRow)
                .where(
                    WorkflowRunRow.tenant_id == tenant,
                    WorkflowRunRow.run_id == run.run_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if locked_run is None:
                raise ValidationError("回复规范流程已失效")
            run = locked_run
            _, snapshot = await self._message(db, tenant, message, actor, lock=True)
            existing = await self._existing(db, tenant, message)
            if existing:
                if (
                    existing.idempotency_key != key
                    or existing.binding["operator"]["employee_id"] != actor.employee_id
                ):
                    raise ValidationError("该回信已提交一次重新识别，请刷新结果")
                # 旧实现误把 Run 写成 pending；只允许原请求、零派发、未过期时修复调度状态。
                if run.status == "pending" and existing.state == "queued":
                    current = await self.current(
                        db, tenant, message, lock=True, model=True
                    )
                    pending = [
                        step
                        for step in steps
                        if step.status == "pending"
                        and step.idempotency_key
                        == f"{tenant}:wfstep:{run.run_id}:classify:manual:1"
                    ]
                    if (
                        current.classifications != 0
                        or current.provider_requests != 0
                        or current.provider_pending
                        or len(steps) != 2
                        or len(pending) != 1
                    ):
                        raise ValidationError("恢复调度状态不能安全继续")
                    run.status = "running"
                return await self._view(db, message, existing, run, actor)
            if len(steps) != 1 or steps[0].status != "failed":
                raise ValidationError("回复失败步骤不唯一")
            await db.scalar(
                select(ModelConfigurationHeadRow)
                .where(ModelConfigurationHeadRow.tenant_id == tenant)
                .with_for_update(read=True)
            )
            original = await self._eligible(db, tenant, message, run)
            identity = invocation_identity(original)
            view = await self._model(identity)
            assert view.limits is not None  # _model validates the public configuration
            if view.model != original.model or not await self._quota(
                db, identity, view.limits
            ):
                raise ValidationError("当前模型配置或额度不支持恢复")
            # 固定操作者当前 user 绑定；请求方不能提供模型身份。
            operator = await db.scalar(
                select(EmployeeRow).where(
                    EmployeeRow.tenant_id == tenant,
                    EmployeeRow.employee_id == actor.employee_id,
                )
            )
            if operator is None or not operator.user_id:
                raise PermissionDenied("操作者已失效")
            stamp = self.now()
            binding = {
                **snapshot,
                "identity": identity.model_dump(mode="json"),
                "model": original.model,
                "operator": {
                    "employee_id": str(actor.employee_id),
                    "user_id": operator.user_id,
                    "role": actor.role,
                    "scope": actor.scope.value,
                    "allowed_owner_ids": sorted(actor.allowed_owner_ids),
                },
            }
            row = ReplyRetryRow(
                tenant_id=tenant,
                message_id=message,
                run_id=run.run_id,
                original_invocation_id=original.invocation_id,
                original_step_id=steps[0].step_id,
                idempotency_key=key,
                binding=binding,
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
                    idempotency_key=f"{tenant}:wfstep:{run.run_id}:classify:manual:1",
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
            run.status, run.next_poll_at = "running", stamp
            # last_error、原 step 及 invocation 不改写；授权行即追加的恢复审计。
            return await self._view(db, message, row, run, actor)

    async def exists(self, tenant: TenantId, message: MessageId) -> bool:
        async with self.factory() as db:
            return await self._existing(db, tenant, message) is not None

    async def _current_binding(
        self,
        db: AsyncSession,
        tenant: TenantId,
        message: MessageId,
        *,
        lock: bool = False,
    ) -> ReplyRetryRow:
        row = await self._existing(db, tenant, message)
        if row is None:
            raise ModelGenerationError("permission")
        op = row.binding["operator"]
        actor = InboxActor(
            tenant,
            op["employee_id"],
            op["role"],
            InboxScope(op["scope"]),
            frozenset(op["allowed_owner_ids"]),
        )
        _, snapshot = await self._message(db, tenant, message, actor, lock=lock)
        if any(row.binding.get(k) != v for k, v in snapshot.items()):
            raise ModelGenerationError("permission")
        employee = await db.scalar(
            select(EmployeeRow).where(
                EmployeeRow.tenant_id == tenant,
                EmployeeRow.employee_id == actor.employee_id,
            )
        )
        if employee is None or employee.user_id != op["user_id"]:
            raise ModelGenerationError("permission")
        if lock:
            row = await db.scalar(
                select(ReplyRetryRow)
                .where(
                    ReplyRetryRow.tenant_id == tenant,
                    ReplyRetryRow.message_id == message,
                    ReplyRetryRow.sequence == row.sequence,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if row is None:
            raise ModelGenerationError("permission")
        identity = InvocationIdentity.model_validate(row.binding["identity"])
        original = await db.scalar(
            select(ModelInvocationRow).where(
                ModelInvocationRow.tenant_id == tenant,
                ModelInvocationRow.invocation_id == row.original_invocation_id,
            )
        )
        if (
            original is None
            or invocation_identity(original) != identity
            or original.model != row.binding["model"]
        ):
            raise ModelGenerationError("permission")
        if "prior_invocation_ids" in row.binding:
            from infra.db.reply_failed_retry import stopped_history

            calls = await stopped_history(
                db, tenant, row.run_id, before_sequence=row.sequence
            )
            if [c.invocation_id for c in calls] != row.binding["prior_invocation_ids"]:
                raise ModelGenerationError("permission")
        elif (
            row.sequence != 1
            or original.state != "rejected"
            or original.dispatched_at is not None
        ):
            raise ModelGenerationError("permission")
        if lock:
            await db.scalar(
                select(ModelConfigurationHeadRow)
                .where(ModelConfigurationHeadRow.tenant_id == tenant)
                .with_for_update(read=True)
            )
        view = await self._model(identity)
        if view.model != row.binding["model"]:
            raise ModelGenerationError("configuration")
        return row

    async def current(
        self,
        db: AsyncSession,
        tenant: TenantId,
        message: MessageId,
        *,
        lock: bool = False,
        model: bool = False,
    ) -> ReplyRetryRow:
        row = await self._current_binding(db, tenant, message, lock=lock)
        if row.state == "failed" or (model and row.state == "classified"):
            raise ModelGenerationError("permission")
        if (
            row.state != "classified"
            and row.expires_at is not None
            and self.now() >= row.expires_at
            and await self._resumption(db, row) is None
        ):
            raise ModelGenerationError("permission")
        return row

    async def _resume_eligible(
        self,
        db: AsyncSession,
        row: ReplyRetryRow,
        run: WorkflowRunRow,
        actor: InboxActor,
        *,
        lock: bool = False,
    ) -> None:
        if (
            row.expires_at is None
            or self.now() < row.expires_at
            or row.state != "queued"
            or row.model_started_at is not None
            or row.classifications != 0
            or row.provider_requests != 0
            or row.input_bytes != 0
            or row.output_tokens != 0
            or row.provider_pending
            or row.binding["operator"]["employee_id"] != actor.employee_id
        ):
            raise ValidationError("只有旧测试中到期但未开始的请求可以继续")
        await self._current_binding(
            db, actor.tenant_id, MessageId(row.message_id), lock=lock
        )
        reader = SqlReplyRunBindingReader(self.factory)
        canonical = await reader._canonical(
            db, actor.tenant_id, MessageId(row.message_id)
        )
        if canonical.run_id != row.run_id or run.run_id != row.run_id:
            raise ValidationError("回复规范流程不一致")
        await reader._validate(db, actor.tenant_id, run)
        calls = (
            await db.scalars(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == actor.tenant_id,
                    ModelInvocationRow.run_id == row.run_id,
                )
            )
        ).all()
        classified = await db.scalar(
            select(ConversationClassificationRow.message_id).where(
                ConversationClassificationRow.tenant_id == actor.tenant_id,
                ConversationClassificationRow.message_id == row.message_id,
            )
        )
        steps = (
            await db.scalars(
                select(WorkflowStepRow).where(
                    WorkflowStepRow.tenant_id == actor.tenant_id,
                    WorkflowStepRow.run_id == row.run_id,
                    WorkflowStepRow.step_name == "classify",
                )
            )
        ).all()
        pending = [
            step
            for step in steps
            if step.status == "pending"
            and step.attempt == 0
            and step.idempotency_key
            == f"{actor.tenant_id}:wfstep:{row.run_id}:classify:manual:1"
        ]
        original = [
            step
            for step in steps
            if step.step_id == row.original_step_id and step.status == "failed"
        ]
        trials = (
            await db.scalars(
                select(QQTrialRow).where(
                    QQTrialRow.tenant_id == actor.tenant_id,
                    QQTrialRow.message_id == row.message_id,
                )
            )
        ).all()
        if (
            len(calls) != 1
            or calls[0].invocation_id != row.original_invocation_id
            or calls[0].sequence != 0
            or calls[0].state != "rejected"
            or calls[0].dispatched_at is not None
            or classified is not None
            or len(steps) != 2
            or len(pending) != 1
            or len(original) != 1
            or any(t.provider_requests != 0 or t.provider_pending for t in trials)
        ):
            raise ValidationError("请求已有执行或关联变化，不能续办")
        identity = invocation_identity(calls[0])
        view = await self._model(identity)
        assert view.limits is not None
        if not await self._quota(db, identity, view.limits):
            raise ValidationError("当前模型额度不足")

    async def resume(
        self,
        tenant: TenantId,
        message: MessageId,
        actor: InboxActor,
        key: str,
    ) -> ReplyRetryView:
        async with self.factory.begin() as db:
            run = await SqlReplyRunBindingReader(self.factory)._canonical(
                db, tenant, message
            )
            steps = (
                await db.scalars(
                    select(WorkflowStepRow)
                    .where(
                        WorkflowStepRow.tenant_id == tenant,
                        WorkflowStepRow.run_id == run.run_id,
                        WorkflowStepRow.step_name == "classify",
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
            await self._message(db, tenant, message, actor, lock=True)
            row = await self._existing(db, tenant, message)
            if (
                row is None
                or row.binding["operator"]["employee_id"] != actor.employee_id
            ):
                raise PermissionDenied("仅原操作者可以继续未开始的请求")
            grant = await self._resumption(db, row)
            if grant:
                if grant.idempotency_key != key:
                    raise ValidationError("该请求已续办一次，请刷新结果")
                await self._current_binding(db, tenant, message, lock=True)
                return await self._view(db, message, row, run, actor)
            await self._resume_eligible(db, row, run, actor, lock=True)
            stamp = self.now()
            db.add(
                ReplyResumeRow(
                    tenant_id=tenant,
                    message_id=message,
                    run_id=row.run_id,
                    original_idempotency_key=row.idempotency_key,
                    idempotency_key=key,
                    operator_employee_id=actor.employee_id,
                    operator_user_id=row.binding["operator"]["user_id"],
                    created_at=stamp,
                )
            )
            run.status, run.next_poll_at = "running", stamp
            for step in steps:
                if step.status == "pending":
                    step.due_at = stamp
            await db.flush()
            return await self._view(db, message, row, run, actor)

    async def require_message(self, tenant: TenantId, message: MessageId) -> None:
        async with self.factory.begin() as db:
            await self.current(db, tenant, message)

    async def retry_identity(
        self, tenant: TenantId, message: MessageId, version: str
    ) -> InvocationIdentity | None:
        async with self.factory.begin() as db:
            row = await self._existing(db, tenant, message)
            if row is None:
                return None
            row = await self.current(db, tenant, message, model=True)
            original = InvocationIdentity.model_validate(row.binding["identity"])
            if original.configuration_version != version:
                raise ModelGenerationError("configuration")
            return original.model_copy(update={"sequence": row.sequence})

    async def authorize_identity(
        self, identity: InvocationIdentity
    ) -> InvocationIdentity:
        async with self.factory.begin() as db:
            row = await db.scalar(
                select(ReplyRetryRow).where(
                    ReplyRetryRow.tenant_id == identity.tenant_id,
                    ReplyRetryRow.run_id == identity.run_id,
                    ReplyRetryRow.sequence == identity.sequence,
                )
            )
            if row is None:
                raise ModelGenerationError("permission")
            row = await self.current(
                db, identity.tenant_id, MessageId(row.message_id), model=True
            )
            original = InvocationIdentity.model_validate(row.binding["identity"])
            if identity != original.model_copy(update={"sequence": row.sequence}):
                raise ModelGenerationError("permission")
            return original

    async def commit_message(
        self, db: AsyncSession, tenant: TenantId, message: MessageId | None
    ) -> None:
        if message is None:
            raise ModelGenerationError("permission")
        row = await self.current(db, tenant, message, lock=True)
        if row.provider_pending:
            raise ModelGenerationError("permission")
        # 分类和恢复完成标记同事务；业务动作仍沿用原 Outbox 和幂等键。
        classification = await db.scalar(
            select(ConversationClassificationRow.message_id).where(
                ConversationClassificationRow.tenant_id == tenant,
                ConversationClassificationRow.message_id == message,
            )
        )
        if classification is not None:
            if row.classifications != 1 or row.provider_requests < 1:
                raise ModelGenerationError("permission")
            row.state = "classified"

    async def committed(self, tenant: TenantId, message: MessageId) -> bool:
        async with self.factory.begin() as db:
            row = await self._existing(db, tenant, message)
            if row is None or row.state != "classified":
                return False
            await self.current(db, tenant, message)
            return True
