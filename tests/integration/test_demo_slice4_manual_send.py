"""Slice 4 手动发送演示的进程级、真实 PostgreSQL 验收。

同一 migrated PostgreSQL 上跑两次演示脚本，断言：不同租户、每次恰一次
provider 发送、真实 DNS 认证事实、一次 hard bounce + 一次 complaint、
身份 suspended、第二次发送被真实身份门禁阻断、一条站内通知与各渠道投递
状态；无效 DSN/OAuth/DNS 配置 → 非零退出 + 固定中文 stderr + 无 marker。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlunsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    AuthenticationCheckRequestRow,
    InAppNotificationRow,
    NotificationDeliveryRow,
    NotificationJobRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    ToolCallRow,
)
from shared.schemas.identifiers import TenantId

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAILURE_MESSAGE = "Slice 4 演示运行失败\n"
_SUMMARY_KEYS = {
    "auth_check_ref_prefix",
    "auth_request_id",
    "auth_request_status",
    "complaint_recorded",
    "dns_queries",
    "email_delivery_status",
    "gmail_search_count",
    "hard_bounce_recorded",
    "identity_id",
    "identity_state",
    "in_app_delivery_status",
    "inbox_notification_count",
    "provider_send_count",
    "second_send_blocked",
    "second_send_category",
    "spam_trap_recorded",
    "send_status",
    "tenant_id",
    "tool_call_ids",
}
_RAW_MARKERS = (
    "demo-recipient-marker@example.test",
    "demo-sender-marker@example.test",
    "demo-subject-marker",
    "demo-body-marker",
    "boss-notify-marker@example.test",
    "demo-oauth-value-marker",
    "https://unsubscribe.example.test/",
    "slice4-dns-marker",
)


def _run_demo(
    database_url: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "DATABASE_URL": database_url,
        "TRADEOS_SLICE4_DEMO_MODE": "controlled",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "scripts/demo_slice4_manual_send.py"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


def _safe_failure(result: subprocess.CompletedProcess[str]) -> str:
    if "demo_slice4_manual_send.py" in result.stderr and "No such file" in result.stderr:
        return "Slice 4 演示脚本缺失（file missing）"
    return "Slice 4 演示子进程失败（输出已脱敏）"


def _successful_summary(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, _safe_failure(result)
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    summary = json.loads(result.stdout)
    assert isinstance(summary, dict) and set(summary) == _SUMMARY_KEYS
    assert isinstance(summary["tenant_id"], str)
    assert isinstance(summary["identity_id"], str)
    # 发送：恰一次 provider 调用
    assert summary["send_status"] == "succeeded"
    assert summary["provider_send_count"] == 1
    # 认证：真实 DNS 查询 + dns_ 前缀 + 请求 SUCCEEDED
    assert set(summary["dns_queries"]) == {
        "cold.example.com.",
        "s1._domainkey.cold.example.com.",
        "_dmarc.cold.example.com.",
    }
    assert summary["auth_check_ref_prefix"] == "dns_"
    assert summary["auth_request_status"] == "succeeded"
    # 反馈与熔断
    assert summary["hard_bounce_recorded"] is True
    assert summary["complaint_recorded"] is True
    assert summary["spam_trap_recorded"] is True
    assert summary["identity_state"] == "suspended"
    # 第二次发送被真实身份门禁阻断
    assert summary["second_send_blocked"] is True
    assert summary["second_send_category"] == "provider_permanent"
    # 通知：一条站内通知 + 双渠道投递完成
    assert summary["inbox_notification_count"] == 1
    assert summary["in_app_delivery_status"] == "delivered"
    assert summary["email_delivery_status"] == "delivered"
    tool_call_ids = summary["tool_call_ids"]
    assert isinstance(tool_call_ids, list) and len(tool_call_ids) == 3
    assert len(set(tool_call_ids)) == 3
    rendered = result.stdout + result.stderr
    assert not any(marker in rendered for marker in _RAW_MARKERS)
    return summary


async def _readback(
    database_url: str, summary: dict[str, object]
) -> dict[str, object]:
    tenant = TenantId(summary["tenant_id"])  # type: ignore[arg-type]
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            attempts = (
                await session.execute(
                    select(OutreachMessageAttemptRow)
                    .where(OutreachMessageAttemptRow.tenant_id == tenant)
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
                    select(ToolCallRow).where(ToolCallRow.tenant_id == tenant)
                )
            ).scalars().all()
            auth_requests = (
                await session.execute(
                    select(AuthenticationCheckRequestRow).where(
                        AuthenticationCheckRequestRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            jobs = (
                await session.execute(
                    select(NotificationJobRow).where(
                        NotificationJobRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            inbox = (
                await session.execute(
                    select(InAppNotificationRow).where(
                        InAppNotificationRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            deliveries = (
                await session.execute(
                    select(NotificationDeliveryRow).where(
                        NotificationDeliveryRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    assert len(attempts) == 2
    assert Counter(row.state for row in attempts) == Counter(
        {"sent": 1, "failed_permanent": 1}
    )
    sent = next(row for row in attempts if row.state == "sent")
    assert sent.provider_ref == "gmail-slice4-sent-1"
    blocked = next(row for row in attempts if row.state == "failed_permanent")
    assert blocked.provider_ref is None
    assert Counter(row.state for row in enrollments) == Counter(
        {"completed": 1, "stopped_identity_unavailable": 1}
    )

    # 工具账本：恰一条 email.send succeeded、一条 dns.auth.check succeeded、
    # 一条 notification.email.send succeeded、一条被拒的 email.send
    by_tool: dict[str, list[object]] = {}
    for row in calls:
        by_tool.setdefault(row.tool_id, []).append(row)
    assert [row.status for row in by_tool.get("dns.auth.check", [])].count(
        "succeeded"
    ) == 1
    assert [row.status for row in by_tool.get("email.send", [])].count(
        "succeeded"
    ) == 1
    assert [row.status for row in by_tool.get("email.send", [])].count(
        "failed_permanent"
    ) == 1
    assert [
        row.status for row in by_tool.get("notification.email.send", [])
    ].count("succeeded") == 1
    blocked_call = next(
        row
        for row in by_tool.get("email.send", [])
        if row.status == "failed_permanent"
    )
    assert blocked_call.error_category == "provider_permanent"

    assert len(auth_requests) == 1
    assert auth_requests[0].status == "succeeded"
    assert auth_requests[0].completed_at is not None

    # 通知：一条 job、一条站内、双渠道投递 completed
    assert len(jobs) == 1
    assert jobs[0].status == "completed"
    assert len(inbox) == 1
    assert len(deliveries) == 2
    assert Counter(row.channel_name for row in deliveries) == Counter(
        {"in_app": 1, "email": 1}
    )
    assert {row.status for row in deliveries} == {"delivered"}

    persisted = repr(
        [
            row.__dict__
            for row in [
                *attempts,
                *enrollments,
                *calls,
                *auth_requests,
                *jobs,
                *inbox,
                *deliveries,
            ]
        ]
    )
    assert not any(marker in persisted for marker in _RAW_MARKERS)
    return {
        "tenant_id": str(tenant),
        "tool_call_ids": frozenset(summary["tool_call_ids"]),  # type: ignore[arg-type]
    }


async def test_demo_proves_slice4_journey_and_isolation(db_url: str) -> None:
    first_summary = _successful_summary(_run_demo(db_url))
    first_before = await _readback(db_url, first_summary)
    second_summary = _successful_summary(_run_demo(db_url))
    second = await _readback(db_url, second_summary)
    first_after = await _readback(db_url, first_summary)

    assert first_before == first_after
    assert first_before["tenant_id"] != second["tenant_id"]
    assert first_before["tool_call_ids"].isdisjoint(second["tool_call_ids"])


def test_demo_invalid_dsn_is_fixed_and_never_echoes_marker() -> None:
    marker = "slice4-password-marker"
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
    assert result.stderr == _FAILURE_MESSAGE
    assert marker not in result.stdout + result.stderr


async def test_demo_invalid_oauth_config_is_fixed_and_never_echoes_marker(
    db_url: str,
) -> None:
    marker = "slice4-oauth-marker"
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            before = (
                await session.execute(
                    select(func.count()).select_from(AuthenticationCheckRequestRow)
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    result = _run_demo(db_url, extra_env={"TRADEOS_SLICE4_GMAIL_REF": marker})
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == _FAILURE_MESSAGE
    assert marker not in result.stdout + result.stderr
    # 证据：OAuth 门禁在 DB 前置失败——未创建任何认证请求行
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            after = (
                await session.execute(
                    select(func.count()).select_from(AuthenticationCheckRequestRow)
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert after == before


async def test_demo_invalid_dns_config_fails_inside_auth_workflow(db_url: str) -> None:
    # 明确已关闭的本地端口：bind 后立即 close，端口不再监听
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            snapshot = (
                await session.execute(
                    select(func.max(ToolCallRow.created_at))
                )
            ).scalar_one()
            if snapshot is None:
                snapshot = datetime.min.replace(tzinfo=UTC)
    finally:
        await engine.dispose()
    result = _run_demo(
        db_url,
        extra_env={"TRADEOS_SLICE4_DNS_PORT": str(closed_port)},
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == _FAILURE_MESSAGE
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            dns_rows = (
                await session.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.tool_id == "dns.auth.check",
                        ToolCallRow.created_at > snapshot,
                    )
                )
            ).scalars().all()
            auth_rows = (
                await session.execute(
                    select(AuthenticationCheckRequestRow).where(
                        AuthenticationCheckRequestRow.requested_at > snapshot
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()
    # 证据：运行确实进入认证 workflow/DNS 阶段后 fail-closed——
    # 真实 connector 对不可达端口查询失败并留下 failed_transient 工具行；
    # 新租户的认证请求已被 workflow 推进（requested/running），未完成。
    assert any(row.status == "failed_transient" for row in dns_rows), (
        "未观察到进入 DNS 工具阶段的证据"
    )
    assert any(row.status in {"requested", "running"} for row in auth_rows), (
        "未观察到认证请求被 workflow 消费的证据"
    )
