"""从持久发送记录投影 QQ 入站授权；不读正文、不写业务表。"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.qq_mail.inbound import ProjectReplyGrant, ReplyHeaders
from infra.db.tables import (
    ContactLegalBasisRow,
    ContactPointRow,
    EmployeeRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    PrivateReplyTestRow,
    ProspectContactRow,
    SendingIdentityRow,
)
from shared.schemas.email_inbound import InboundError, InboundRoute
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, UserId


class CurrentQQMailbox(Protocol):
    async def matches(
        self, tenant_id: TenantId, employee_id: EmployeeId, user_id: UserId,
        address: str,
    ) -> bool:
        """由受信当前绑定端口验证本人邮箱；不得仅信配置声明。"""
        ...


class PostgresProjectReplyScope:
    """严格限定本次两个测试邮箱；授权来自当前员工、归属、联系人及已发送事实。"""
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], route: InboundRoute,
        employee_id: EmployeeId, mailbox: CurrentQQMailbox, *, attempt_id: str | None = None,
    ) -> None:
        self._sessions, self._route, self._employee, self._mailbox = sessions, route, employee_id, mailbox
        self._attempt_id = attempt_id

    async def resolve(
        self, route: InboundRoute, employee_id: EmployeeId, headers: ReplyHeaders,
    ) -> ProjectReplyGrant | None:
        if (headers.sender, headers.recipient) != ("986647123@qq.com", "1249583590@qq.com"):
            return None
        grant = await self.target(route, employee_id)
        return grant if grant is not None and headers.in_reply_to == grant.outbound_message_header else None

    async def target(
        self, route: InboundRoute, employee_id: EmployeeId,
    ) -> ProjectReplyGrant | None:
        """先读取指定成功Attempt的当前授权；缺失时不允许连接邮箱。"""
        if route != self._route or employee_id != self._employee:
            raise InboundError()
        if self._attempt_id is None:
            return None
        tenant = route.tenant_id
        async with self._sessions() as session:
            rows = (await session.execute(
                select(OutreachMessageAttemptRow.message_id, EmployeeRow.user_id,
                    OutreachMessageAttemptRow.deterministic_message_id)
                .select_from(OutreachMessageAttemptRow)
                .join(OutreachEnrollmentRow,
                    (OutreachEnrollmentRow.tenant_id == tenant)
                    & (OutreachEnrollmentRow.enrollment_id == OutreachMessageAttemptRow.enrollment_id)
                    & (OutreachEnrollmentRow.sending_identity_id == route.configured_identity_id))
                .join(SendingIdentityRow,
                    (SendingIdentityRow.tenant_id == tenant)
                    & (SendingIdentityRow.identity_id == route.configured_identity_id)
                    & (SendingIdentityRow.address == "1249583590@qq.com"))
                .join(ContactPointRow,
                    (ContactPointRow.tenant_id == tenant)
                    & (ContactPointRow.contact_point_id == OutreachEnrollmentRow.contact_point_id)
                    & (ContactPointRow.kind == "email")
                    & (ContactPointRow.value == "986647123@qq.com"))
                .join(ProspectContactRow,
                    (ProspectContactRow.tenant_id == tenant)
                    & (ProspectContactRow.contact_id == ContactPointRow.contact_id)
                    & (ProspectContactRow.account_id == OutreachEnrollmentRow.account_id))
                .join(ContactLegalBasisRow,
                    (ContactLegalBasisRow.tenant_id == tenant)
                    & (ContactLegalBasisRow.contact_point_id == ContactPointRow.contact_point_id)
                    & (ContactLegalBasisRow.basis == "consent")
                    & (ContactLegalBasisRow.source == "private_reply_test_confirmation"))
                .join(PrivateReplyTestRow,
                    (PrivateReplyTestRow.tenant_id == tenant)
                    & (PrivateReplyTestRow.account_id == OutreachEnrollmentRow.account_id)
                    & (PrivateReplyTestRow.contact_point_id == ContactPointRow.contact_point_id)
                    & (PrivateReplyTestRow.sending_identity_id == route.configured_identity_id)
                    & (PrivateReplyTestRow.owner_id == employee_id))
                .join(OwnershipLockRow,
                    (OwnershipLockRow.tenant_id == tenant)
                    & (OwnershipLockRow.account_id == OutreachEnrollmentRow.account_id)
                    & (OwnershipLockRow.owner == employee_id))
                .join(EmployeeRow,
                    (EmployeeRow.tenant_id == tenant)
                    & (EmployeeRow.employee_id == employee_id)
                    & EmployeeRow.is_active.is_(True)
                    & EmployeeRow.role.in_(("boss", "manager", "sales")))
                .where(
                    OutreachMessageAttemptRow.tenant_id == tenant,
                    OutreachMessageAttemptRow.attempt_id == self._attempt_id,
                    OutreachMessageAttemptRow.sending_identity_id == route.configured_identity_id,
                    OutreachMessageAttemptRow.state == "sent",
                    OutreachMessageAttemptRow.provider_ref.is_not(None),
                    OutreachMessageAttemptRow.provider_ref != "",
                    OutreachEnrollmentRow.execution_mode == "manual",
                    OutreachEnrollmentRow.source_hypothesis_id.is_(None),
                ).limit(2)
            )).all()
        if len(rows) != 1 or not rows[0].user_id:
            return None
        if not await self._mailbox.matches(
            tenant, employee_id, UserId(rows[0].user_id), "1249583590@qq.com",
        ):
            return None
        return ProjectReplyGrant(
            route, employee_id, MessageId(rows[0].message_id),
            rows[0].deterministic_message_id, "1249583590@qq.com", "986647123@qq.com", self._attempt_id,
        )
