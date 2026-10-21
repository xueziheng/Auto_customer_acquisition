"""显式询盘准入的原子持久端口；借原邮箱锁、同事务域服务和outbox。"""
from collections.abc import Callable
from datetime import datetime
from email.utils import getaddresses
from typing import Self, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.dialogue_source import DialogueSource
from domains.conversations.service import ConversationsUnitOfWork
from domains.conversations.service_impl import ConversationServiceImpl
from domains.demand.repository import DemandUnitOfWork
from domains.demand.service_impl import DemandServiceImpl
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_followup import CurrentBusinessMailLease
from infra.db.demand_uow import BorrowedDemandUnitOfWork
from infra.db.dialogue_execution_tables import DialogueBindingRow
from infra.db.dialogue_source import SqlDialogueSources, source_from_row
from infra.db.email_inbound_uow import _BoundConversations
from infra.db.tables import (
    ContactLegalBasisRow,
    ContactPointRow,
    EmployeeRow,
    OwnershipLockRow,
    ProspectAccountRow,
    ProspectContactRow,
    RawArtifactRow,
    SendingDomainRow,
    SendingIdentityRow,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import SendingIdentityId, TenantId
from workflows.reply_qualification.inquiry_admission import (
    InquiryAdmissionFacts,
    InquiryAdmissionRequest,
)


class SqlInquiryAdmissionTransaction:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tenant_id: TenantId,
                 mailbox: SqlBusinessMailRepository, *, now: Callable[[], datetime]) -> None:
        self.sessions, self.tenant_id, self.now = sessions, tenant_id, now
        self.lease = CurrentBusinessMailLease(mailbox)
        self.sources = SqlDialogueSources(sessions, tenant_id)

    async def __aenter__(self) -> Self:
        self.db = self.sessions()
        await self.db.begin()
        self.conversations = ConversationServiceImpl(
            lambda tenant: cast(ConversationsUnitOfWork, _BoundConversations(self.db, self._tenant(tenant), self.now)), now=self.now)
        self.demand = DemandServiceImpl(
            lambda tenant: cast(DemandUnitOfWork, BorrowedDemandUnitOfWork(self.db, self._tenant(tenant), now=self.now)), now=self.now)
        return self

    def _tenant(self, tenant: TenantId) -> TenantId:
        if tenant != self.tenant_id:
            raise PermissionDenied("询盘准入租户不匹配")
        return tenant

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        try:
            if exc_type is None:
                await self.db.commit()
            else:
                await self.db.rollback()
        finally:
            await self.db.close()

    async def facts(self, actor: BusinessMailActor, business_message_id: str,
                    request: InquiryAdmissionRequest) -> InquiryAdmissionFacts:
        tenant = self._tenant(actor.tenant_id)
        message, box = await self.lease.acquire(self.db, actor, business_message_id)
        employee = await self.db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant,
            EmployeeRow.employee_id == actor.employee_id))
        owner = await self.db.scalar(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == tenant,
            OwnershipLockRow.account_id == request.account_id).with_for_update())
        artifact = await self.db.get(RawArtifactRow, (str(tenant), message.artifact_id))
        if (employee is None or owner is None or owner.owner != actor.employee_id
            or message.source_kind != "full_mime" or message.message_header is None
            or artifact is None or artifact.kind != "email_raw" or artifact.content_hash != message.source_sha256):
            raise PermissionDenied("询盘本人原件或客户归属不匹配")
        identity = await self.db.scalar(select(SendingIdentityRow).where(SendingIdentityRow.tenant_id == tenant,
            SendingIdentityRow.address == box.address).with_for_update(read=True))
        point = await self.db.scalar(select(ContactPointRow).where(ContactPointRow.tenant_id == tenant,
            ContactPointRow.contact_point_id == request.contact_point_id).with_for_update(read=True))
        contact = await self.db.get(ProspectContactRow, (str(tenant), point.contact_id)) if point else None
        account = await self.db.get(ProspectAccountRow, (str(tenant), request.account_id))
        basis = await self.db.scalar(select(ContactLegalBasisRow).where(ContactLegalBasisRow.tenant_id == tenant,
            ContactLegalBasisRow.contact_point_id == request.contact_point_id).with_for_update(read=True))
        if (identity is None or point is None or contact is None or account is None or basis is None
            or contact.account_id != request.account_id or point.kind != "email"
            or point.verification_status != "verified" or point.verified_at is None):
            raise PermissionDenied("询盘联系人尚未关联或验证")
        domain = await self.db.get(SendingDomainRow, (str(tenant), identity.domain))
        if domain is None:
            raise PermissionDenied("询盘发件域不存在")
        addresses = getaddresses([message.sender])
        return InquiryAdmissionFacts(role=employee.role, account_name=account.name,
            ownership_locked_at=owner.locked_at, sending_identity_id=SendingIdentityId(identity.identity_id),
            sender_role=domain.role, mailbox_operation_id=box.binding_operation_id,
            artifact_id=message.artifact_id, source_sha256=message.source_sha256,
            external_message_id=message.message_header, received_at=message.received_at,
            customer_body=message.body_text,
            contact_matches_sender=len(addresses) == 1 and addresses[0][1].casefold() == point.value.casefold())

    async def existing(self, actor: BusinessMailActor, business_message_id: str) -> DialogueSource | None:
        tenant = self._tenant(actor.tenant_id)
        row = await self.db.scalar(select(DialogueBindingRow).where(DialogueBindingRow.tenant_id == tenant,
            DialogueBindingRow.business_message_id == business_message_id))
        return source_from_row(row) if row else None

    async def bind(self, source: DialogueSource) -> None:
        await self.sources.admit(self.db, source)

    async def recheck(self, actor: BusinessMailActor, business_message_id: str) -> None:
        self._tenant(actor.tenant_id)
        _, box = await self.lease.acquire(self.db, actor, business_message_id)
        await self.lease.check(actor, box)
