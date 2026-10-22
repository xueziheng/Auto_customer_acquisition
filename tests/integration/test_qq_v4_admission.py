"""原 Gateway/UoW 的隔离 PG 验证，不触及真实邮件或生产游标。"""

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from infra.db.qq_trial_tables import QQTrialEventRow
from infra.db.tables import EmailInboundReceiptRow, MessageRow
from tests.integration.test_qq_trial_ledger import snapshot
from tests.integration.test_qq_trial_ledger import (
    trial as trial,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.test_qq_trial_pipeline import setup_pipeline
from tests.unit.qq_mime_fixture import structure_for
from tests.unit.test_qq_q02_compatibility import malformed_alternative
from tests.unit.test_qq_trial_wire import ALIAS, alternative


async def prepare(trial, *, unsafe=False, change=None):
    gate, sessions, *_ = trial
    driver, store, qq, blobs, *_ = await setup_pipeline(trial)
    raw = malformed_alternative() if unsafe else alternative()
    raw = raw.replace(ALIAS.encode(), gate.scope.provider_header.encode())
    qq.messages[11] = raw
    plan = structure_for(raw)
    if change == "attachment":
        plan[0][9] = [b"ATTACHMENT", None]
    elif change == "integrity":
        plan[0][6] += 1
    elif change == "limit":
        plan[0][6] = 262145
    original_headers = qq.headers

    async def headers(uid):
        value = await original_headers(uid)
        return replace(value, bodystructure=plan)

    qq.headers = headers
    return gate, sessions, driver, store, qq, blobs


async def test_alternative_commits_once_with_original_bytes_receipt_and_no_extra_budget(trial):
    gate, sessions, driver, store, qq, blobs = await prepare(trial)
    assert await driver.scan_once() == 1
    after = await store.read_cursor()
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "received" and row.bodies == 1 and row.headers == 1
    assert qq.downloads == [11] and list(blobs.values.values()) == [qq.messages[11]]
    assert (await store.read_cursor()).cursor == after.cursor
    async with sessions() as s:
        for model in (MessageRow, EmailInboundReceiptRow):
            query = select(func.count()).select_from(model).where(model.tenant_id == gate.scope.route.tenant_id)
            if model is MessageRow:
                query = query.where(MessageRow.direction == "inbound")
            assert await s.scalar(query) == 1


@pytest.mark.parametrize("change,reason,bodies", [
    ("unsafe", "reply_headers_unsafe", 0),
    ("attachment", "attachments_not_allowed", 0),
    ("limit", "mime_structure_invalid", 0),
    ("integrity", "mime_body_mismatch", 1),
])
async def test_failure_keeps_cursor_and_persists_stop_without_archive_or_retry(trial, change, reason, bodies):
    gate, sessions, driver, store, qq, blobs = await prepare(trial, unsafe=change == "unsafe", change=change)
    before = await store.read_cursor()
    watermark = (await snapshot(gate, sessions)).watermark
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "stopped" and row.reason == reason and row.bodies == bodies
    assert row.watermark == watermark and row.message_id is None and not blobs.values
    assert (await store.read_cursor()).cursor == before.cursor
    opens = qq.opens
    assert await driver.scan_once() == 0 and qq.opens == opens
    assert (await snapshot(gate, sessions)).bodies == bodies
    async with sessions() as s:
        events = (await s.scalars(select(QQTrialEventRow.kind).where(
            QQTrialEventRow.tenant_id == gate.scope.route.tenant_id,
            QQTrialEventRow.trial_id == gate.scope.trial_id,
        ))).all()
        assert events.count(reason) == 1
        assert await s.scalar(select(func.count()).select_from(EmailInboundReceiptRow).where(
            EmailInboundReceiptRow.tenant_id == gate.scope.route.tenant_id)) == 0
