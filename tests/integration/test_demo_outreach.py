"""Slice 4B-1 触达域离线 PostgreSQL 演示的进程级验收。"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlunsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    OutboxEventRow,
    OutreachActionRow,
    OutreachCampaignRow,
    OutreachCampaignVersionRow,
    OutreachDailyQuotaRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OutreachSequenceStepRow,
    OutreachSuppressionRow,
)
from shared.schemas.identifiers import TenantId

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SUMMARY_KEYS = {
    "tenant_id",
    "campaign_id",
    "campaign_version",
    "enrollment_ids",
    "message_attempt_id",
    "suppression_id",
    "stopped_count",
    "outbox_counts",
}
_AUDIT_KEYS = {"message", "actor", "action", "tenant_id", "scope", "rule"}
_EXPECTED_ACTIONS = Counter(
    {
        "campaign:create": 1,
        "campaign:submit": 1,
        "campaign:activate": 1,
        "enrollment:create": 2,
        "enrollment:prepare_send": 1,
        "suppression:add": 1,
    }
)
_PRIVATE_MARKERS = {
    "Demo discovery campaign",
    "demo_contact_basis",
    "demo_reply_source",
    "sender-one@cold-demo.example",
    "sender-two@cold-demo.example",
    "cold-demo.example",
}
_CAMPAIGN_GRAPH_LINES = {
    "draft → pending_approval / cancelled",
    "pending_approval → active / cancelled",
    "active → paused / completed / cancelled / pending_approval",
    "paused → active / completed / cancelled / pending_approval",
    "completed / cancelled → 无后继",
}


def _run_demo(database_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/demo_outreach.py"],
        cwd=_REPO_ROOT,
        env={"DATABASE_URL": database_url, "PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _safe_failure(result: subprocess.CompletedProcess[str]) -> str:
    if "demo_outreach.py" in result.stderr and "No such file" in result.stderr:
        return "触达演示脚本缺失（file missing）"
    return "触达演示子进程失败（输出已脱敏）"


def _successful_run(
    result: subprocess.CompletedProcess[str],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    assert result.returncode == 0, _safe_failure(result)
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    summary = json.loads(result.stdout)
    assert isinstance(summary, dict) and set(summary) == _SUMMARY_KEYS
    assert isinstance(summary["tenant_id"], str)
    assert isinstance(summary["campaign_id"], str)
    assert summary["campaign_version"] == 1
    enrollment_ids = summary["enrollment_ids"]
    assert isinstance(enrollment_ids, list)
    assert len(enrollment_ids) == 2 and len(set(enrollment_ids)) == 2
    assert isinstance(summary["message_attempt_id"], str)
    assert isinstance(summary["suppression_id"], str)
    assert summary["stopped_count"] == 1
    assert summary["outbox_counts"] == {"CampaignStateChanged": 1, "SuppressionAdded": 1}

    audit = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(audit) == 7
    for record in audit:
        assert isinstance(record, dict) and set(record) == _AUDIT_KEYS
        assert record["message"] == "授权审计"
        assert record["tenant_id"] == summary["tenant_id"]
        assert record["scope"] in {"tenant", "system"}
        assert isinstance(record["rule"], str)
        assert str(record["rule"]).startswith("phase1:")
    assert Counter(record["action"] for record in audit) == _EXPECTED_ACTIONS

    rendered = result.stdout + result.stderr
    assert not any(marker in rendered for marker in _PRIVATE_MARKERS)
    assert not any(
        marker in rendered.lower()
        for marker in ("bearer", "password", "secret", "token")
    )
    return summary, audit


async def _readback(
    database_url: str, summary: dict[str, object]
) -> dict[str, object]:
    tenant = TenantId(summary["tenant_id"])
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            campaign = (
                await session.execute(
                    select(OutreachCampaignRow).where(
                        OutreachCampaignRow.tenant_id == tenant
                    )
                )
            ).scalar_one()
            versions = (
                await session.execute(
                    select(OutreachCampaignVersionRow).where(
                        OutreachCampaignVersionRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            steps = (
                await session.execute(
                    select(OutreachSequenceStepRow)
                    .where(OutreachSequenceStepRow.tenant_id == tenant)
                    .order_by(OutreachSequenceStepRow.step_number)
                )
            ).scalars().all()
            enrollments = (
                await session.execute(
                    select(OutreachEnrollmentRow)
                    .where(OutreachEnrollmentRow.tenant_id == tenant)
                    .order_by(OutreachEnrollmentRow.enrollment_id)
                )
            ).scalars().all()
            suppressions = (
                await session.execute(
                    select(OutreachSuppressionRow).where(
                        OutreachSuppressionRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            quotas = (
                await session.execute(
                    select(OutreachDailyQuotaRow).where(
                        OutreachDailyQuotaRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            attempts = (
                await session.execute(
                    select(OutreachMessageAttemptRow).where(
                        OutreachMessageAttemptRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            actions = (
                await session.execute(
                    select(OutreachActionRow).where(
                        OutreachActionRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            outbox = (
                await session.execute(
                    select(OutboxEventRow).where(
                        OutboxEventRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    assert campaign.campaign_id == summary["campaign_id"]
    assert campaign.state == "active" and campaign.current_version == 1
    assert campaign.approval_id is not None
    assert campaign.approved_by is not None and campaign.approved_at is not None
    assert len(versions) == 1 and versions[0].version == 1
    assert len(steps) == 2
    assert [(row.step_number, row.intent, row.wait_days) for row in steps] == [
        (1, "discovery", 0),
        (2, "follow_up", 2),
    ]
    assert {row.enrollment_id for row in enrollments} == set(
        summary["enrollment_ids"]
    )
    assert len({row.account_id for row in enrollments}) == 2
    assert len({row.sending_identity_id for row in enrollments}) == 2
    assert Counter(row.state for row in enrollments) == Counter(
        {"stopped_suppressed": 1, "enrolled": 1}
    )
    assert len(suppressions) == 1
    assert suppressions[0].suppression_id == summary["suppression_id"]
    assert suppressions[0].account_id is not None
    stopped = next(row for row in enrollments if row.state == "stopped_suppressed")
    assert stopped.account_id == suppressions[0].account_id
    assert len(attempts) == 1
    assert attempts[0].attempt_id == summary["message_attempt_id"]
    assert attempts[0].state == "reserved"
    active = next(row for row in enrollments if row.state == "enrolled")
    assert attempts[0].enrollment_id == active.enrollment_id
    assert len(quotas) == 1
    assert (quotas[0].new_contacts_reserved, quotas[0].messages_reserved) == (2, 1)
    assert len(actions) == 8
    assert Counter(row.action for row in actions) == Counter(
        {
            "campaign:create": 1,
            "campaign:submit": 1,
            "campaign:activate": 1,
            "enrollment:create": 2,
            "enrollment:prepare_send": 1,
            "suppression:add": 2,
        }
    )
    activation = next(row for row in actions if row.action == "campaign:activate")
    assert activation.action_key.endswith(f":activate:{campaign.approval_id}")
    assert Counter(row.event_type for row in outbox) == Counter(
        {"CampaignStateChanged": 1, "SuppressionAdded": 1}
    )
    assert all(row.event_type != "MessageSent" for row in outbox)
    return {
        "tenant": str(tenant),
        "campaign": campaign.campaign_id,
        "enrollments": {row.enrollment_id for row in enrollments},
        "suppression": suppressions[0].suppression_id,
    }


async def test_demo_runs_twice_and_database_readback_proves_isolation(
    db_url: str,
) -> None:
    first_summary, _first_audit = _successful_run(_run_demo(db_url))
    first_before = await _readback(db_url, first_summary)
    second_summary, _second_audit = _successful_run(_run_demo(db_url))
    second = await _readback(db_url, second_summary)
    first_after = await _readback(db_url, first_summary)
    assert first_before == first_after
    assert first_before["tenant"] != second["tenant"]
    assert first_before["campaign"] != second["campaign"]
    assert first_before["enrollments"].isdisjoint(second["enrollments"])
    assert first_before["suppression"] != second["suppression"]


def test_demo_invalid_dsn_is_fixed_and_does_not_leak_marker() -> None:
    marker = "password-demo-marker"
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
    assert result.stderr == "触达演示运行失败\n"
    assert marker not in result.stdout + result.stderr


async def test_demo_disposes_engine_when_composition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import logging

    demo = importlib.import_module("scripts.demo_outreach")
    root_logger = logging.getLogger()
    logging_before = (root_logger.level, tuple(root_logger.handlers))

    class DisposableEngine:
        def __init__(self) -> None:
            self.disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = DisposableEngine()
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")
    monkeypatch.setattr(demo, "_configure_safe_audit_logging", lambda: None)
    monkeypatch.setattr(demo, "create_engine_from", lambda _url: engine)

    def fail_factory(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("composition-marker")

    monkeypatch.setattr(demo, "async_sessionmaker", fail_factory)
    with pytest.raises(RuntimeError, match="composition-marker"):
        await demo.main()
    assert engine.disposed
    assert (root_logger.level, tuple(root_logger.handlers)) == logging_before


def test_outreach_agent_rules_lock_the_exact_campaign_graph() -> None:
    rules = (_REPO_ROOT / "domains/outreach/AGENTS.md").read_text()
    for transition in _CAMPAIGN_GRAPH_LINES:
        assert transition in rules
