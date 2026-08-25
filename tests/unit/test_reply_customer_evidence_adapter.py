"""客户回复证据必须由耐久 Conversation/Outreach 精确链验证。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from domains.conversations.schemas import ReplyCategory
from domains.outreach.schemas import DeliveryFeedbackTarget
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EnrollmentId,
    MessageAttemptId,
    MessageId,
    NeedHypothesisId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
OTHER_TENANT = TenantId(new_id("tn"))
ACCOUNT = ProspectAccountId(new_id("acc"))
CONTACT = ContactPointId(new_id("cp"))
ENROLLMENT = EnrollmentId(new_id("enr"))
HYPOTHESIS = NeedHypothesisId(new_id("hyp"))
MESSAGE = MessageId(new_id("msg"))
OUTBOUND = OutboundMessageId(
    f"<reply-evidence.{'c' * 64}@messages.tradeos.invalid>"
)
IDENTITY = SendingIdentityId(new_id("sid"))


class _Repository:
    def __init__(self, value: object | None) -> None:
        self.value = value

    async def get(self, tenant_id, resource_id):
        del tenant_id, resource_id
        return self.value


class _Uow:
    def __init__(self, state: "_State") -> None:
        self.messages = _Repository(state.message)
        self.classifications = _Repository(state.classification)
        self.conversations = _Repository(state.conversation)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb


class _State:
    def __init__(self) -> None:
        self.message = SimpleNamespace(
            tenant_id=TENANT,
            message_id=MESSAGE,
            conversation_id="conv_reply_evidence",
            direction=SimpleNamespace(value="inbound"),
            outbound_message_id=OUTBOUND,
        )
        self.classification = SimpleNamespace(
            tenant_id=TENANT,
            message_id=MESSAGE,
            category=ReplyCategory.PROVIDES_SPECIFICATION,
            classified_by="reply-model-v4",
            classified_at=NOW,
        )
        self.conversation = SimpleNamespace(
            tenant_id=TENANT,
            conversation_id="conv_reply_evidence",
            account_id=ACCOUNT,
        )


class _Outreach:
    def __init__(self) -> None:
        self.source_hypothesis_id = HYPOTHESIS
        self.account_id = ACCOUNT

    async def get_enrollment(self, tenant_id, enrollment_id, *, actor):
        del actor
        return SimpleNamespace(
            tenant_id=tenant_id,
            enrollment_id=enrollment_id,
            account_id=self.account_id,
            contact_point_id=CONTACT,
            sending_identity_id=IDENTITY,
            source_hypothesis_id=self.source_hypothesis_id,
        )

    async def resolve_delivery_feedback(self, tenant_id, lookup, *, actor):
        del lookup, actor
        return DeliveryFeedbackTarget(
            tenant_id=tenant_id,
            attempt_id=MessageAttemptId(new_id("mat")),
            enrollment_id=ENROLLMENT,
            account_id=self.account_id,
            contact_point_id=CONTACT,
            sending_identity_id=IDENTITY,
        )


def _claim():
    schemas = importlib.import_module("domains.demand.schemas")
    return schemas.CustomerReplyEvidenceClaim(
        hypothesis_id=HYPOTHESIS,
        source_message_id=MESSAGE,
        outbound_message_id=OUTBOUND,
        enrollment_id=ENROLLMENT,
        account_id=ACCOUNT,
        contact_point_id=CONTACT,
    )


def _verifier(state: _State, outreach: _Outreach):
    try:
        module = importlib.import_module(
            "apps.scheduler_worker.adapters.reply_customer_evidence"
        )
    except ModuleNotFoundError:
        pytest.fail("生产客户回复证据 verifier 尚未实现")
    return module.TenantBoundCustomerReplyEvidenceVerifier(
        tenant_id=TENANT,
        conversations_uow_factory=lambda tenant: _Uow(state),
        outreach=outreach,
    )


async def test_verifier_returns_exact_durable_customer_evidence() -> None:
    proof = await _verifier(_State(), _Outreach()).verify(TENANT, _claim())

    assert proof.tenant_id == TENANT
    assert proof.hypothesis_id == HYPOTHESIS
    assert proof.source_message_id == MESSAGE
    assert proof.account_id == ACCOUNT
    assert proof.evidence_level.value == "customer_specification"
    assert proof.classified_by == "reply-model-v4"
    assert proof.classified_at == NOW


@pytest.mark.parametrize(
    "corruption, expected",
    [
        ("missing", "消息不存在"),
        ("outbound", "非入站"),
        ("wrong_outbound", "出站关联不匹配"),
        ("unclassified", "分类不存在"),
        ("wrong_classification_message", "分类关联不匹配"),
        ("wrong_account", "企业关联不匹配"),
        ("wrong_hypothesis", "来源假设不匹配"),
    ],
)
async def test_verifier_fails_closed_for_untrusted_customer_evidence(
    corruption: str,
    expected: str,
) -> None:
    state = _State()
    outreach = _Outreach()
    if corruption == "missing":
        state.message = None
    elif corruption == "outbound":
        state.message.direction = SimpleNamespace(value="outbound")
    elif corruption == "wrong_outbound":
        state.message.outbound_message_id = OutboundMessageId(
            f"<wrong-evidence.{'f' * 64}@messages.tradeos.invalid>"
        )
    elif corruption == "unclassified":
        state.classification = None
    elif corruption == "wrong_classification_message":
        state.classification.message_id = MessageId(new_id("msg"))
    elif corruption == "wrong_account":
        state.conversation.account_id = ProspectAccountId(new_id("acc"))
    else:
        outreach.source_hypothesis_id = NeedHypothesisId(new_id("hyp"))

    with pytest.raises(ValidationError, match=expected):
        await _verifier(state, outreach).verify(TENANT, _claim())


async def test_verifier_rejects_cross_tenant_before_reading() -> None:
    with pytest.raises(TenantIsolationViolation):
        await _verifier(_State(), _Outreach()).verify(OTHER_TENANT, _claim())
