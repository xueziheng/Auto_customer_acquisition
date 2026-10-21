"""启动的当前事实及恢复读取；配置不是员工/邮箱/发送事实授权的替代品。"""
from datetime import datetime
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.aliyun_connection import AliyunActor
from infra.db.qq_inbound import CurrentQQMailbox, PostgresProjectReplyScope
from infra.db.qq_trial_tables import QQTrialRow
from infra.db.tables import (
    EmailInboundCursorRow,
    EmployeeRow,
    OutreachMessageAttemptRow,
    ToolCallRow,
)
from infra.qq_inbound_binding import CurrentQQBinding
from infra.standalone.settings import QQReplySettings, StandaloneModelSettings
from shared.schemas.identifiers import EmployeeId
from shared.schemas.qq_trial import QQTrialScope, QQTrialStopped


class QQStartupFacts:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], qq: QQReplySettings,
                 settings: StandaloneModelSettings, mailbox: CurrentQQMailbox) -> None:
        assert qq.trial is not None
        self.sessions, self.qq, self.settings, self.launch = sessions, qq, settings, qq.trial
        self.current = PostgresProjectReplyScope(sessions, self.launch.route,
            EmployeeId(qq.employee_id), mailbox, attempt_id=qq.attempt_id)
        self.authorization_deadline: float | None = None
        self.delivery_binding: CurrentQQBinding | None = None
        delivery = self.launch.delivery
        if delivery is not None:
            self.delivery_binding = CurrentQQBinding(qq.mailbox_directory,
                AliyunActor(tenant_id=self.launch.route.tenant_id,
                    employee_id=delivery.employee_id, user_id=delivery.user_id), qq.recipient)

    async def authorize(self) -> bool:
        launch, qq = self.launch, self.qq
        target = await self.current.target(launch.route, EmployeeId(qq.employee_id))
        if target is None or target.outbound_message_header != launch.canonical_header:
            return False
        tenant = launch.route.tenant_id
        delivery = launch.delivery
        if delivery is not None:
            if (delivery.employee_id != qq.classifier_employee_id
                or delivery.user_id != launch.classifier_user_id or self.delivery_binding is None):
                return False
            try:
                if not await self.delivery_binding.matches(tenant, delivery.employee_id,
                    delivery.user_id, qq.recipient):
                    return False
            except Exception:  # noqa: BLE001 - 撤销或私有连接损坏均拒绝，不输出凭证错误。
                return False
        expected_tool = "email.qq.delivery_alias.read" if delivery else "email.qq.sent_alias.read"
        async with self.sessions() as session:
            conflicting_source = await session.scalar(select(ToolCallRow.tool_call_id).where(
                ToolCallRow.tenant_id == tenant, ToolCallRow.message_attempt_id == qq.attempt_id,
                ToolCallRow.tool_id.in_(("email.qq.sent_alias.read", "email.qq.delivery_alias.read")),
                ToolCallRow.tool_id != expected_tool).limit(1))
            if conflicting_source is not None:
                return False
            checked_from = monotonic()
            stamp = await session.scalar(select(func.clock_timestamp()))
            if isinstance(stamp, datetime):
                # 使用 DB 时钟，并从请求开始折算，不能把查询耗时增加到授权窗口。
                self.authorization_deadline = checked_from + (launch.expires_at - stamp).total_seconds()
            owner = await session.scalar(select(EmployeeRow).where(
                EmployeeRow.tenant_id==tenant, EmployeeRow.employee_id==qq.employee_id,
                EmployeeRow.user_id==qq.user_id, EmployeeRow.is_active.is_(True)))
            boss = await session.scalar(select(EmployeeRow).where(
                EmployeeRow.tenant_id==tenant, EmployeeRow.employee_id==qq.classifier_employee_id,
                EmployeeRow.user_id==launch.classifier_user_id,
                EmployeeRow.role=="boss", EmployeeRow.is_active.is_(True)))
            attempt = await session.scalar(select(OutreachMessageAttemptRow).where(
                OutreachMessageAttemptRow.tenant_id==tenant,
                OutreachMessageAttemptRow.attempt_id==qq.attempt_id,
                OutreachMessageAttemptRow.state=="sent",
                OutreachMessageAttemptRow.deterministic_message_id==launch.canonical_header,
                OutreachMessageAttemptRow.sending_identity_id==launch.route.configured_identity_id))
            return (owner is not None and boss is not None and attempt is not None
                and attempt.idempotency_header == launch.canonical_header[1:].split("@")[0]
                and isinstance(stamp,datetime) and launch.starts_at <= stamp < launch.expires_at)

    async def existing(self) -> QQTrialScope | None:
        tenant = self.launch.route.tenant_id
        async with self.sessions() as session:
            rows = (await session.scalars(select(QQTrialRow).where(
                QQTrialRow.tenant_id==tenant,
                (QQTrialRow.trial_id==self.launch.trial_id) |
                (QQTrialRow.attempt_id==self.qq.attempt_id) |
                (QQTrialRow.mailbox_alias==self.launch.route.mailbox_alias)))).all()
            if rows:
                if len(rows)!=1 or rows[0].trial_id!=self.launch.trial_id or rows[0].state not in {"active","received"}:
                    raise QQTrialStopped("existing_trial_conflict")
                return QQTrialScope.model_validate(rows[0].scope)
            cursor = await session.scalar(select(EmailInboundCursorRow).where(
                EmailInboundCursorRow.tenant_id==tenant,
                EmailInboundCursorRow.mailbox_alias==self.launch.route.mailbox_alias))
            if cursor is not None:
                raise QQTrialStopped("existing_cursor_forbidden")
        return None
