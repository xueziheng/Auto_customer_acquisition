"""普通对话每次动作的当前范围组合；来源关联不能替代批准或原件权限。"""
from dataclasses import asdict, dataclass

from domains.conversations.dialogue_access import (
    DialogueOwnerAccess,
    DialogueOwnerSnapshot,
)
from domains.conversations.inbox_access import InboxActor
from domains.outreach.ordinary_dialogue_policy import OrdinaryDialoguePolicy
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import SuppressionTarget
from domains.outreach.service import OutreachService
from domains.products.permissions import ProductActor, ProductRole
from domains.products.service import ProductCustomerView, ProductService
from domains.prospecting.service import ProspectingService
from shared.errors import PermissionDenied, PolicyViolation, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    EmployeeId,
    MessageId,
    ProductId,
    TenantId,
)
from shared.schemas.quote_facts import canonical_fact_hash


@dataclass(frozen=True)
class CurrentDialogueScope:
    owner: DialogueOwnerSnapshot
    policy: OrdinaryDialoguePolicy
    products: tuple[ProductCustomerView, ...]


class CurrentOrdinaryDialogueAccess:
    """仅将当前公开领域事实取交集；各领域仍拥有自身权限与事实规则。"""

    def __init__(self, owners: DialogueOwnerAccess, outreach: OutreachService,
                 products: ProductService, prospects: ProspectingService) -> None:
        self.owners, self.outreach, self.products, self.prospects = owners, outreach, products, prospects

    async def require(self, tenant_id: TenantId, message_id: MessageId,
                      employee_id: EmployeeId) -> InboxActor:
        return (await self.current(tenant_id, message_id, employee_id)).owner.actor

    async def require_original(self, tenant_id: TenantId, message_id: MessageId,
                               employee_id: EmployeeId) -> InboxActor:
        return (await self.owners.require(tenant_id, message_id, employee_id)).actor

    async def current(self, tenant_id: TenantId, message_id: MessageId,
                      employee_id: EmployeeId) -> CurrentDialogueScope:
        """每次调用重读，不缓存scope；失败不回退为冷发送或system原件读取。"""
        owner = await self.owners.require(tenant_id, message_id, employee_id)
        source = owner.source
        if (source.tenant_id != tenant_id or source.owner_employee_id != employee_id
            or owner.actor.employee_id != employee_id or owner.actor.tenant_id != tenant_id
            or owner.latest_message_id != message_id or owner.binding_state != "active"
            or owner.pending_inbound):
            raise PermissionDenied("当前普通对话范围拒绝")
        try:
            campaign = await self.outreach.get_ordinary_dialogue_scope(
                tenant_id, CampaignId(source.campaign_id), version=source.campaign_version,
                owner_employee_id=employee_id,
                actor=Actor(str(employee_id), OutreachScope(
                    level={"boss": ScopeLevel.TENANT, "manager": ScopeLevel.MANAGER,
                           "sales": ScopeLevel.SELF}[owner.actor.role],
                    allowed_campaign_ids=frozenset({CampaignId(source.campaign_id)})), owner.actor.role))
        except (ValidationError, PolicyViolation):
            raise PermissionDenied("当前普通对话范围拒绝") from None
        policy = campaign.boundary.ordinary_dialogue_policy
        if (policy is None or campaign.approval_id != source.approval_id
            or source.sending_identity_id not in policy.reply_sender_identity_ids):
            raise PermissionDenied("当前普通对话范围拒绝")
        contact = await self.prospects.get_outreach_contact_facts(
            tenant_id, source.account_id, source.contact_point_id)
        if (contact.tenant_id != tenant_id or contact.account_id != source.account_id
            or contact.contact_point_id != source.contact_point_id or contact.kind.value != "email"
            or contact.verification.value != "verified" or contact.verified_at is None
            or not contact.legal_basis_ref or contact.country not in campaign.boundary.markets
            or contact.entity_type not in campaign.boundary.target_entity_types):
            raise PermissionDenied("当前普通对话联系人拒绝")
        for target in (SuppressionTarget(contact_point_id=source.contact_point_id),
                       SuppressionTarget(account_id=source.account_id)):
            suppressed = await self.outreach.is_suppressed(tenant_id, target,
                actor=Actor("system:dialogue-suppression", OutreachScope(level=ScopeLevel.SYSTEM,
                    allowed_suppression_targets=frozenset({target.canonical_id})), "system"))
            if suppressed is not None:
                raise PermissionDenied("当前普通对话已停止")
        products = []
        for reference in policy.product_refs:
            try:
                visible = await self.products.get_ordinary_dialogue_view(tenant_id, ProductId(reference.product_id),
                    actor=ProductActor(str(employee_id), ProductRole.BOSS if owner.actor.role == "boss" else ProductRole.SALES, tenant_id))
            except ValidationError:
                raise PermissionDenied("普通对话产品范围已改变") from None
            if (visible.product_id != reference.product_id
                or visible.category.strip().lower() not in campaign.boundary.allowed_categories
                or not visible.inquiry_enabled
                or canonical_fact_hash(asdict(visible)) != reference.customer_view_sha256):
                raise PermissionDenied("普通对话产品范围已改变")
            products.append(visible)
        # 最终再取一次原件owner交集，避免跨领域读取期间转交客户或轮换邮箱。
        final_owner = await self.owners.require(tenant_id, message_id, employee_id)
        if final_owner != owner:
            raise PermissionDenied("当前普通对话范围已改变")
        return CurrentDialogueScope(owner, policy, tuple(products))
