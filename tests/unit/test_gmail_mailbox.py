"""账号镜像覆盖全邮箱，不借用外发关联或 30 天入站过滤。"""

import base64

import pytest

from connectors.gmail.mailbox import GmailMailboxReader, parse_message
from shared.schemas.mailbox import MailboxFailure


def message(mid="a1", labels=None):
    return {
        "id": mid,
        "threadId": "b1",
        "internalDate": "1700000000000",
        "labelIds": labels or ["SENT"],
        "snippet": "preview",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "询价"},
                {"name": "From", "value": "owner@example.com"},
            ],
            "body": {"data": base64.urlsafe_b64encode("你好".encode()).decode()},
        },
    }


class Provider:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def get(self, path, params):
        self.calls.append((path, params))
        if path == "profile":
            return {"emailAddress": "owner@example.com", "historyId": "10"}
        if path == "messages":
            if params.get("pageToken") == "second":
                return {"messages": [{"id": "a2"}]}
            return {"messages": [{"id": "a1"}], "nextPageToken": "second"}
        if path == "history":
            if self.fail:
                raise MailboxFailure("history_expired")
            return {
                "historyId": "12",
                "history": [{"messagesAdded": [{"message": {"id": "a3"}}]}],
            }
        return message(path.split("/")[-1])


def test_body_plain_text_and_attachment_metadata():
    raw = message()
    parsed = parse_message(raw)
    assert parsed.body_text == "你好"
    assert parsed.subject == "询价"
    assert parsed.labels == ["SENT"]
    raw["payload"] = {
        "mimeType": "text/html",
        "body": {
            "data": base64.urlsafe_b64encode(
                b'<script>steal()</script><p>Hello<img src="https://tracker.example/pixel"></p>'
            ).decode()
        },
    }
    assert parse_message(raw).body_text == "Hello"


@pytest.mark.asyncio
async def test_full_mailbox_then_catch_up_no_date_or_inbox_filter():
    provider = Provider()
    reader = GmailMailboxReader(provider, "owner@example.com")
    first = await reader.fetch(None)
    assert [m.message_id for m in first.messages] == ["a1"]
    assert first.phase == "backfill"
    second = await reader.fetch(first.cursor)
    assert second.phase == "catch_up"
    third = await reader.fetch(second.cursor)
    assert third.phase == "synced"
    assert [m.message_id for m in third.messages] == ["a3"]
    for path, params in provider.calls:
        if path == "messages":
            assert params["includeSpamTrash"] == "true"
            assert "q" not in params and "labelIds" not in params
        if path == "history":
            assert params["startHistoryId"] == "10"


@pytest.mark.asyncio
async def test_wrong_google_account_rejected_before_reading_mail():
    provider = Provider()
    with pytest.raises(MailboxFailure, match="account_mismatch"):
        await GmailMailboxReader(provider, "other@example.com").fetch(None)
    assert [p for p, _ in provider.calls] == ["profile"]


@pytest.mark.asyncio
async def test_expired_history_restarts_full_sync_without_claiming_complete():
    provider = Provider()
    reader = GmailMailboxReader(provider, "owner@example.com")
    first = await reader.fetch(None)
    second = await reader.fetch(first.cursor)
    provider.fail = True
    reset = await reader.fetch(second.cursor)
    assert reset.phase == "backfill" and reset.reset
    assert not reset.full_scan_complete


@pytest.mark.asyncio
async def test_history_pagination_does_not_claim_caught_up_early():
    class Paged(Provider):
        async def get(self, path, params):
            if path == "history" and "pageToken" not in params:
                return {"historyId": "15", "nextPageToken": "h2", "history": []}
            if path == "history":
                assert params["pageToken"] == "h2"
                assert params["startHistoryId"] == "10"
                return {"historyId": "16", "history": []}
            return await super().get(path, params)

    reader = GmailMailboxReader(Paged(), "owner@example.com")
    first = await reader.fetch(None)
    second = await reader.fetch(first.cursor)
    third = await reader.fetch(second.cursor)
    assert third.phase == "catch_up"
    fourth = await reader.fetch(third.cursor)
    assert fourth.phase == "synced"


@pytest.mark.asyncio
async def test_failure_halfway_through_page_delivers_no_checkpoint():
    class Fails(Provider):
        async def get(self, path, params):
            if path == "messages":
                return {"messages": [{"id": "a1"}, {"id": "a2"}]}
            if path == "messages/a2":
                raise MailboxFailure("rate_limited", 120)
            return await super().get(path, params)

    with pytest.raises(MailboxFailure, match="rate_limited"):
        await GmailMailboxReader(Fails(), "owner@example.com").fetch(None)


@pytest.mark.asyncio
async def test_requested_latest_page_preserves_historical_checkpoint():
    provider = Provider()
    first = await GmailMailboxReader(provider, "owner@example.com").fetch(None)
    provider.calls.clear()
    latest = await GmailMailboxReader(provider, "owner@example.com", refresh_latest=True).fetch(first.cursor)
    assert [m.message_id for m in latest.messages] == ["a1"]
    assert latest.cursor == first.cursor
    assert latest.phase == "backfill"
    assert latest.refresh_complete and not latest.full_scan_complete
    assert ("messages", {"maxResults": "25", "includeSpamTrash": "true"}) in provider.calls
    resumed = await GmailMailboxReader(provider, "owner@example.com").fetch(latest.cursor)
    assert [m.message_id for m in resumed.messages] == ["a2"]
    assert resumed.phase == "catch_up"


@pytest.mark.asyncio
async def test_requested_latest_does_not_reset_incremental_sync():
    provider = Provider()
    reader = GmailMailboxReader(provider, "owner@example.com")
    first = await reader.fetch(None)
    second = await reader.fetch(first.cursor)
    result = await GmailMailboxReader(provider, "owner@example.com", refresh_latest=True).fetch(second.cursor)
    assert result.phase == "synced"
    assert [m.message_id for m in result.messages] == ["a3"]
