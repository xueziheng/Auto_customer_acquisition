"""回复模型的 tenant-bound 只读关联；不重取 workflow 已持有的行锁。"""

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import (
    ConversationRow,
    MessageRow,
    ModelInvocationRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    WorkflowRunRow,
)
from shared.schemas.identifiers import MessageId, RunId, TenantId
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError


class SqlReplyRunBindingReader:
    """只投影安全身份；真实原文读取仍由既有证据 Gateway 执行。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def resolve(self, tenant_id: TenantId, message_id: MessageId) -> RunId:
        """同消息存在不唯一的回复 Run 时关闭，不能任意挑一条。"""
        async with self._factory() as session:
            row = await self._canonical(session, tenant_id, message_id)
            await self._validate(session, tenant_id, row)
            return RunId(row.run_id)

    async def validate(self, tenant_id: TenantId, run_id: RunId) -> MessageId:
        """授权前后按调用 Run 重读当前关联，不能只信最初绑定。"""
        async with self._factory() as session:
            row = await session.scalar(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.tenant_id == tenant_id,
                    WorkflowRunRow.run_id == run_id,
                )
            )
            if row is None:
                raise ModelGenerationError("permission")
            canonical = await self._canonical(
                session, tenant_id, MessageId(row.subject_ref)
            )
            if canonical.run_id != run_id:
                raise ModelGenerationError("permission")
            return await self._validate(session, tenant_id, row)

    async def _canonical(
        self, session: AsyncSession, tenant_id: TenantId, message_id: MessageId
    ) -> WorkflowRunRow:
        rows = (
            await session.scalars(
                select(WorkflowRunRow)
                .where(
                    WorkflowRunRow.tenant_id == tenant_id,
                    WorkflowRunRow.workflow_type == "reply_qualification",
                    WorkflowRunRow.subject_ref == message_id,
                )
                .limit(2)
            )
        ).all()
        if len(rows) != 1:
            raise ModelGenerationError("permission")
        return rows[0]

    async def _validate(
        self, session: AsyncSession, tenant_id: TenantId, run: WorkflowRunRow
    ) -> MessageId:
        context = run.context
        if (
            run.workflow_type != "reply_qualification"
            or run.current_step != "classify"
            or run.status not in {"pending", "running"}
            or run.idempotency_key != f"reply:{run.subject_ref}"
            or not isinstance(context, dict)
            or context.get("message_id") != run.subject_ref
        ):
            raise ModelGenerationError("permission")
        # 同一查询快照核对消息所属账户与出站序列；避免拼接另一账户的回复。
        found = (
            await session.execute(
                select(MessageRow, OutreachMessageAttemptRow, OutreachEnrollmentRow)
                .join(
                    ConversationRow,
                    (ConversationRow.tenant_id == MessageRow.tenant_id)
                    & (ConversationRow.conversation_id == MessageRow.conversation_id),
                )
                .join(
                    OutreachMessageAttemptRow,
                    (OutreachMessageAttemptRow.tenant_id == MessageRow.tenant_id)
                    & (
                        OutreachMessageAttemptRow.deterministic_message_id
                        == MessageRow.outbound_message_id
                    ),
                )
                .join(
                    OutreachEnrollmentRow,
                    (
                        OutreachEnrollmentRow.tenant_id
                        == OutreachMessageAttemptRow.tenant_id
                    )
                    & (
                        OutreachEnrollmentRow.enrollment_id
                        == OutreachMessageAttemptRow.enrollment_id
                    ),
                )
                .where(
                    MessageRow.tenant_id == tenant_id,
                    MessageRow.message_id == run.subject_ref,
                    MessageRow.direction == "inbound",
                    ConversationRow.tenant_id == tenant_id,
                    ConversationRow.account_id == OutreachEnrollmentRow.account_id,
                    OutreachMessageAttemptRow.tenant_id == tenant_id,
                    OutreachEnrollmentRow.tenant_id == tenant_id,
                )
            )
        ).one_or_none()
        if found is None:
            raise ModelGenerationError("permission")
        message, attempt, enrollment = found
        expected = {
            "message_id": message.message_id,
            "outbound_message_id": message.outbound_message_id,
            "enrollment_id": attempt.enrollment_id,
            "account_id": enrollment.account_id,
            "contact_point_id": enrollment.contact_point_id,
        }
        if any(
            not value or context.get(key) != value for key, value in expected.items()
        ):
            raise ModelGenerationError("permission")
        return MessageId(message.message_id)

    async def prior(
        self, tenant_id: TenantId, run_id: RunId
    ) -> tuple[tuple[InvocationIdentity, str], ...]:
        """跨版本读取包含 unknown 的全部状态；不把配置变化当新回复。"""
        async with self._factory() as session:
            rows = (
                await session.scalars(
                    select(ModelInvocationRow)
                    .where(
                        ModelInvocationRow.tenant_id == tenant_id,
                        ModelInvocationRow.run_id == run_id,
                        ModelInvocationRow.capability == "reply_qualification",
                        ModelInvocationRow.sequence == 0,
                    )
                    .order_by(
                        ModelInvocationRow.created_at, ModelInvocationRow.invocation_id
                    )
                )
            ).all()
        try:
            return tuple(
                (
                    InvocationIdentity.model_validate({
                        "tenant_id": row.tenant_id,
                        "user_id": row.user_id,
                        "employee_id": row.employee_id,
                        "run_id": row.run_id,
                        "turn_id": row.turn_id,
                        "capability": row.capability,
                        "configuration_version": row.configuration_version,
                        "sequence": row.sequence,
                    }),
                    row.model,
                )
                for row in rows
            )
        except ValidationError:
            raise ModelGenerationError("permission") from None
