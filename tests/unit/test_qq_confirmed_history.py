from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from types import SimpleNamespace

import pytest

from connectors.qq_mail import confirmed_history as history
from shared.schemas.email_inbound import InboundError

GRANT = SimpleNamespace(sender="1249583590@qq.com", recipient="986647123@qq.com")
ORIGINAL = "Original TradeOS private test. " * 6
CONFIRMED = history.ConfirmedHistory("test-Q01", ORIGINAL, datetime(2026, 10, 15, 8, 14, tzinfo=UTC),
                                    ("希望添加WhatsApp", "我想购买太阳能风扇，请告诉我怎么买"))


def mime(index=0, **updates):
    message = EmailMessage(policy=SMTP)
    values = {"From": GRANT.recipient, "To": GRANT.sender, "Subject": "Re: test-Q01",
              "Message-ID": f"<reply{index}@qq.com>", "In-Reply-To": "<rewritten@qq.com>",
              "Date": f"Thu, 15 Oct 2026 16:{15 + index}:00 +0800"}
    values.update(updates)
    for key, value in values.items():
        message[key] = value
    message.set_content(CONFIRMED.reply_phrases[index] + "\n\n" + ORIGINAL)
    return message.as_bytes()


def test_exact_confirmed_pair_preserves_raw_rewritten_header_and_dates():
    rows = [(mime(i), history.checked_headers(mime(i), GRANT, CONFIRMED)) for i in range(2)]
    result = history.checked_bodies(rows, GRANT, CONFIRMED)
    assert len(result) == 2
    assert result[0].original_reply_to == "<rewritten@qq.com>"
    assert result[0].raw == mime(0)
    assert result[0].sent_at == datetime(2026, 10, 15, 8, 15, tzinfo=UTC)


@pytest.mark.parametrize("changes", [
    {"From": "stranger@qq.com"}, {"To": "someone@qq.com"}, {"Cc": "cc@qq.com"},
    {"Subject": "Re: unrelated"}, {"Date": "Thu, 15 Oct 2026 17:00:00 +0800"},
    {"In-Reply-To": "<other@invalid.test>"},
])
def test_rejects_unconfirmed_header_scope(changes):
    assert history.checked_headers(mime(**changes), GRANT, CONFIRMED) is None


def test_rejects_wrong_quoted_original_even_with_matching_subject():
    raw = mime().replace(b"Original TradeOS", b"Unrelated text")
    with pytest.raises(InboundError):
        history.checked_bodies([(raw, history.checked_headers(raw, GRANT, CONFIRMED))], GRANT, CONFIRMED)


def test_rejects_duplicate_or_missing_confirmation():
    raw = mime()
    with pytest.raises(InboundError):
        history.checked_bodies([(raw, history.checked_headers(raw, GRANT, CONFIRMED))] * 2, GRANT, CONFIRMED)


def test_single_visually_confirmed_reply_with_explicit_one_hour_window():
    confirmed = replace(CONFIRMED, reply_phrases=(CONFIRMED.reply_phrases[0],), reply_window=timedelta(hours=1))
    raw = mime(Date="Thu, 15 Oct 2026 17:00:00 +0800")
    result = history.checked_bodies([(raw, history.checked_headers(raw, GRANT, confirmed))], GRANT, confirmed)
    assert len(result) == 1 and result[0].raw == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "count", "uidvalidity", "permission"])
async def test_bounded_header_filter_precedes_body_fetch(monkeypatch, invalid):
    bodies = []
    class Session:
        def __init__(self, connection):
            self.snapshots = 0
        async def snapshot(self):
            self.snapshots += 1
            return (2 if invalid == "uidvalidity" and self.snapshots > 1 else 1), 10
        async def search_day(self, sent_at):
            return (1, 2, 3) if invalid != "count" else (1, 3)
        async def recovery_header(self, uid):
            raw = mime(uid - 1) if uid < 3 else mime(Subject="unrelated")
            return len(raw), raw.split(b"\r\n\r\n")[0] + b"\r\n\r\n"
        async def body(self, uid, size):
            bodies.append(uid)
            return mime(uid - 1)
    class Transport:
        @asynccontextmanager
        async def session(self):
            yield SimpleNamespace(connection=None)
    count = 0
    async def authorize():
        nonlocal count
        count += 1
        return None if invalid == "permission" and count > 4 else GRANT
    monkeypatch.setattr(history, "_HistorySession", Session)
    if invalid:
        with pytest.raises(InboundError):
            await history.read_confirmed_history(Transport(), GRANT, CONFIRMED, authorize)
        assert bodies == []
    else:
        result = await history.read_confirmed_history(Transport(), GRANT, CONFIRMED, authorize)
        assert len(result) == 2 and bodies == [1, 2]
