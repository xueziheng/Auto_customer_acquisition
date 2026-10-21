"""批准scope与当前owner事实交集，不把绑定或产品摘要当作发送许可。"""
from dataclasses import asdict, replace

import pytest

from domains.conversations.dialogue_access import DialogueOwnerSnapshot
from domains.conversations.dialogue_source import DialogueSource
from domains.conversations.inbox_access import InboxActor, InboxScope
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import CampaignApprovalSnapshot, CampaignApprovalState
from domains.prospecting.schemas import OutreachContactFacts
from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id
from shared.schemas.quote_facts import canonical_fact_hash
from tests.unit.test_ordinary_dialogue_policy import policy_input
from tests.unit.test_outreach_service import NOW, SENDER, TENANT, _request, _service
from tests.unit.test_product_service import _formal_product, _Products
from tests.unit.test_product_service import _service as product_service


async def test_authority_uses_current_scope_owner_source_and_customer_view():
    from workflows.reply_qualification.dialogue_authority import (
        CurrentOrdinaryDialogueAccess,
    )
    outreach, _, _, approvals, _ = _service()
    product = _formal_product(new_id("prd"))
    product.tenant_id = TENANT
    products, _, _ = product_service(products=_Products([product]))
    from domains.products.permissions import ProductActor, ProductRole
    visible = await products.get_ordinary_dialogue_view(TENANT, product.product_id,
        actor=ProductActor("system:dialogue", ProductRole.SYSTEM, TENANT))
    owner = new_id("emp")
    boss = Actor(new_id("emp"), OutreachScope(level=ScopeLevel.TENANT), "boss")
    policy = type(policy_input()).model_validate(policy_input().model_dump() | {"owner_employee_id": owner,
        "product_refs": ({"product_id": product.product_id, "customer_view_sha256": canonical_fact_hash(asdict(visible))},)})
    campaign = await outreach.create_campaign(TENANT, replace(_request(), allowed_categories=("hinges",), ordinary_dialogue_policy=policy), actor=boss)
    await outreach.submit_campaign(TENANT, campaign.campaign_id, actor=boss)
    approval = CampaignApprovalSnapshot(TENANT, campaign.campaign_id, 1, new_id("apr"),
        CampaignApprovalState.APPROVED, new_id("emp"), NOW)
    approvals.values[(campaign.campaign_id, 1)] = approval
    await outreach.activate_campaign(TENANT, campaign.campaign_id, actor=boss)
    message = new_id("msg")
    source = DialogueSource(TENANT, new_id("dlg"), new_id("acc"), new_id("cp"), new_id("hyp"),
        SENDER, campaign.campaign_id, 1, approval.approval_id, owner, NOW, "current-op", "inquiry",
        None, new_id("bmi"), message, "a" * 64)
    snapshot = DialogueOwnerSnapshot(source, InboxActor(TENANT, owner, "sales", InboxScope.SELF,
        frozenset({owner})), new_id("usr"), message, "active", 0, 0, 0)

    class Owners:
        async def require(self, tenant, msg, employee):
            assert (tenant, msg) == (TENANT, message)
            if employee != owner:
                raise PermissionDenied("owner")
            return snapshot

    class Prospects:
        async def get_outreach_contact_facts(self, tenant, account, contact):
            return OutreachContactFacts(tenant_id=tenant, account_id=account, contact_point_id=contact,
                kind="email", verification="verified", verified_at=NOW, legal_basis="consent",
                legal_basis_ref="confirmed-consent", country="US", entity_type="importer")

    authority = CurrentOrdinaryDialogueAccess(Owners(), outreach, products, Prospects())
    assert (await authority.require(TENANT, message, owner)).role == "sales"
    with pytest.raises(PermissionDenied):
        await authority.require(TENANT, message, boss.actor_id)
    product.name_en = "Changed customer product"
    with pytest.raises(PermissionDenied):
        await authority.require(TENANT, message, owner)
    product.name_en = "Hinge"
    snapshot = replace(snapshot, latest_message_id=new_id("msg"))
    with pytest.raises(PermissionDenied):
        await authority.require(TENANT, message, owner)
    snapshot = replace(snapshot, latest_message_id=message)
    await outreach.pause_campaign(TENANT, campaign.campaign_id, "withdrawn", actor=boss)
    with pytest.raises(PermissionDenied):
        await authority.require(TENANT, message, owner)
