"""本人业务询盘的显式准入桥；分类结果不代表联系人、授权或购买意愿。"""
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from domains.conversations.business_mail import BusinessMailActor
from domains.conversations.dialogue_source import DialogueSource
from domains.conversations.service import ConversationService
from domains.demand.service import DemandService, SignalCaptureRequest
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import SuppressionTarget
from domains.outreach.service import OutreachService
from domains.products.permissions import ProductActor, ProductRole
from domains.products.service import ProductService
from domains.prospecting.service import ProspectingService
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.business_reply import validate_reply_message_id
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    MessageId,
    ProductId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from shared.schemas.quote_facts import canonical_fact_hash


class InquiryAdmissionRequest(BaseModel):
    """用户选现有客户、联系人、产品与精确批准范围，并指出客户表达的原话。"""
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    campaign_id: CampaignId
    campaign_version: int = Field(strict=True, gt=0)
    product_id: ProductId
    customer_quote: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class InquiryAdmissionFacts:
    role: str
    account_name: str
    ownership_locked_at: datetime
    sending_identity_id: SendingIdentityId
    sender_role: str
    mailbox_operation_id: str
    artifact_id: str
    source_sha256: str
    external_message_id: str
    received_at: datetime
    customer_body: str
    contact_matches_sender: bool


class InquiryAdmissionTransaction(Protocol):
    conversations: ConversationService
    demand: DemandService

    async def facts(self, actor: BusinessMailActor, business_message_id: str,
                    request: InquiryAdmissionRequest) -> InquiryAdmissionFacts:
        """锁住当前本人原件、客户owner和真实联系人，返回最小准入投影。"""
        ...

    async def existing(self, actor: BusinessMailActor, business_message_id: str) -> DialogueSource | None: ...
    async def bind(self, source: DialogueSource) -> None: ...
    async def recheck(self, actor: BusinessMailActor, business_message_id: str) -> None: ...


class InquiryAdmission:
    def __init__(self, transactions: Callable[[TenantId], AbstractAsyncContextManager[InquiryAdmissionTransaction]],
                 outreach: OutreachService, products: ProductService, prospects: ProspectingService) -> None:
        self.transactions, self.outreach, self.products, self.prospects = transactions, outreach, products, prospects

    async def admit(self, actor: BusinessMailActor, business_message_id: str,
                    request: InquiryAdmissionRequest) -> DialogueSource:
        """同事务落 canonical Message/假设/来源/outbox，绝不建立冷 Enrollment。"""
        tenant = actor.tenant_id
        async with self.transactions(tenant) as tx:
            facts = await tx.facts(actor, business_message_id, request)
            validate_reply_message_id(facts.external_message_id)
            if (not facts.contact_matches_sender or not request.customer_quote.strip()
                or request.customer_quote not in facts.customer_body
                or facts.sender_role not in {"cold_outreach", "primary_business"}):
                raise PermissionDenied("询盘原件、联系人或回复身份未获准")
            campaign = await self.outreach.get_ordinary_dialogue_scope(tenant, request.campaign_id,
                version=request.campaign_version, owner_employee_id=actor.employee_id,
                actor=Actor(str(actor.employee_id), OutreachScope(
                    level={"boss": ScopeLevel.TENANT, "manager": ScopeLevel.MANAGER, "sales": ScopeLevel.SELF}[facts.role],
                    allowed_campaign_ids=frozenset({request.campaign_id})), facts.role))
            policy = campaign.boundary.ordinary_dialogue_policy
            if policy is None or facts.sending_identity_id not in policy.reply_sender_identity_ids or not campaign.approval_id:
                raise PermissionDenied("询盘缺少精确普通对话授权")
            refs = [ref for ref in policy.product_refs if ref.product_id == request.product_id]
            if len(refs) != 1:
                raise PermissionDenied("询盘产品未获准")
            product = await self.products.get_ordinary_dialogue_view(tenant, request.product_id,
                actor=ProductActor(str(actor.employee_id), ProductRole.BOSS if facts.role == "boss" else ProductRole.SALES, tenant))
            if (canonical_fact_hash(asdict(product)) != refs[0].customer_view_sha256
                or product.category.strip().lower() not in campaign.boundary.allowed_categories
                or not product.inquiry_enabled):
                raise PermissionDenied("询盘产品范围已变化")
            contact = await self.prospects.get_outreach_contact_facts(tenant, request.account_id, request.contact_point_id)
            if (contact.tenant_id != tenant or contact.account_id != request.account_id
                or contact.contact_point_id != request.contact_point_id or contact.kind.value != "email"
                or contact.verification.value != "verified" or contact.verified_at is None
                or not contact.legal_basis_ref or contact.country not in campaign.boundary.markets
                or contact.entity_type not in campaign.boundary.target_entity_types):
                raise PermissionDenied("询盘联系人尚未准入")
            for target in (SuppressionTarget(account_id=request.account_id), SuppressionTarget(contact_point_id=request.contact_point_id)):
                if await self.outreach.is_suppressed(tenant, target, actor=Actor("system:inquiry-suppression",
                    OutreachScope(level=ScopeLevel.SYSTEM, allowed_suppression_targets=frozenset({target.canonical_id})), "system")):
                    raise PermissionDenied("询盘已被抑制")
            existing = await tx.existing(actor, business_message_id)
            if existing is not None:
                if (existing.account_id != request.account_id or existing.contact_point_id != request.contact_point_id
                    or existing.campaign_id != request.campaign_id or existing.campaign_version != request.campaign_version
                    or existing.owner_employee_id != actor.employee_id or existing.mailbox_operation_id != facts.mailbox_operation_id):
                    raise ValidationError("询盘准入来源不可改写")
                await tx.recheck(actor, business_message_id)
                return existing
            message = await tx.conversations.ingest_inbound(tenant, None, request.account_id,
                facts.artifact_id, facts.external_message_id, facts.received_at)
            # 人工确认的逐字表达是信号；产品选择只限定假设类别，不是客户需求事实。
            signal = await tx.demand.capture_signal(tenant, SignalCaptureRequest(
                "inbound_inquiry", facts.account_name, request.customer_quote, facts.received_at,
                "conversation", str(message), str(actor.employee_id)))
            hypothesis = await tx.demand.create_hypothesis(tenant, request.account_id, product.category.strip().lower(),
                [signal], "负责人将客户询盘与获准产品范围关联，实际需求仍需客户原文验证", str(actor.employee_id))
            source = DialogueSource(tenant_id=tenant, binding_id=new_id("dlg"), account_id=request.account_id,
                contact_point_id=request.contact_point_id, hypothesis_id=hypothesis,
                sending_identity_id=facts.sending_identity_id, campaign_id=request.campaign_id,
                campaign_version=request.campaign_version, approval_id=campaign.approval_id,
                owner_employee_id=actor.employee_id, ownership_locked_at=facts.ownership_locked_at,
                mailbox_operation_id=facts.mailbox_operation_id, source_kind="inquiry", enrollment_id=None,
                business_message_id=business_message_id, admitted_message_id=MessageId(message),
                admitted_source_sha256=facts.source_sha256)
            await tx.bind(source)
            await tx.recheck(actor, business_message_id)
            return source


class InquiryAdmissionView(BaseModel):
    """准入结果仅给现有业务对象引用，不暴露执行租约或模型绑定。"""
    model_config = ConfigDict(frozen=True, extra='forbid')
    message_id: MessageId
    hypothesis_id: str
    state: str = 'admitted'
