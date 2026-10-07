"""Smart Inbox 查询投影：原分类、人工纠正与原件引用必须同时可审计。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest

from domains.conversations.schemas import ReplyCategory
from domains.conversations.service import InboxActor, InboxEmployeeFacts, InboxScope
from domains.conversations.service_impl import ConversationServiceImpl
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
CONVERSATION = ConversationId(new_id("con"))
ACCOUNT = ProspectAccountId(new_id("acc"))
MESSAGE = MessageId(new_id("msg"))
ACTOR = InboxActor(TENANT, "emp_test", "boss", InboxScope.TENANT)

_models = importlib.import_module("domains.conversations.models")
ClassificationCorrection = _models.ClassificationCorrection
Conversation = _models.Conversation
Message = _models.Message
MessageClassification = _models.MessageClassification
MessageDirection = _models.MessageDirection


class _Conversations:
    def __init__(self, conversation: Conversation | None) -> None:
        self.conversation = conversation
        self.list_calls: list[tuple[TenantId, int]] = []

    async def list_recent(
        self, tenant_id: TenantId, *, actor, limit: int
    ) -> list[Conversation]:
        self.list_calls.append((tenant_id, limit))
        return [self.conversation] if self.conversation is not None else []

    async def get_inbox(
        self, tenant_id: TenantId, conversation_id: ConversationId, *, actor
    ) -> Conversation | None:
        if tenant_id == TENANT and conversation_id == CONVERSATION:
            return self.conversation
        return None


class _Messages:
    def __init__(self, messages: list[Message]) -> None:
        self.messages = messages

    async def list_for_conversation(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> list[Message]:
        assert tenant_id == TENANT
        assert conversation_id == CONVERSATION
        return list(self.messages)


class _Classifications:
    def __init__(
        self,
        classification: MessageClassification,
        corrections: list[ClassificationCorrection],
    ) -> None:
        self.classification = classification
        self.corrections = corrections

    async def get(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> MessageClassification | None:
        assert tenant_id == TENANT
        return self.classification if message_id == MESSAGE else None

    async def list_corrections(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> list[ClassificationCorrection]:
        assert tenant_id == TENANT
        return list(self.corrections) if message_id == MESSAGE else []


class _Uow:
    def __init__(
        self,
        conversation: Conversation | None,
        messages: list[Message],
        classification: MessageClassification,
        corrections: list[ClassificationCorrection],
    ) -> None:
        self.inbox_facts = self
        self.conversations = _Conversations(conversation)
        self.messages = _Messages(messages)
        self.classifications = _Classifications(classification, corrections)

    async def read_employee(self, tenant_id, employee_id):
        return InboxEmployeeFacts(ACTOR.employee_id, "boss", True, None)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args


def _fixture() -> tuple[ConversationServiceImpl, _Uow]:
    conversation = Conversation(
        conversation_id=CONVERSATION,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        channel="email",
        created_at=NOW - timedelta(days=3),
        last_inbound_at=NOW,
        last_outbound_at=NOW - timedelta(days=1),
    )
    messages = [
        Message(
            message_id=MessageId(new_id("msg")),
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            direction=MessageDirection.OUTBOUND,
            sent_at=NOW - timedelta(days=1),
            raw_artifact_ref="artifact:outbound-1",
        ),
        Message(
            message_id=MESSAGE,
            tenant_id=TENANT,
            conversation_id=CONVERSATION,
            direction=MessageDirection.INBOUND,
            sent_at=NOW,
            raw_artifact_ref="artifact:inbound-1",
        ),
    ]
    classification = MessageClassification(
        tenant_id=TENANT,
        message_id=MESSAGE,
        category=ReplyCategory.REJECTION,
        classified_by="reply-classifier:v3",
        classified_at=NOW + timedelta(minutes=1),
    )
    corrections = [
        ClassificationCorrection(
            correction_id=new_id("ccr"),
            tenant_id=TENANT,
            message_id=MESSAGE,
            corrected_category=ReplyCategory.REQUESTS_QUOTE,
            corrected_by="emp_reviewer",
            corrected_at=NOW + timedelta(minutes=2),
        )
    ]
    uow = _Uow(conversation, messages, classification, corrections)
    return ConversationServiceImpl(
        lambda _: uow,  # type: ignore[return-value]
        now=lambda: NOW,
    ), uow


@pytest.mark.asyncio
async def test_list_inbox_keeps_model_judgement_and_effective_correction() -> None:
    service, uow = _fixture()

    items = await service.list_inbox(
        TENANT, actor=ACTOR, category=ReplyCategory.REQUESTS_QUOTE, limit=20
    )

    assert uow.conversations.list_calls == [(TENANT, 200)]
    assert len(items) == 1
    item = items[0]
    assert item.conversation_id == CONVERSATION
    assert item.latest_message_id == MESSAGE
    assert item.original_category is ReplyCategory.REJECTION
    assert item.effective_category is ReplyCategory.REQUESTS_QUOTE
    assert item.classified_by == "reply-classifier:v3"
    assert item.raw_artifact_ref == "artifact:inbound-1"
    assert item.required_actions == ("stop_sequence", "handoff")


@pytest.mark.asyncio
async def test_inbox_detail_exposes_evidence_without_copying_message_body() -> None:
    service, _uow = _fixture()

    detail = await service.get_inbox_detail(TENANT, CONVERSATION, actor=ACTOR)

    assert detail.account_id == ACCOUNT
    assert len(detail.messages) == 2
    inbound = detail.messages[1]
    assert inbound.original_category is ReplyCategory.REJECTION
    assert inbound.effective_category is ReplyCategory.REQUESTS_QUOTE
    assert inbound.corrections[0].corrected_by == "emp_reviewer"
    assert inbound.raw_artifact_ref == "artifact:inbound-1"
    assert not hasattr(inbound, "body")
    assert not hasattr(inbound, "subject")


@pytest.mark.asyncio
async def test_inbox_detail_fails_closed_for_unknown_conversation() -> None:
    service, _uow = _fixture()

    with pytest.raises(PermissionDenied, match="收件箱访问拒绝"):
        await service.get_inbox_detail(
            TENANT, ConversationId(new_id("con")), actor=ACTOR
        )


async def test_missing_actor_cannot_read_tenant_inbox():
    """移除显式actor门禁会让此拒绝用例泄漏真实域投影。"""
    from shared.errors import PermissionDenied

    service, _ = _fixture()
    with pytest.raises((PermissionDenied, TypeError)):
        await service.list_inbox(TENANT, category=None, limit=20)
