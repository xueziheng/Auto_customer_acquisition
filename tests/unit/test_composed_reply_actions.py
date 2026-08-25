"""生产回复动作组合：只用元数据 ID 重读证据并调用公共域服务。"""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domains.outreach.schemas import DeliveryFeedbackTarget
from domains.sending_identity.service import DeliveryEventType
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from workflows.reply_qualification.ports import ReplyActionContext, ReplyMessageContent

NOW = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
MESSAGE_ID = MessageId(new_id("msg"))
OUTBOUND_MESSAGE_ID = OutboundMessageId(
    f"<reply-route.{'a' * 64}@messages.tradeos.invalid>"
)
ENROLLMENT_ID = EnrollmentId(new_id("enr"))
ACCOUNT_ID = ProspectAccountId(new_id("acc"))
CONTACT_POINT_ID = ContactPointId(new_id("cp"))
SENDING_IDENTITY_ID = SendingIdentityId(new_id("sid"))
CONTEXT = ReplyActionContext(
    message_id=MESSAGE_ID,
    outbound_message_id=OUTBOUND_MESSAGE_ID,
    enrollment_id=ENROLLMENT_ID,
    account_id=ACCOUNT_ID,
    contact_point_id=CONTACT_POINT_ID,
)


def _module():
    try:
        return importlib.import_module("apps.scheduler_worker.reply_actions")
    except ModuleNotFoundError:
        pytest.fail("生产 ReplyActionPorts 组合尚未实现")


class _Evidence:
    def __init__(
        self,
        category: str = "provides_specification",
        *,
        has_candidate_fields: bool = True,
    ) -> None:
        self.category = category
        self.has_candidate_fields = has_candidate_fields

    async def load(self, tenant_id: TenantId, message_id: MessageId):
        module = _module()
        assert tenant_id == TENANT
        assert message_id == CONTEXT.message_id
        quote = "We need 5000 stainless steel hinges."
        return module.ReplyEvidenceSnapshot(
            message_id=message_id,
            category=self.category,
            classified_by="reply-model-v3",
            classified_at=NOW,
            raw_artifact_ref="art_reply_actions_1",
            candidate_fields=(
                (
                    module.ReplyFieldSnapshot("product_category", "hinges", quote),
                    module.ReplyFieldSnapshot("quantity", "5000", quote),
                )
                if self.has_candidate_fields
                else ()
            ),
        )


class _Business:
    def __init__(self, *, present: bool = True) -> None:
        self.present = present

    async def load(self, tenant_id: TenantId, context: ReplyActionContext):
        module = _module()
        assert tenant_id == TENANT
        assert context == CONTEXT
        if not self.present:
            return None
        return module.ReplyBusinessFacts(
            need_id="need_reply_actions_1",
            hypothesis_id=None,
            opportunity_id="opp_reply_actions_1",
            account_name="Acme Imports",
            country="US",
            why_valuable="Customer supplied order specifications.",
            how_we_found_them="Demand signal to verified outreach reply.",
            validated_need_summary="Stainless steel hinges, quantity 5000.",
            missing_information=("destination", "required_by"),
            already_sent=("Discovery email",),
            commitments_made=(),
            suggested_next_step="Confirm destination and required date.",
        )


class _Content:
    async def load(self, tenant_id: TenantId, message_id: MessageId):
        assert tenant_id == TENANT
        assert message_id == CONTEXT.message_id
        return ReplyMessageContent(
            subject="Specifications",
            body="We need 5000 stainless steel hinges.",
        )


class _Demand:
    def __init__(self) -> None:
        self.updates: dict[tuple[str, str], dict[str, object]] = {}

    async def update_need_fields(
        self,
        tenant_id,
        need_id,
        fields,
        source_message_id,
        updated_by=None,
    ) -> None:
        del updated_by
        key = (str(tenant_id), str(source_message_id))
        existing = self.updates.get(key)
        if existing is not None and existing != fields:
            raise ValidationError("字段动作幂等冲突")
        self.updates[key] = fields

    async def promote_to_validated(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("已有 need 时不得重复晋升")


class _Opportunities:
    def __init__(self) -> None:
        self.requests: dict[str, object] = {}

    async def request_handoff(self, tenant_id, request, *, actor):
        del actor
        key = f"{tenant_id}:{request.opportunity_id}"
        self.requests.setdefault(key, request)
        return "hand_reply_actions_1"


@dataclass(frozen=True)
class _FeedbackEffect:
    target: DeliveryFeedbackTarget
    occurred_at: datetime


class _Outreach:
    def __init__(self, *, corrupt_target: bool = False) -> None:
        target_contact = (
            ContactPointId(new_id("cp")) if corrupt_target else CONTACT_POINT_ID
        )
        self.target = DeliveryFeedbackTarget(
            tenant_id=TENANT,
            attempt_id=new_id("mat"),
            enrollment_id=ENROLLMENT_ID,
            account_id=ACCOUNT_ID,
            contact_point_id=target_contact,
            sending_identity_id=SENDING_IDENTITY_ID,
        )
        self.bounces: dict[str, _FeedbackEffect] = {}
        self.complaints: dict[str, _FeedbackEffect] = {}
        self.lookups: list[object] = []

    async def get_enrollment(self, tenant_id, enrollment_id, *, actor):
        assert tenant_id == TENANT
        assert enrollment_id == ENROLLMENT_ID
        assert actor.scope.allowed_enrollment_ids == frozenset({ENROLLMENT_ID})
        return SimpleNamespace(
            tenant_id=TENANT,
            enrollment_id=ENROLLMENT_ID,
            account_id=ACCOUNT_ID,
            contact_point_id=CONTACT_POINT_ID,
            sending_identity_id=SENDING_IDENTITY_ID,
        )

    async def resolve_delivery_feedback(self, tenant_id, lookup, *, actor):
        assert tenant_id == TENANT
        assert lookup.deterministic_message_id == OUTBOUND_MESSAGE_ID
        assert actor.scope.allowed_sending_identity_ids == frozenset(
            {SENDING_IDENTITY_ID}
        )
        self.lookups.append(lookup)
        return self.target

    async def apply_hard_bounce(
        self, tenant_id, target, provider_event_id, occurred_at, *, actor
    ):
        assert tenant_id == TENANT
        assert actor.scope.allowed_sending_identity_ids == frozenset(
            {SENDING_IDENTITY_ID}
        )
        self.bounces.setdefault(
            provider_event_id, _FeedbackEffect(target, occurred_at)
        )

    async def apply_complaint(
        self, tenant_id, target, provider_event_id, occurred_at, *, actor
    ):
        assert tenant_id == TENANT
        assert actor.scope.allowed_sending_identity_ids == frozenset(
            {SENDING_IDENTITY_ID}
        )
        self.complaints.setdefault(
            provider_event_id, _FeedbackEffect(target, occurred_at)
        )


class _SendingIdentities:
    def __init__(self) -> None:
        self.events: dict[str, object] = {}

    async def record_delivery_event(
        self, tenant_id, identity_id, event, *, actor
    ) -> bool:
        assert tenant_id == TENANT
        assert identity_id == SENDING_IDENTITY_ID
        assert actor.scope.allowed_identity_ids == frozenset(
            {SENDING_IDENTITY_ID}
        )
        key = str(event.dedup_key)
        created = key not in self.events
        self.events.setdefault(key, event)
        return created


class _ConversationActions:
    def __init__(self) -> None:
        self.requests: dict[str, object] = {}

    async def enqueue_reply_work_action(self, tenant_id, request):
        assert tenant_id == TENANT
        self.requests.setdefault(str(request.idempotency_key), request)
        return SimpleNamespace(status="pending")


async def test_composed_actions_apply_evidence_and_handoff_idempotently() -> None:
    module = _module()
    demand = _Demand()
    opportunities = _Opportunities()
    actions = module.ComposedReplyActionPorts(
        tenant_id=TENANT,
        evidence=_Evidence(),
        business=_Business(),
        content=_Content(),
        demand=demand,
        opportunities=opportunities,
        outreach=_Outreach(),
        sending_identities=_SendingIdentities(),
        conversations=_ConversationActions(),
    )

    for _ in range(2):
        await actions.extract_need_fields(
            TENANT, CONTEXT, f"reply:extract_need_fields:{MESSAGE_ID}"
        )
        await actions.request_handoff(
            TENANT, CONTEXT, f"reply:handoff:{MESSAGE_ID}"
        )

    assert len(demand.updates) == 1
    fields = next(iter(demand.updates.values()))
    assert fields == {
        "product_category": {
            "value": "hinges",
            "quote": "We need 5000 stainless steel hinges.",
            "extracted_by": "reply-model-v3",
        },
        "quantity": {
            "value": "5000",
            "quote": "We need 5000 stainless steel hinges.",
            "extracted_by": "reply-model-v3",
        },
    }
    assert len(opportunities.requests) == 1
    packet = next(iter(opportunities.requests.values()))
    assert packet.opportunity_id == "opp_reply_actions_1"
    assert packet.trigger == "specification_file_received"
    assert packet.customer_verbatim == "We need 5000 stainless steel hinges."
    assert packet.customer_verbatim_provenance.source_id == MESSAGE_ID
    assert packet.customer_verbatim_provenance.source_quote == packet.customer_verbatim
    assert packet.evidence_links == ["art_reply_actions_1"]
    assert packet.missing_information == ["destination", "required_by"]
    assert packet.already_sent == ["Discovery email"]


async def test_composed_actions_fail_closed_without_business_mapping() -> None:
    module = _module()
    actions = module.ComposedReplyActionPorts(
        tenant_id=TENANT,
        evidence=_Evidence(),
        business=_Business(present=False),
        content=_Content(),
        demand=_Demand(),
        opportunities=_Opportunities(),
        outreach=_Outreach(),
        sending_identities=_SendingIdentities(),
        conversations=_ConversationActions(),
    )

    with pytest.raises(ValidationError, match="业务关联不存在"):
        await actions.request_handoff(
            TENANT, CONTEXT, f"reply:handoff:{MESSAGE_ID}"
        )


@pytest.mark.parametrize(
    "category,trigger",
    [
        ("requests_materials", "materials_requested"),
        ("requests_quote", "quote_requested"),
        ("requests_sample", "sample_requested"),
    ],
)
async def test_handoff_categories_do_not_require_extracted_fields(
    category: str,
    trigger: str,
) -> None:
    """资料/报价/样品请求本身就是接管证据，字段为空也不得漏接管。"""
    module = _module()
    opportunities = _Opportunities()
    actions = module.ComposedReplyActionPorts(
        tenant_id=TENANT,
        evidence=_Evidence(category, has_candidate_fields=False),
        business=_Business(),
        content=_Content(),
        demand=_Demand(),
        opportunities=opportunities,
        outreach=_Outreach(),
        sending_identities=_SendingIdentities(),
        conversations=_ConversationActions(),
    )

    await actions.request_handoff(
        TENANT, CONTEXT, f"reply:handoff:{MESSAGE_ID}"
    )

    packet = next(iter(opportunities.requests.values()))
    assert packet.trigger == trigger


@pytest.mark.parametrize(
    "category,method_name,action,collection",
    [
        ("bounce", "route_bounce", "route_bounce", "bounces"),
        (
            "complaint",
            "record_complaint",
            "record_complaint",
            "complaints",
        ),
    ],
)
async def test_composed_feedback_actions_apply_once_through_outreach(
    category: str,
    method_name: str,
    action: str,
    collection: str,
) -> None:
    module = _module()
    outreach = _Outreach()
    sending_identities = _SendingIdentities()
    actions = module.ComposedReplyActionPorts(
        tenant_id=TENANT,
        evidence=_Evidence(category),
        business=_Business(),
        content=_Content(),
        demand=_Demand(),
        opportunities=_Opportunities(),
        outreach=outreach,
        sending_identities=sending_identities,
        conversations=_ConversationActions(),
    )

    for _ in range(2):
        await getattr(actions, method_name)(
            TENANT, CONTEXT, f"reply:{action}:{MESSAGE_ID}"
        )

    assert len(outreach.lookups) == 2
    effects = getattr(outreach, collection)
    assert len(effects) == 1
    event_id = next(iter(effects))
    expected_material = (
        f"reply-action-v1\x00{TENANT}\x00{action}\x00"
        f"{MESSAGE_ID}\x00{OUTBOUND_MESSAGE_ID}"
    )
    assert event_id == hashlib.sha256(expected_material.encode()).hexdigest()
    assert effects[event_id].occurred_at == NOW
    assert list(sending_identities.events) == [event_id]
    reputation = sending_identities.events[event_id]
    expected_reputation = (
        DeliveryEventType.HARD_BOUNCED
        if category == "bounce"
        else DeliveryEventType.COMPLAINT
    )
    assert reputation.event_type is expected_reputation
    assert reputation.occurred_at == NOW


async def test_composed_feedback_actions_reject_corrupt_correlation() -> None:
    module = _module()
    actions = module.ComposedReplyActionPorts(
        tenant_id=TENANT,
        evidence=_Evidence("bounce"),
        business=_Business(),
        content=_Content(),
        demand=_Demand(),
        opportunities=_Opportunities(),
        outreach=_Outreach(corrupt_target=True),
        sending_identities=_SendingIdentities(),
        conversations=_ConversationActions(),
    )

    with pytest.raises(ValidationError, match="关联不匹配"):
        await actions.route_bounce(
            TENANT, CONTEXT, f"reply:route_bounce:{MESSAGE_ID}"
        )


@pytest.mark.parametrize(
    "method_name,action",
    [
        ("start_qualification", "start_qualification"),
        ("mark_future_restart", "mark_future_restart"),
        ("create_follow_up", "create_follow_up"),
        ("intake_new_contact", "intake_new_contact"),
    ],
)
async def test_composed_actions_create_durable_conversation_work_once(
    method_name: str,
    action: str,
) -> None:
    module = _module()
    conversations = _ConversationActions()
    actions = module.ComposedReplyActionPorts(
        tenant_id=TENANT,
        evidence=_Evidence(),
        business=_Business(),
        content=_Content(),
        demand=_Demand(),
        opportunities=_Opportunities(),
        outreach=_Outreach(),
        sending_identities=_SendingIdentities(),
        conversations=conversations,
    )

    for _ in range(2):
        await getattr(actions, method_name)(
            TENANT, CONTEXT, f"reply:{action}:{MESSAGE_ID}"
        )

    assert len(conversations.requests) == 1
    request = next(iter(conversations.requests.values()))
    assert request.message_id == MESSAGE_ID
    assert request.outbound_message_id == OUTBOUND_MESSAGE_ID
    assert request.enrollment_id == ENROLLMENT_ID
    assert request.account_id == ACCOUNT_ID
    assert request.contact_point_id == CONTACT_POINT_ID
    assert request.action.value == action
    assert str(request.idempotency_key) == f"reply:{action}:{MESSAGE_ID}"
