"""把当前公开领域事实映射到触达只读端口；不产生发送或额度副作用。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domains.conversations.service import ConversationService
from domains.demand.service import DemandService
from domains.outreach.schemas import (
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from domains.prospecting.schemas import ContactPointKind
from domains.prospecting.service import ProspectingService
from domains.sending_identity.permissions import Actor, ScopeLevel, SendingIdentityScope
from domains.sending_identity.service import SendingIdentityService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
)


def sending_reader_actor(identity_id: SendingIdentityId) -> Actor:
    """复用域的单身份 SYSTEM scope，不新增授权规则。"""
    return Actor(
        "system:current-facts",
        SendingIdentityScope(
            level=ScopeLevel.SYSTEM, allowed_identity_ids=frozenset({identity_id})
        ),
        "system",
    )


class CurrentContactEligibilityReader:
    """企业类别只来自当前有证据需求假设，缺事实失败关闭。"""

    def __init__(
        self,
        tenant_id: TenantId,
        prospecting: ProspectingService,
        demand: DemandService,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._tenant = tenant_id
        self._prospecting = prospecting
        self._demand = demand
        self._now = now

    async def get_contact_eligibility(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ContactEligibilitySnapshot:
        if tenant_id != self._tenant:
            raise ValidationError("当前联系人事实不可用")
        fact = await self._prospecting.get_outreach_contact_facts(
            tenant_id, account_id, contact_point_id
        )
        categories = await self._demand.get_outreach_hypothesis_categories(
            tenant_id, account_id
        )
        if (
            fact.tenant_id != tenant_id
            or fact.account_id != account_id
            or fact.contact_point_id != contact_point_id
            or fact.kind is not ContactPointKind.EMAIL
            or categories.tenant_id != tenant_id
            or categories.account_id != account_id
            or not fact.entity_type
        ):
            raise ValidationError("当前联系人事实不可用")
        return ContactEligibilitySnapshot(
            tenant_id,
            contact_point_id,
            account_id,
            ContactVerificationStatus(fact.verification.value),
            fact.verified_at,
            ContactLegalBasis(fact.legal_basis.value),
            fact.legal_basis_ref,
            True,
            fact.country,
            fact.entity_type,
            frozenset(categories.categories),
            self._now(),
        )


class CurrentSendingIdentityReader:
    """只读现有认证、角色与许可结果，不保留地址、不预占发送名额。"""

    def __init__(
        self,
        tenant_id: TenantId,
        sending: SendingIdentityService,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._tenant = tenant_id
        self._sending = sending
        self._now = now

    async def get_sending_identity_eligibility(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> SendingIdentityEligibilitySnapshot:
        if tenant_id != self._tenant:
            raise ValidationError("当前发件身份事实不可用")
        actor = sending_reader_actor(identity_id)
        view = await self._sending.get(tenant_id, identity_id, actor=actor)
        permission = await self._sending.check_send_permission(
            tenant_id, identity_id, True, actor=actor
        )
        if view.identity_id != identity_id:
            raise ValidationError("当前发件身份事实不可用")
        return SendingIdentityEligibilitySnapshot(
            tenant_id,
            identity_id,
            OutreachSenderRole(view.role.value),
            view.auth is not None and view.auth.all_passed,
            permission.allowed,
            permission.remaining_today,
            self._now(),
        )


class CurrentReplyStatusReader:
    """精确核对联系人所属企业，再按企业级当前入站事实保守暂停。"""

    def __init__(
        self,
        tenant_id: TenantId,
        prospecting: ProspectingService,
        conversations: ConversationService,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._tenant = tenant_id
        self._prospecting = prospecting
        self._conversations = conversations
        self._now = now

    async def get_reply_status(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ReplyStatusSnapshot:
        if tenant_id != self._tenant:
            raise ValidationError("当前回复事实不可用")
        contact = await self._prospecting.get_outreach_contact_facts(
            tenant_id, account_id, contact_point_id
        )
        fact = await self._conversations.get_account_reply_status(tenant_id, account_id)
        if (
            contact.tenant_id != tenant_id
            or contact.account_id != account_id
            or contact.contact_point_id != contact_point_id
            or contact.kind is not ContactPointKind.EMAIL
            or fact.tenant_id != tenant_id
            or fact.account_id != account_id
        ):
            raise ValidationError("当前回复事实不可用")
        if fact.state == "unknown":
            raise TransientError("当前回复等待分类")
        return ReplyStatusSnapshot(
            tenant_id,
            contact_point_id,
            account_id,
            ReplyState(fact.state),
            fact.replied_at,
            self._now(),
        )
