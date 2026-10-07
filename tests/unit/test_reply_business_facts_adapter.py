"""回复业务事实 reader 只沿 Enrollment 的精确来源链读取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.outreach.schemas import DeliveryFeedbackTarget
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    MessageAttemptId,
    MessageId,
    NeedHypothesisId,
    OpportunityId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from workflows.reply_qualification.ports import ReplyActionContext

TENANT = TenantId(new_id("tn"))
ACCOUNT = ProspectAccountId(new_id("acc"))
CONTACT = ContactPointId(new_id("cp"))
ENROLLMENT = EnrollmentId(new_id("enr"))
HYPOTHESIS = NeedHypothesisId(new_id("hyp"))
NEED = ValidatedNeedId(new_id("need"))
OPPORTUNITY = OpportunityId(new_id("opp"))
ATTEMPT = MessageAttemptId(new_id("mat"))
SENDING_IDENTITY = SendingIdentityId(new_id("sid"))
CONTEXT = ReplyActionContext(
    message_id=MessageId(new_id("msg")),
    outbound_message_id=OutboundMessageId(
        f"<reply-route.{'a' * 64}@messages.tradeos.invalid>"
    ),
    enrollment_id=ENROLLMENT,
    account_id=ACCOUNT,
    contact_point_id=CONTACT,
)


@dataclass
class _Outreach:
    source_hypothesis_id: NeedHypothesisId | None = HYPOTHESIS
    mismatched_delivery: bool = False

    async def get_enrollment(self, tenant_id, enrollment_id, *, actor):
        del actor
        return SimpleNamespace(
            tenant_id=tenant_id,
            enrollment_id=enrollment_id,
            account_id=ACCOUNT,
            contact_point_id=CONTACT,
            sending_identity_id=SENDING_IDENTITY,
            source_hypothesis_id=self.source_hypothesis_id,
        )

    async def resolve_delivery_feedback(self, tenant_id, lookup, *, actor):
        del lookup, actor
        return DeliveryFeedbackTarget(
            tenant_id=tenant_id,
            attempt_id=ATTEMPT,
            enrollment_id=ENROLLMENT,
            account_id=(
                ProspectAccountId(new_id("acc"))
                if self.mismatched_delivery
                else ACCOUNT
            ),
            contact_point_id=CONTACT,
            sending_identity_id=SENDING_IDENTITY,
        )


class _Demand:
    async def get_hypothesis(self, tenant_id, hypothesis_id):
        del tenant_id
        return SimpleNamespace(
            hypothesis_id=str(hypothesis_id),
            account_id=str(ACCOUNT),
            account_name="Acme Components",
            category="industrial hinges",
            status="validated",
            validated_need_id=str(NEED),
        )

    async def get_need(self, tenant_id, need_id):
        del tenant_id
        return SimpleNamespace(
            need_id=str(need_id),
            account_id=str(ACCOUNT),
            account_name="Acme Components",
            product_category="industrial hinges",
            completeness=4,
            missing_for_sourcing=[],
        )


class _Prospecting:
    async def get_account(self, tenant_id, account_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            account_id=account_id,
            name="Acme Components",
            country="US",
        )

    async def get_contact_point(self, tenant_id, contact_point_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            contact_point_id=contact_point_id,
            account_id=ACCOUNT,
            verification=SimpleNamespace(value="verified"),
            verified_at=datetime(2026, 8, 25, 8, 0, tzinfo=UTC),
        )


class _Opportunities:
    async def get_by_need(self, tenant_id, need_id, *, actor):
        del tenant_id, actor
        assert need_id == NEED
        return SimpleNamespace(
            opportunity_id=str(OPPORTUNITY),
            need_id=str(NEED),
            account_id=str(ACCOUNT),
            product_category="industrial hinges",
        )


def _reader():
    from apps.scheduler_worker.adapters.reply_business_facts import (
        TenantBoundReplyBusinessFactsReader,
    )

    return TenantBoundReplyBusinessFactsReader(
        tenant_id=TENANT,
        outreach=_Outreach(),
        demand=_Demand(),
        prospecting=_Prospecting(),
        opportunities=_Opportunities(),
        opportunity_actor=Actor(
            "boss:reply-facts",
            OpportunityScope(level=ScopeLevel.TENANT),
            "boss",
        ),
    )


async def test_reader_returns_only_exact_linked_need_and_opportunity() -> None:
    facts = await _reader().load(TENANT, CONTEXT)

    assert facts is not None
    assert facts.hypothesis_id == HYPOTHESIS
    assert facts.need_id == NEED
    assert facts.opportunity_id == OPPORTUNITY
    assert facts.account_name == "Acme Components"
    assert facts.country == "US"
    assert facts.validated_need_summary == "industrial hinges；完整度 4/5"


async def test_reader_fails_closed_without_source_hypothesis() -> None:
    reader = _reader()
    reader._outreach.source_hypothesis_id = None

    with pytest.raises(ValidationError, match="来源假设"):
        await reader.load(TENANT, CONTEXT)


async def test_reader_rejects_context_that_disagrees_with_enrollment() -> None:
    other = ReplyActionContext(
        message_id=CONTEXT.message_id,
        outbound_message_id=CONTEXT.outbound_message_id,
        enrollment_id=CONTEXT.enrollment_id,
        account_id=ProspectAccountId(new_id("acc")),
        contact_point_id=CONTEXT.contact_point_id,
    )

    with pytest.raises(ValidationError, match="Enrollment 关联不匹配"):
        await _reader().load(TENANT, other)


async def test_reader_rejects_outbound_message_not_bound_to_exact_enrollment() -> None:
    reader = _reader()
    reader._outreach.mismatched_delivery = True

    with pytest.raises(ValidationError, match="出站消息关联不匹配"):
        await reader.load(TENANT, CONTEXT)
