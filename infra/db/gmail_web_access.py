"""网页邮箱的当前身份及诊断信誉检查；每条查询显式过滤租户。"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.gmail_connection import GmailActor, require_gmail_test_role
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    OutreachSuppressionRow,
    ProspectContactRow,
    SendingIdentityRow,
)
from shared.errors import PermissionDenied


class SqlGmailWebAccess:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    async def role(self, actor: GmailActor) -> str:
        async with self.sessions() as session:
            role = await session.scalar(select(EmployeeRow.role).where(
                EmployeeRow.tenant_id == actor.tenant_id,
                EmployeeRow.employee_id == actor.employee_id,
                EmployeeRow.user_id == actor.user_id,
                EmployeeRow.is_active.is_(True),
            ))
        if role is None:
            raise PermissionDenied("当前员工或邮箱绑定已失效")
        return str(role)

    async def test_allowed(self, actor: GmailActor, sender: str, recipient: str) -> bool:
        require_gmail_test_role(await self.role(actor))
        async with self.sessions() as session:
            states = (await session.scalars(select(SendingIdentityRow.state).where(
                SendingIdentityRow.tenant_id == actor.tenant_id,
                SendingIdentityRow.address == sender,
            ))).all()
            if not states or any(state not in {"created", "auth_pending", "warming", "active"} for state in states):
                return False
            suppressed = await session.scalar(
                select(func.count()).select_from(ContactPointRow)
                .join(ProspectContactRow,
                      (ProspectContactRow.tenant_id == ContactPointRow.tenant_id)
                      & (ProspectContactRow.contact_id == ContactPointRow.contact_id))
                .join(OutreachSuppressionRow,
                      (OutreachSuppressionRow.tenant_id == ContactPointRow.tenant_id)
                      & or_(OutreachSuppressionRow.contact_point_id == ContactPointRow.contact_point_id,
                            OutreachSuppressionRow.account_id == ProspectContactRow.account_id))
                .where(ContactPointRow.tenant_id == actor.tenant_id,
                       ProspectContactRow.tenant_id == actor.tenant_id,
                       OutreachSuppressionRow.tenant_id == actor.tenant_id,
                       ContactPointRow.kind == "email",
                       func.lower(ContactPointRow.value) == recipient.casefold())
            )
        return suppressed == 0
