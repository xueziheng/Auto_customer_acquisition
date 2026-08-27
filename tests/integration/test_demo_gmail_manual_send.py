"""Gmail 手工发送离线演示的进程级、真实 PostgreSQL 验收。"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    SendReservationRow,
    ToolCallEventRow,
    ToolCallRow,
)
from shared.schemas.identifiers import TenantId

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUMMARY_KEYS = {
    "ambiguous_status",
    "attempt_ids",
    "duplicate_status",
    "first_status",
    "gmail_search_count",
    "gmail_send_count",
    "reservation_count",
    "tenant_id",
    "tool_call_ids",
}
_RAW_MARKERS = (
    "demo-recipient-marker@example.test",
    "demo-sender-marker@example.test",
    "demo-subject-marker",
    "demo-body-marker",
    "demo-oauth-value-marker",
    "https://unsubscribe.example.test/",
)


def _run_demo(database_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/demo_gmail_manual_send.py"],
        cwd=_REPO_ROOT,
        env={
            "PYTHONPATH": str(_REPO_ROOT),
            "DATABASE_URL": database_url,
            "TRADEOS_GMAIL_DEMO_MODE": "controlled",
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _safe_failure(result: subprocess.CompletedProcess[str]) -> str:
    if "demo_gmail_manual_send.py" in result.stderr and "No such file" in result.stderr:
        return "Gmail 手工发送演示脚本缺失（file missing）"
    return "Gmail 手工发送演示子进程失败（输出已脱敏）"


def _successful_summary(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, _safe_failure(result)
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    summary = json.loads(result.stdout)
    assert isinstance(summary, dict) and set(summary) == _SUMMARY_KEYS
    assert isinstance(summary["tenant_id"], str)
    assert summary["first_status"] == "succeeded"
    assert summary["duplicate_status"] == "duplicate"
    assert summary["ambiguous_status"] == "reconciliation_required"
    assert summary["gmail_send_count"] == 1
    assert summary["gmail_search_count"] == 2
    assert summary["reservation_count"] == 2
    attempt_ids = summary["attempt_ids"]
    tool_call_ids = summary["tool_call_ids"]
    assert isinstance(attempt_ids, list) and len(attempt_ids) == 2
    assert isinstance(tool_call_ids, list) and len(tool_call_ids) == 2
    assert len(set(attempt_ids)) == 2
    assert len(set(tool_call_ids)) == 2
    rendered = result.stdout + result.stderr
    assert not any(marker in rendered for marker in _RAW_MARKERS)
    return summary


async def _readback(
    database_url: str, summary: dict[str, object]
) -> dict[str, object]:
    tenant = TenantId(summary["tenant_id"])
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            attempts = (
                await session.execute(
                    select(OutreachMessageAttemptRow)
                    .where(OutreachMessageAttemptRow.tenant_id == tenant)
                    .order_by(OutreachMessageAttemptRow.attempt_id)
                )
            ).scalars().all()
            enrollments = (
                await session.execute(
                    select(OutreachEnrollmentRow).where(
                        OutreachEnrollmentRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            calls = (
                await session.execute(
                    select(ToolCallRow)
                    .where(ToolCallRow.tenant_id == tenant)
                    .order_by(ToolCallRow.created_at, ToolCallRow.tool_call_id)
                )
            ).scalars().all()
            events = (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            reservations = (
                await session.execute(
                    select(SendReservationRow).where(
                        SendReservationRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    expected_attempt_ids = set(summary["attempt_ids"])
    expected_tool_call_ids = set(summary["tool_call_ids"])
    assert {row.attempt_id for row in attempts} == expected_attempt_ids
    assert Counter(row.state for row in attempts) == Counter(
        {"sent": 1, "sending": 1}
    )
    sent = next(row for row in attempts if row.state == "sent")
    ambiguous = next(row for row in attempts if row.state == "sending")
    assert sent.provider_ref == "gmail_demo_ref_1"
    assert ambiguous.provider_ref is None
    assert Counter(row.state for row in enrollments) == Counter(
        {"completed": 1, "enrolled": 1}
    )

    canonical = [row for row in calls if row.idempotency_key is not None]
    resolutions = [row for row in calls if row.idempotency_key is None]
    assert {row.tool_call_id for row in canonical} == expected_tool_call_ids
    assert Counter(row.status for row in canonical) == Counter(
        {"succeeded": 1, "failed_transient": 1}
    )
    succeeded = next(row for row in canonical if row.status == "succeeded")
    reconciling = next(row for row in canonical if row.status == "failed_transient")
    assert succeeded.provider_ref == "gmail_demo_ref_1"
    assert succeeded.error_category is None
    assert reconciling.provider_ref is None
    assert reconciling.error_category == "reconciliation_required"
    assert Counter(row.status for row in resolutions) == Counter({"duplicate": 1})
    assert resolutions[0].duplicate_of == succeeded.tool_call_id

    assert len(reservations) == 2
    assert {row.identity_id for row in reservations} == {
        attempts[0].sending_identity_id
    }
    assert len({row.reservation_key for row in reservations}) == 2
    assert events
    assert {
        (row.stage, row.outcome)
        for row in events
        if row.tool_call_id == succeeded.tool_call_id
    } >= {
        ("ledger", "received"),
        ("idempotency", "claimed"),
        ("ledger", "executing"),
        ("ledger", "succeeded"),
    }
    assert {
        (row.stage, row.outcome, row.category)
        for row in events
        if row.tool_call_id == reconciling.tool_call_id
    } >= {
        ("ledger", "received", None),
        ("idempotency", "claimed", None),
        ("ledger", "executing", None),
        ("connector", "failed", "reconciliation_required"),
    }

    persisted = repr(
        [
            row.__dict__
            for row in [*attempts, *enrollments, *calls, *events, *reservations]
        ]
    )
    assert not any(marker in persisted for marker in _RAW_MARKERS)
    assert not {
        "recipient_address",
        "from_address",
        "subject",
        "body",
        "unsubscribe_url",
        "oauth_token",
    } & {column.name for column in ToolCallRow.__table__.columns}
    return {
        "tenant_id": str(tenant),
        "attempt_ids": expected_attempt_ids,
        "tool_call_ids": expected_tool_call_ids,
    }


async def test_demo_proves_success_duplicate_reconciliation_and_isolation(
    db_url: str,
) -> None:
    first_summary = _successful_summary(_run_demo(db_url))
    first_before = await _readback(db_url, first_summary)
    second_summary = _successful_summary(_run_demo(db_url))
    second = await _readback(db_url, second_summary)
    first_after = await _readback(db_url, first_summary)

    assert first_before == first_after
    assert first_before["tenant_id"] != second["tenant_id"]
    assert first_before["attempt_ids"].isdisjoint(second["attempt_ids"])
    assert first_before["tool_call_ids"].isdisjoint(second["tool_call_ids"])


def test_demo_invalid_dsn_is_fixed_and_never_echoes_marker() -> None:
    marker = "demo-password-marker"
    result = _run_demo(
        urlunsplit(
            (
                "postgresql+asyncpg",
                f"demo:{marker}@127.0.0.1:1",
                "/no_database",
                "",
                "",
            )
        )
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "Gmail 手动发送演示运行失败\n"
    assert marker not in result.stdout + result.stderr
