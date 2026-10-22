"""真实本任务Postgres持久停止；只用固定邮件，不接触真实Provider。"""

import pytest
from sqlalchemy import select

from infra.db.qq_trial_tables import QQTrialEventRow
from tests.integration.test_qq_trial_ledger import snapshot
from tests.integration.test_qq_trial_ledger import (
    trial as trial,  # noqa: PLC0414 - pytest fixture re-export
)
from tests.integration.test_qq_trial_pipeline import setup_pipeline
from tests.unit.test_qq_trial_wire import ALIAS, alternative


async def test_alternative_stops_durably_without_body_archive_or_cursor_commit(trial):
    gate, sessions, *_ = trial
    driver, store, qq, blobs, *_ = await setup_pipeline(trial)
    original = await store.read_cursor()
    qq.messages[11] = alternative().replace(ALIAS.encode(), gate.scope.provider_header.encode())
    assert await driver.scan_once() == 0
    row = await snapshot(gate, sessions)
    assert row.state == "stopped" and row.reason == "mime_structure_unverified"
    assert row.bodies == 0 and row.message_id is None
    assert qq.downloads == [] and not blobs.values
    assert (await store.read_cursor()).cursor == original.cursor
    assert await driver.scan_once() == 0
    async with sessions() as session:
        reasons = (await session.scalars(select(QQTrialEventRow.kind).where(
            QQTrialEventRow.tenant_id == gate.scope.route.tenant_id,
            QQTrialEventRow.trial_id == gate.scope.trial_id,
        ))).all()
    assert "mime_structure_unverified" in reasons
    assert "attachments_not_allowed" not in reasons


@pytest.mark.parametrize("reason", [
    "mime_headers_invalid", "bcc_not_allowed", "attachments_not_allowed",
    "mime_structure_unverified", "mime_type_not_supported", "cancelled",
    "SYNTHETIC-SECRET\r\nFORGED-LOG-LINE",
])
async def test_stop_reason_uses_fixed_allowlist_and_never_overwrites_first_stop(trial, reason):
    gate, sessions, *_ = trial
    await gate.stop(reason)
    expected = "failed_or_unknown" if reason.startswith("SYNTHETIC") else reason
    assert (await snapshot(gate, sessions)).reason == expected
    await gate.stop("cancelled")
    assert (await snapshot(gate, sessions)).reason == expected
