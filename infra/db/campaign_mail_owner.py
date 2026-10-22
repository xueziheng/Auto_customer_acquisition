"""由当前发送身份、本人邮箱和客户归属求交集；不把技术调用者当发件人。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.schemas import MessageSendPreflight
from infra.aliyun_mail_store import AliyunMailStore
from infra.db.business_mail_tables import BusinessMailboxRow
from infra.db.tables import (
    EmployeeRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OwnershipLockRow,
    SendingIdentityRow,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId
from tool_gateway.handlers.email_send import OutboundMailOwner


class CampaignMailboxOwner:
    """只在原 email.send prepare/execute/reconcile 内使用的当前身份投影。"""
    def __init__(self, sessions: async_sessionmaker[AsyncSession], store: AliyunMailStore) -> None:
        self.sessions, self.store = sessions, store

    async def _read(self, tenant: TenantId, attempt_id: str, address: str) -> OutboundMailOwner:
        async with self.sessions() as db:
            attempt = await db.get(OutreachMessageAttemptRow, (str(tenant), attempt_id))
            if attempt is None:
                raise PermissionDenied("当前发件身份不可用")
            enrollment = await db.get(OutreachEnrollmentRow, (str(tenant), attempt.enrollment_id))
            identity = await db.get(SendingIdentityRow, (str(tenant), attempt.sending_identity_id))
            box = await db.scalar(select(BusinessMailboxRow).where(
                BusinessMailboxRow.tenant_id == tenant, BusinessMailboxRow.address == address,
                BusinessMailboxRow.enabled.is_(True)))
            if enrollment is None or identity is None or identity.address != address or box is None:
                raise PermissionDenied("当前本人邮箱与发件身份不匹配")
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == box.owner_id))
            ownership = await db.scalar(select(OwnershipLockRow).where(
                OwnershipLockRow.tenant_id == tenant, OwnershipLockRow.account_id == enrollment.account_id))
            if (employee is None or not employee.is_active or employee.role not in {"boss", "manager", "sales"}
                or employee.user_id != box.user_id or ownership is None or ownership.owner != employee.employee_id):
                raise PermissionDenied("当前客户归属与本人邮箱不匹配")
            proof = (attempt_id, address, identity.identity_id, str(identity.version), box.mailbox_id,
                box.binding_operation_id, employee.user_id, ownership.locked_at.isoformat())
            return OutboundMailOwner(tenant, EmployeeId(employee.employee_id), proof)

    async def bind(self, preflight: MessageSendPreflight, from_address: str) -> OutboundMailOwner | None:
        if not from_address.endswith("@qq.com"):
            return None
        return await self._read(preflight.tenant_id, str(preflight.attempt_id), from_address)

    async def check(self, owner: OutboundMailOwner) -> None:
        current = await self._read(owner.tenant_id, owner.proof[0], owner.proof[1])
        if current != owner:
            raise PermissionDenied("原发件身份或本人邮箱绑定已变化")
