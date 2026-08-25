"""Smart Inbox API：角色门、租户绑定与纠正审计入口。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from domains.conversations.schemas import (
    ConversationInboxDetail,
    ConversationInboxItem,
    InboxMessageView,
    ReplyCategory,
)
from shared.schemas.identifiers import (
    ConversationId,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)
from tests.unit.test_api_app import _ApiClient, _app, _employee

NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
TENANT = TenantId("tenant-a")
CONVERSATION = ConversationId(new_id("con"))
MESSAGE = MessageId(new_id("msg"))
OUTBOUND = OutboundMessageId("<tradeos.outbound@example.test>")
ACCOUNT = ProspectAccountId(new_id("acc"))


class _Conversations:
    def __init__(self) -> None:
        self.list_calls: list[tuple[TenantId, ReplyCategory | None, int]] = []
        self.detail_calls: list[tuple[TenantId, ConversationId]] = []
        self.correct_calls: list[tuple[TenantId, MessageId, ReplyCategory, str]] = []

    async def list_inbox(
        self,
        tenant_id: TenantId,
        *,
        category: ReplyCategory | None,
        limit: int,
    ) -> list[ConversationInboxItem]:
        self.list_calls.append((tenant_id, category, limit))
        return [
            ConversationInboxItem(
                conversation_id=CONVERSATION,
                account_id=ACCOUNT,
                channel="email",
                last_activity_at=NOW,
                latest_message_id=MESSAGE,
                latest_message_at=NOW,
                raw_artifact_ref="artifact:reply-1",
                original_category=ReplyCategory.REJECTION,
                effective_category=ReplyCategory.REQUESTS_QUOTE,
                classified_by="reply-classifier:v3",
                classified_at=NOW,
                correction_count=1,
                required_actions=("stop_sequence", "handoff"),
            )
        ]

    async def get_inbox_detail(
        self, tenant_id: TenantId, conversation_id: ConversationId
    ) -> ConversationInboxDetail:
        self.detail_calls.append((tenant_id, conversation_id))
        return ConversationInboxDetail(
            conversation_id=CONVERSATION,
            account_id=ACCOUNT,
            channel="email",
            created_at=NOW,
            last_inbound_at=NOW,
            last_outbound_at=None,
            messages=(
                InboxMessageView(
                    message_id=MESSAGE,
                    outbound_message_id=OUTBOUND,
                    direction="inbound",
                    sent_at=NOW,
                    raw_artifact_ref="artifact:reply-1",
                    original_category=ReplyCategory.REJECTION,
                    effective_category=ReplyCategory.REQUESTS_QUOTE,
                    classified_by="reply-classifier:v3",
                    classified_at=NOW,
                    corrections=(),
                    required_actions=("stop_sequence", "handoff"),
                ),
            ),
        )

    async def correct_classification(
        self,
        tenant_id: TenantId,
        message_id: MessageId,
        corrected_category: ReplyCategory,
        corrected_by: str,
    ) -> None:
        self.correct_calls.append(
            (tenant_id, message_id, corrected_category, corrected_by)
        )


def _inbox_app(*, role: str = "boss") -> tuple[Any, _Conversations]:
    app, _service, _scope, dependencies, _markers = _app(
        _employee(employee_id="emp-inbox", role=role)
    )
    conversations = _Conversations()
    app.state.dependencies = replace(dependencies, conversations=conversations)
    return app, conversations


def _headers() -> dict[str, str]:
    return {"X-Tenant-Id": str(TENANT), "X-Employee-Id": "emp-inbox"}


def _post(app: Any, path: str, body: dict[str, object]) -> Response:
    async def run() -> Response:
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(path, headers=_headers(), json=body)

    return asyncio.run(run())


def test_inbox_list_filters_by_effective_category_and_tenant() -> None:
    app, conversations = _inbox_app()

    response = _ApiClient(app).get(
        "/inbox/conversations?category=requests_quote&limit=20",
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()[0]["classified_by"] == "reply-classifier:v3"
    assert response.json()[0]["raw_artifact_ref"] == "artifact:reply-1"
    assert conversations.list_calls == [
        (TENANT, ReplyCategory.REQUESTS_QUOTE, 20)
    ]


def test_inbox_detail_exposes_only_safe_outbound_correlation_id() -> None:
    app, _conversations = _inbox_app()

    response = _ApiClient(app).get(
        f"/inbox/conversations/{CONVERSATION}", headers=_headers()
    )

    assert response.status_code == 200
    message = response.json()["messages"][0]
    assert message["message_id"] == MESSAGE
    assert message["outbound_message_id"] == OUTBOUND
    assert set(message).isdisjoint({"body", "subject", "from_address", "to_address"})


def test_inbox_detail_never_returns_copied_subject_or_body() -> None:
    app, conversations = _inbox_app()

    response = _ApiClient(app).get(
        f"/inbox/conversations/{CONVERSATION}", headers=_headers()
    )

    assert response.status_code == 200
    message = response.json()["messages"][0]
    assert message["raw_artifact_ref"] == "artifact:reply-1"
    assert "body" not in message
    assert "subject" not in message
    assert conversations.detail_calls == [(TENANT, CONVERSATION)]


def test_inbox_correction_uses_authenticated_employee() -> None:
    app, conversations = _inbox_app()

    response = _post(
        app,
        f"/inbox/messages/{MESSAGE}/correct-classification",
        {"category": "unsubscribe"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "message_id": str(MESSAGE),
        "category": "unsubscribe",
        "corrected_by": "emp-inbox",
    }
    assert conversations.correct_calls == [
        (TENANT, MESSAGE, ReplyCategory.UNSUBSCRIBE, "emp-inbox")
    ]


def test_viewer_cannot_read_smart_inbox() -> None:
    app, conversations = _inbox_app(role="viewer")

    response = _ApiClient(app, raise_server_exceptions=False).get(
        "/inbox/conversations", headers=_headers()
    )

    assert response.status_code == 403
    assert conversations.list_calls == []
