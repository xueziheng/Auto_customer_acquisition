"""从私有用途标记读取精确原Enrollment关联；不创建业务数据。"""
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.outreach.schemas import ContactEligibilitySnapshot
from domains.outreach.service import ContactEligibilityProvider
from domains.prospecting.private_reply_test import PrivateReplyTestOption
from infra.db.tables import (
    ContactPointRow,
    OutreachEnrollmentRow,
    PrivateReplyTestRow,
    SendingIdentityRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
)


class PrivateReplyTestReader:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def matches(
        self, tenant: TenantId, account: ProspectAccountId,
        contact: ContactPointId, enrollment: EnrollmentId,
    ) -> bool:
        async with self._sessions() as session:
            marker = await session.scalar(select(PrivateReplyTestRow).where(
                PrivateReplyTestRow.tenant_id == tenant, PrivateReplyTestRow.account_id == account))
            if marker is None:
                return False
            current = await session.scalar(select(OutreachEnrollmentRow).where(
                OutreachEnrollmentRow.tenant_id == tenant, OutreachEnrollmentRow.enrollment_id == enrollment))
            point = await session.scalar(select(ContactPointRow.value).where(
                ContactPointRow.tenant_id == tenant, ContactPointRow.contact_point_id == contact))
            sender = await session.scalar(select(SendingIdentityRow.address).where(
                SendingIdentityRow.tenant_id == tenant, SendingIdentityRow.identity_id == marker.sending_identity_id))
            if (current is None or current.account_id != account or current.contact_point_id != contact
                or marker.contact_point_id != contact or current.sending_identity_id != marker.sending_identity_id
                or current.source_hypothesis_id is not None or current.execution_mode != "manual"
                or point != "986647123@qq.com" or sender != "1249583590@qq.com"):
                raise ValidationError("私有测试用途关联已变化")
            return True


class PrivateReplyContactEligibility:
    """测试用途读取器装饰原资格端口；没有标记时保持全部正式业务规则。"""
    def __init__(self, sessions: async_sessionmaker[AsyncSession], fallback: ContactEligibilityProvider, *, now: Callable[[], datetime]) -> None:
        self._sessions, self._fallback, self._now = sessions, fallback, now

    async def get_contact_eligibility(self, tenant_id: TenantId, contact_point_id: ContactPointId, account_id: ProspectAccountId) -> ContactEligibilitySnapshot:
        from domains.outreach.schemas import (
            ContactEligibilitySnapshot,
            ContactLegalBasis,
            ContactVerificationStatus,
            PrivateReplyTestPermission,
        )
        from infra.db.tables import (
            ContactLegalBasisRow,
            EmployeeRow,
            OwnershipLockRow,
            ProspectContactRow,
        )
        async with self._sessions() as session:
            marker = await session.scalar(select(PrivateReplyTestRow).where(
                PrivateReplyTestRow.tenant_id == tenant_id, PrivateReplyTestRow.account_id == account_id))
            if marker is not None:
                row = (await session.execute(
                    select(ContactPointRow, ContactLegalBasisRow, EmployeeRow)
                    .select_from(ContactPointRow)
                    .join(ProspectContactRow, (ProspectContactRow.tenant_id == tenant_id)
                        & (ProspectContactRow.contact_id == ContactPointRow.contact_id)
                        & (ProspectContactRow.account_id == account_id))
                    .join(ContactLegalBasisRow, (ContactLegalBasisRow.tenant_id == tenant_id)
                        & (ContactLegalBasisRow.contact_point_id == contact_point_id))
                    .join(OwnershipLockRow, (OwnershipLockRow.tenant_id == tenant_id)
                        & (OwnershipLockRow.account_id == account_id) & (OwnershipLockRow.owner == marker.owner_id))
                    .join(EmployeeRow, (EmployeeRow.tenant_id == tenant_id) & (EmployeeRow.employee_id == marker.owner_id)
                        & EmployeeRow.is_active.is_(True) & EmployeeRow.role.in_(("boss", "manager", "sales")))
                    .where(ContactPointRow.tenant_id == tenant_id, ContactPointRow.contact_point_id == contact_point_id)
                )).one_or_none()
                sender = await session.scalar(select(SendingIdentityRow.address).where(
                    SendingIdentityRow.tenant_id == tenant_id, SendingIdentityRow.identity_id == marker.sending_identity_id))
                if (marker.contact_point_id != contact_point_id or row is None or not row[2].user_id
                    or row[0].kind != "email" or row[0].value != "986647123@qq.com" or sender != "1249583590@qq.com"
                    or row[0].verification_status != "unverified" or row[0].verified_at is not None
                    or row[1].basis != "consent" or row[1].source != "private_reply_test_confirmation"):
                    raise ValidationError("私有测试联系人当前授权不完整")
                return ContactEligibilitySnapshot(
                    tenant_id, contact_point_id, account_id, ContactVerificationStatus.UNVERIFIED,
                    None, ContactLegalBasis.CONSENT, "private_reply_test_confirmation", True,
                    "ZZ", "private_test", frozenset(), self._now(),
                    PrivateReplyTestPermission(marker.sending_identity_id, marker.owner_id),
                )
        return await self._fallback.get_contact_eligibility(tenant_id, contact_point_id, account_id)


class PrivateReplyTestStore:
    """在现有客户表创建明确的私有测试对象；不取得公开候选或验证联系方式。"""
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, now: Callable[[], datetime]) -> None:
        self._sessions, self._now = sessions, now

    async def ensure(self, tenant: TenantId, employee: EmployeeId, user: UserId, sender: SendingIdentityId) -> tuple[ProspectAccountId, ContactPointId, datetime]:
        import hashlib

        from sqlalchemy import text

        from infra.db.tables import (
            ContactLegalBasisRow,
            EmployeeRow,
            OwnershipLockRow,
            ProspectAccountRow,
            ProspectContactRow,
        )
        from shared.errors import PermissionDenied
        from shared.schemas.identifiers import new_id
        async with self._sessions.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "private-reply-test:" + tenant})
            current = await db.scalar(select(EmployeeRow).where(
                EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee,
                EmployeeRow.user_id == user, EmployeeRow.is_active.is_(True),
                EmployeeRow.role.in_(("boss", "manager", "sales"))).with_for_update(read=True))
            address = await db.scalar(select(SendingIdentityRow.address).where(
                SendingIdentityRow.tenant_id == tenant, SendingIdentityRow.identity_id == sender))
            if current is None or address != "1249583590@qq.com":
                raise PermissionDenied("私有测试身份或发件地址不匹配")
            existing = (await db.scalars(select(PrivateReplyTestRow).where(
                PrivateReplyTestRow.tenant_id == tenant, PrivateReplyTestRow.owner_id == employee))).all()
            if existing:
                if len(existing) != 1 or existing[0].sending_identity_id != sender:
                    raise PermissionDenied("私有测试对象已绑定其他发件身份")
                marker = existing[0]
                owner = await db.scalar(select(OwnershipLockRow).where(
                    OwnershipLockRow.tenant_id == tenant, OwnershipLockRow.account_id == marker.account_id,
                    OwnershipLockRow.owner == employee))
                if owner is None:
                    raise PermissionDenied("私有测试负责人已改变")
                return marker.account_id, marker.contact_point_id, owner.locked_at
            value_hash = hashlib.sha256(b"986647123@qq.com").hexdigest()
            conflict = await db.scalar(select(ContactPointRow.contact_point_id).where(
                ContactPointRow.tenant_id == tenant, ContactPointRow.kind == "email",
                ContactPointRow.value_hash == value_hash))
            if conflict is not None:
                raise ValidationError("该邮箱已有其他联系人记录，不允许自动改作私有测试")
            stamp = self._now()
            account, contact, point = new_id("acc"), new_id("pc"), new_id("cp")
            db.add(ProspectAccountRow(tenant_id=tenant, account_id=account,
                name="私有回信测试（非商业客户）", country="ZZ", created_at=stamp,
                source_signal_refs=[], field_provenance={}))
            await db.flush()
            db.add(ProspectContactRow(tenant_id=tenant, contact_id=contact, account_id=account,
                full_name="本人控制的测试收件人", created_at=stamp))
            await db.flush()
            db.add(ContactPointRow(tenant_id=tenant, contact_point_id=point, contact_id=contact,
                kind="email", value="986647123@qq.com", value_hash=value_hash,
                verification_status="unverified", created_at=stamp))
            await db.flush()
            db.add(ContactLegalBasisRow(tenant_id=tenant, contact_point_id=point, basis="consent",
                subject_type="natural_person", contact_type="personal_business",
                source="private_reply_test_confirmation", collected_at=stamp))
            db.add(OwnershipLockRow(tenant_id=tenant, lock_id=new_id("own"), account_id=account,
                owner=employee, locked_at=stamp, locked_by_rule="private_reply_test"))
            db.add(PrivateReplyTestRow(tenant_id=tenant, account_id=account,
                contact_point_id=point, sending_identity_id=sender, owner_id=employee,
                created_by=employee, created_at=stamp))
            return account, point, stamp


    async def options(self, tenant: TenantId, employee: EmployeeId, user: UserId) -> tuple[PrivateReplyTestOption, ...]:
        from sqlalchemy import func

        from domains.prospecting.private_reply_test import PrivateReplyTestOption
        from infra.db.tables import (
            EmployeeRow,
            OutreachCampaignRow,
            OutreachCampaignVersionRow,
            OutreachSequenceStepRow,
        )
        async with self._sessions() as db:
            current = await db.scalar(select(EmployeeRow.employee_id).where(
                EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee,
                EmployeeRow.user_id == user, EmployeeRow.is_active.is_(True)))
            if current is None:
                raise ValidationError("当前私有测试员工不可用")
            sid = await db.scalar(select(SendingIdentityRow.identity_id).where(
                SendingIdentityRow.tenant_id == tenant, SendingIdentityRow.address == "1249583590@qq.com"))
            if sid is None:
                return ()
            count = select(func.count()).select_from(OutreachSequenceStepRow).where(
                OutreachSequenceStepRow.tenant_id == tenant,
                OutreachSequenceStepRow.campaign_id == OutreachCampaignVersionRow.campaign_id,
                OutreachSequenceStepRow.version == OutreachCampaignVersionRow.version).correlate(OutreachCampaignVersionRow).scalar_subquery()
            rows = (await db.scalars(select(OutreachCampaignVersionRow).join(
                OutreachCampaignRow, (OutreachCampaignRow.tenant_id == tenant)
                & (OutreachCampaignRow.campaign_id == OutreachCampaignVersionRow.campaign_id)
                & (OutreachCampaignRow.current_version == OutreachCampaignVersionRow.version)
                & (OutreachCampaignRow.state == "active"))
                .where(OutreachCampaignVersionRow.tenant_id == tenant,
                    OutreachCampaignVersionRow.sender_identity_ids == [sid], count == 1)
                .order_by(OutreachCampaignVersionRow.created_at.desc()).limit(100))).all()
            return tuple(PrivateReplyTestOption(campaign_id=row.campaign_id, sending_identity_id=sid, name=row.name) for row in rows)
