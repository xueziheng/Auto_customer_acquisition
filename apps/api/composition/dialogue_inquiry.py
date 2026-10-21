"""本人询盘的最小准入选择；不扩大Campaign列表或其他邮箱的读取权限。"""
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.business_mail import BusinessMailActor, BusinessMailService
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.service import OutreachService
from domains.products.permissions import ProductActor, ProductRole
from domains.products.service import ProductService
from infra.db.tables import (
    ContactPointRow,
    EmployeeRow,
    OutreachCampaignRow,
    OutreachCampaignVersionRow,
    OwnershipLockRow,
    ProspectAccountRow,
    ProspectContactRow,
)
from shared.errors import PermissionDenied, PolicyViolation, ValidationError
from shared.schemas.identifiers import CampaignId, ProductId


class InquiryContactChoice(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    account_id: str
    account_name: str
    contact_point_id: str


class InquiryProductChoice(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    product_id: str
    name: str


class InquiryScopeChoice(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    campaign_id: str
    campaign_version: int
    name: str
    products: list[InquiryProductChoice]


class InquiryChoices(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    contacts: list[InquiryContactChoice]
    scopes: list[InquiryScopeChoice]


class OrdinaryInquiryChoices:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], mail: BusinessMailService,
                 outreach: OutreachService, products: ProductService) -> None:
        self.sessions, self.mail, self.outreach, self.products = sessions, mail, outreach, products

    async def read(self, actor: BusinessMailActor, message_id: str) -> InquiryChoices:
        """原件鉴权在查询前后执行；这里只给候选，POST仍重核每项准入事实。"""
        original = await self.mail.detail(actor, message_id)
        tenant = actor.tenant_id
        async with self.sessions() as db:
            employee = await db.scalar(select(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == actor.employee_id))
            if employee is None or not employee.is_active or employee.user_id != actor.user_id:
                raise PermissionDenied('普通询盘当前员工不可用')
            role = employee.role
            contacts = (await db.execute(select(ProspectAccountRow.account_id, ProspectAccountRow.name, ContactPointRow.contact_point_id)
                .join(ProspectContactRow, (ProspectContactRow.tenant_id == ProspectAccountRow.tenant_id) & (ProspectContactRow.account_id == ProspectAccountRow.account_id))
                .join(ContactPointRow, (ContactPointRow.tenant_id == ProspectContactRow.tenant_id) & (ContactPointRow.contact_id == ProspectContactRow.contact_id))
                .join(OwnershipLockRow, (OwnershipLockRow.tenant_id == ProspectAccountRow.tenant_id) & (OwnershipLockRow.account_id == ProspectAccountRow.account_id))
                .where(ProspectAccountRow.tenant_id == tenant, ProspectContactRow.tenant_id == tenant,
                    ContactPointRow.tenant_id == tenant, OwnershipLockRow.tenant_id == tenant,
                    OwnershipLockRow.owner == actor.employee_id, ContactPointRow.kind == 'email',
                    ContactPointRow.value == original.sender, ContactPointRow.verification_status == 'verified').limit(20))).all()
            candidates = (await db.execute(select(OutreachCampaignRow.campaign_id, OutreachCampaignRow.current_version)
                .join(OutreachCampaignVersionRow, (OutreachCampaignVersionRow.tenant_id == OutreachCampaignRow.tenant_id) &
                    (OutreachCampaignVersionRow.campaign_id == OutreachCampaignRow.campaign_id) &
                    (OutreachCampaignVersionRow.version == OutreachCampaignRow.current_version))
                .where(OutreachCampaignRow.tenant_id == tenant, OutreachCampaignVersionRow.tenant_id == tenant,
                    OutreachCampaignRow.state == 'active',
                    OutreachCampaignVersionRow.ordinary_dialogue_policy['owner_employee_id'].astext == actor.employee_id).limit(100))).all()
        scopes = []
        for campaign_id, version in candidates:
            try:
                campaign = await self.outreach.get_ordinary_dialogue_scope(tenant, CampaignId(campaign_id), version=version,
                    owner_employee_id=actor.employee_id, actor=Actor(str(actor.employee_id), OutreachScope(level={'boss':ScopeLevel.TENANT,'manager':ScopeLevel.MANAGER,'sales':ScopeLevel.SELF}[role],
                        allowed_campaign_ids=frozenset({CampaignId(campaign_id)})), role))
                policy = campaign.boundary.ordinary_dialogue_policy
                if policy is None:
                    continue
                products = []
                for ref in policy.product_refs:
                    view = await self.products.get_ordinary_dialogue_view(tenant, ProductId(ref.product_id),
                        actor=ProductActor(str(actor.employee_id), ProductRole.BOSS if role == 'boss' else ProductRole.SALES, tenant))
                    products.append(InquiryProductChoice(product_id=ref.product_id, name=view.name_en))
                scopes.append(InquiryScopeChoice(campaign_id=campaign_id, campaign_version=version, name=campaign.name, products=products))
            except (PermissionDenied, ValidationError, PolicyViolation):
                continue
        await self.mail.detail(actor, message_id)
        return InquiryChoices(contacts=[InquiryContactChoice(account_id=row[0], account_name=row[1], contact_point_id=row[2]) for row in contacts], scopes=scopes)
