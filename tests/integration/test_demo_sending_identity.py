"""Slice 4A 发件身份真实 PostgreSQL 演示的进程级验收。

子进程只接收 Alembic head 测试库的 ``DATABASE_URL``。成功后不信任 stdout
自述，而是用其中的随机 tenant 经独立 engine/session 回读七张业务表与 outbox。
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    AuthenticationCheckRow,
    IdentityActionRow,
    OutboxEventRow,
    ReputationEventRow,
    SendCounterRow,
    SendingDomainRow,
    SendingIdentityRow,
    SendReservationRow,
)
from scripts import demo_sending_identity
from shared.schemas.identifiers import TenantId

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOMAIN_AGENT_RULES = _REPO_ROOT / "domains/sending_identity/AGENTS.md"
_SENDING_IDENTITY_ARCHITECTURE = (
    _REPO_ROOT / "docs/architecture/07-sending-identity.md"
)
_SUMMARY_KEYS = {
    "tenant_id",
    "identity_ids",
    "final_states",
    "reservation_success_count",
    "history_counts",
    "outbox_count",
}
_AUDIT_LOG_KEYS = {
    "message",
    "actor",
    "action",
    "tenant_id",
    "scope",
    "rule",
}
_EXPECTED_AUDIT_ACTION_COUNTS = {
    "identity:register": 2,
    "auth:check_begin": 2,
    "auth:result_record": 2,
    "warmup:start": 2,
    "warmup:advance": 2,
    "send_slot:reserve": 56,
    "delivery_event:record": 4,
}


class _DisposableEngine:
    """只用于证明装配失败仍释放 engine，不建立任何连接。"""

    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def _run_demo(database_url: str) -> subprocess.CompletedProcess[str]:
    """用最小环境运行演示；失败细节只由安全分类函数对测试报告暴露。"""
    return subprocess.run(
        [sys.executable, "scripts/demo_sending_identity.py"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=_REPO_ROOT,
        env={"DATABASE_URL": database_url, "PYTHONPATH": str(_REPO_ROOT)},
        check=False,
    )


def _normalized_document(document: Path) -> str:
    """忽略换行宽度，只比较固定领域语义 token。"""
    return " ".join(document.read_text(encoding="utf-8").split())


def _safe_failure(result: subprocess.CompletedProcess[str]) -> str:
    """把 TDD 的脚本缺失与其他失败安全分类，不回显子进程原文。"""
    if "demo_sending_identity.py" in result.stderr and "No such file" in result.stderr:
        return "发件身份演示脚本缺失（file missing）"
    return "发件身份演示子进程失败（输出已脱敏）"


def _successful_run(
    result: subprocess.CompletedProcess[str],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """验证单行 summary 与固定字段审计日志；业务事实仍由数据库证明。"""
    assert result.returncode == 0, _safe_failure(result)
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    summary = json.loads(result.stdout)
    assert isinstance(summary, dict)
    assert set(summary) == _SUMMARY_KEYS
    assert isinstance(summary["tenant_id"], str)
    identity_ids = summary["identity_ids"]
    assert isinstance(identity_ids, list)
    assert len(identity_ids) == 2
    assert len(set(identity_ids)) == 2
    assert all(isinstance(identity_id, str) for identity_id in identity_ids)
    assert summary["final_states"] == ["suspended", "suspended"]
    assert summary["reservation_success_count"] == 5
    assert summary["history_counts"] == {
        "authentication_checks": 2,
        "identity_actions": 10,
        "reputation_events": 3,
    }
    assert summary["outbox_count"] == 5

    audit_records = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(audit_records) == 70
    assert all(isinstance(record, dict) for record in audit_records)
    for record in audit_records:
        assert set(record) == _AUDIT_LOG_KEYS
        assert record["message"] == "授权审计"
        assert record["tenant_id"] == summary["tenant_id"]
        assert record["scope"] in {"tenant", "system"}
        assert isinstance(record["rule"], str)
        assert str(record["rule"]).startswith("phase1:")
    assert Counter(record["action"] for record in audit_records) == Counter(
        _EXPECTED_AUDIT_ACTION_COUNTS
    )
    return summary, audit_records


async def _assert_independent_database_readback(
    database_url: str,
    summary: dict[str, object],
    audit_records: list[dict[str, object]],
) -> None:
    """按 summary tenant 独立回读，锁定 cap、窗口、幂等与熔断持久化事实。"""
    tenant_id = TenantId(summary["tenant_id"])
    ordered_identity_ids = summary["identity_ids"]
    assert isinstance(ordered_identity_ids, list)
    expected_identity_ids = set(ordered_identity_ids)
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            domains = (
                await session.execute(
                    select(SendingDomainRow).where(
                        SendingDomainRow.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            identities = (
                await session.execute(
                    select(SendingIdentityRow)
                    .where(SendingIdentityRow.tenant_id == tenant_id)
                    .order_by(SendingIdentityRow.identity_id)
                )
            ).scalars().all()
            authentication = (
                await session.execute(
                    select(AuthenticationCheckRow).where(
                        AuthenticationCheckRow.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            actions = (
                await session.execute(
                    select(IdentityActionRow).where(
                        IdentityActionRow.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            counters = (
                await session.execute(
                    select(SendCounterRow).where(SendCounterRow.tenant_id == tenant_id)
                )
            ).scalars().all()
            reservations = (
                await session.execute(
                    select(SendReservationRow).where(
                        SendReservationRow.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            reputation_events = (
                await session.execute(
                    select(ReputationEventRow).where(
                        ReputationEventRow.tenant_id == tenant_id
                    )
                )
            ).scalars().all()
            outbox = (
                await session.execute(
                    select(OutboxEventRow).where(OutboxEventRow.tenant_id == tenant_id)
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    assert len(domains) == 1
    assert domains[0].role == "cold_outreach"
    assert len(identities) == 2
    assert {row.identity_id for row in identities} == expected_identity_ids
    assert {row.domain for row in identities} == {domains[0].domain}
    assert {row.state for row in identities} == {"suspended"}
    assert {row.suspension_category for row in identities} == {"hard_bounce_rate"}
    assert {row.sendable_state_before_restriction for row in identities} == {"active"}
    assert all(row.activated_at is not None for row in identities)
    assert all(row.suspended_at is not None for row in identities)
    assert {row.target_daily_volume for row in identities} == {100}
    assert len({row.warmup_started_on for row in identities}) == 1

    assert len(authentication) == 2
    assert {row.identity_id for row in authentication} == expected_identity_ids
    assert all(
        row.spf_passed and row.dkim_passed and row.dmarc_passed
        for row in authentication
    )

    assert len(actions) == 10
    assert Counter(row.action for row in actions) == Counter(
        {
            "identity:register": 2,
            "auth:check_begin": 2,
            "warmup:start": 2,
            "warmup:advance": 2,
            "delivery_event:record": 2,
        }
    )
    for identity_id in expected_identity_ids:
        assert Counter(
            row.action for row in actions if row.identity_id == identity_id
        ) == Counter(
            {
                "identity:register": 1,
                "auth:check_begin": 1,
                "warmup:start": 1,
                "warmup:advance": 1,
                "delivery_event:record": 1,
            }
        )

    started_on = identities[0].warmup_started_on
    assert started_on is not None
    day29 = started_on + timedelta(days=28)
    day1_counters = [row for row in counters if row.on_day == started_on]
    day29_counters = [row for row in counters if row.on_day == day29]
    assert len(day1_counters) == 1
    assert day1_counters[0].sent_attempts == 5
    assert len(day29_counters) == 2
    assert {row.identity_id for row in day29_counters} == expected_identity_ids
    assert [row.sent_attempts for row in day29_counters] == [25, 25]

    assert len(reservations) == 55
    assert len(
        {(row.identity_id, row.reservation_key) for row in reservations}
    ) == 55
    assert len(
        [
            row
            for row in reservations
            if row.identity_id == day1_counters[0].identity_id
            and row.on_day == started_on
        ]
    ) == 5
    day29_reservations = [row for row in reservations if row.on_day == day29]
    assert len(day29_reservations) == 50
    assert Counter(row.identity_id for row in day29_reservations) == Counter(
        {identity_id: 25 for identity_id in expected_identity_ids}
    )
    assert all(row.created_at is not None for row in day29_reservations)
    activated_at = max(row.activated_at for row in identities if row.activated_at)
    rolling_reservations = [
        row
        for row in reservations
        if activated_at - timedelta(days=7) <= row.created_at <= activated_at
    ]
    assert len(rolling_reservations) == 50

    assert len(reputation_events) == 3
    assert {row.event_type for row in reputation_events} == {"hard_bounced"}
    assert len({row.dedup_key for row in reputation_events}) == 3
    assert Counter(row.identity_id for row in reputation_events) == Counter(
        {ordered_identity_ids[0]: 2, ordered_identity_ids[1]: 1}
    )
    assert Decimal(len(reputation_events)) / Decimal(len(rolling_reservations)) == Decimal(
        ".06"
    )
    assert all(
        sum(
            row.identity_id == identity_id
            for row in rolling_reservations
        )
        < 50
        for identity_id in expected_identity_ids
    )

    assert len(outbox) == 5
    assert Counter(row.event_type for row in outbox) == Counter(
        {
            "SendingIdentityActivated": 2,
            "SendingIdentitySuspended": 2,
            "ReputationThresholdBreached": 1,
        }
    )
    activated_payload_ids = {
        row.event_payload["sending_identity_id"]
        for row in outbox
        if row.event_type == "SendingIdentityActivated"
    }
    suspended_payload_ids = {
        row.event_payload["sending_identity_id"]
        for row in outbox
        if row.event_type == "SendingIdentitySuspended"
    }
    assert activated_payload_ids == expected_identity_ids
    assert suspended_payload_ids == expected_identity_ids
    breach = next(
        row for row in outbox if row.event_type == "ReputationThresholdBreached"
    )
    assert (
        breach.event_payload["sending_identity_id"],
        breach.event_payload["metric"],
        Decimal(str(breach.event_payload["value"])),
        Decimal(str(breach.event_payload["threshold"])),
        breach.event_payload["severity"],
    ) == (
        ordered_identity_ids[0],
        "hard_bounce_rate",
        Decimal(".06"),
        Decimal(".05"),
        "suspended",
    )

    assert Counter(record["action"] for record in audit_records)[
        "send_slot:reserve"
    ] == len(reservations) + 1
    assert Counter(record["action"] for record in audit_records)[
        "delivery_event:record"
    ] == len(reputation_events) + 1

    process_output = "\n".join(
        [json.dumps(summary, ensure_ascii=False), *(json.dumps(row) for row in audit_records)]
    )
    sensitive_values = {
        *(row.address for row in identities),
        *(row.domain for row in identities),
        *(row.connector_ref for row in identities if row.connector_ref),
        *(row.check_ref for row in authentication),
        *(row.source_ref for row in reputation_events),
    }
    assert all(value not in process_output for value in sensitive_values)


async def test_demo_sending_identity_runs_twice_with_real_service_and_isolated_readback(
    db_url: str,
) -> None:
    """省略 public service、错误计数或随机 tenant 时，两次同库回读必须暴露。"""
    first_result = _run_demo(str(db_url))
    second_result = _run_demo(str(db_url))
    first_summary, first_audit = _successful_run(first_result)
    second_summary, second_audit = _successful_run(second_result)

    assert first_summary["tenant_id"] != second_summary["tenant_id"]
    assert set(first_summary["identity_ids"]).isdisjoint(second_summary["identity_ids"])
    await _assert_independent_database_readback(str(db_url), first_summary, first_audit)
    await _assert_independent_database_readback(str(db_url), second_summary, second_audit)

    for result in (first_result, second_result):
        output = result.stdout + result.stderr
        assert str(db_url) not in output
        dsn_secret = urlsplit(str(db_url)).password
        assert dsn_secret is None or dsn_secret not in output


async def test_demo_sending_identity_disposes_engine_when_composition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """engine 创建后的任何装配失败都必须进入 finally 释放资源。"""
    engine = _DisposableEngine()

    def fail_composition(*args: object, **kwargs: object) -> None:
        raise RuntimeError("composition failed")

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://unused")
    monkeypatch.setattr(demo_sending_identity, "_configure_safe_audit_logging", lambda: None)
    monkeypatch.setattr(demo_sending_identity, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(demo_sending_identity, "async_sessionmaker", lambda **_: object())
    monkeypatch.setattr(
        demo_sending_identity,
        "SendingIdentityServiceImpl",
        fail_composition,
    )

    with pytest.raises(RuntimeError, match="composition failed"):
        await demo_sending_identity.main()

    assert engine.disposed


def test_sending_identity_docs_keep_complete_restriction_and_warmup_contract() -> None:
    """限制态退役边与低 target 预热基准不得在绑定文档中丢失。"""
    documents = (
        _normalized_document(_DOMAIN_AGENT_RULES),
        _normalized_document(_SENDING_IDENTITY_ARCHITECTURE),
    )
    required_transitions = (
        "throttled → warming/active/suspended/retired",
        "suspended → warming/active/retired",
    )
    for document in documents:
        assert all(transition in document for transition in required_transitions)

    assert "第 22–28 天从 `min(50,target)`" in documents[0]


def test_demo_sending_identity_failure_redacts_invalid_database_url() -> None:
    """连接失败只能返回固定中文边界，不得泄漏 DSN、driver error 或 traceback。"""
    marker = "s4a_task6_invalid_dsn_marker"
    invalid_url = (
        "postgresql+asyncpg://" + "demo" + ":" + marker + "@127.0.0.1:1/unreachable"
    )
    result = _run_demo(invalid_url)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "发件身份演示运行失败\n"
    assert invalid_url not in result.stdout + result.stderr
    assert marker not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "asyncpg" not in result.stderr
