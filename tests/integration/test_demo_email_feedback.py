"""邮件反馈离线 demo 的进程级与真实 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import (
    EmailFeedbackCursorRow,
    EmailFeedbackQuarantineRow,
    EmailFeedbackReceiptRow,
    OutboxEventRow,
    OutreachActionRow,
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    OutreachSuppressionRow,
    ReputationEventRow,
    UnsubscribeTokenRow,
)
from shared.schemas.identifiers import TenantId

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "demo_email_feedback.py"
_FAILURE = "邮件反馈演示运行失败\n"
_SUMMARY_KEYS = {
    "action_count",
    "campaign_id",
    "cursor_version",
    "duplicate_count",
    "enrollment_ids",
    "enrollment_states",
    "identity_id",
    "message_attempt_ids",
    "outbox_counts",
    "quarantine_count",
    "receipt_count",
    "reputation_count",
    "suppression_count",
    "tenant_id",
    "token_consumed_count",
}
_FORBIDDEN_MARKERS = (
    "demo-oauth-private-marker",
    "private-customer-body-marker",
    "private-customer@example.test",
    "feedback-demo-sender@example.test",
    "feedback-demo-recipient@example.test",
)
_WORKER_OAUTH_MARKER = "worker-oauth-private-marker"


def _run_demo(
    database_url: str, *, secret_ref: str | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {
        "DATABASE_URL": database_url,
        "TRADEOS_EMAIL_FEEDBACK_DEMO_MODE": "controlled",
    }
    if secret_ref is not None:
        environment["TRADEOS_EMAIL_FEEDBACK_DEMO_SECRET_REF"] = secret_ref
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _dsn(status: str, message_id: str, idempotency_header: str) -> bytes:
    boundary = "tradeos-worker-feedback-boundary"
    lines = [
        "Date: Thu, 13 Aug 2026 10:00:00 +0000",
        f'Content-Type: multipart/report; report-type="delivery-status"; boundary="{boundary}"',
        "MIME-Version: 1.0",
        "",
        f"--{boundary}",
        "Content-Type: text/plain",
        "",
        "worker-private-customer-body-marker",
        f"--{boundary}",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; mx.example.invalid",
        "",
        "Final-Recipient: rfc822; worker-private-customer@example.test",
        f"Status: {status}",
        "",
        f"--{boundary}",
        "Content-Type: message/rfc822",
        "",
        f"Message-ID: {message_id}",
        f"X-TradeOS-Idempotency-V1: {idempotency_header}",
        "",
        f"--{boundary}--",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


def _raw_payload(raw: bytes) -> bytes:
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return json.dumps({"raw": encoded}, separators=(",", ":")).encode("ascii")


class _WorkerGmailScenario:
    def __init__(self, raw_messages: dict[str, bytes]) -> None:
        self.raw_messages = raw_messages
        self.mode = "bootstrap"
        self.block_first_raw = True
        self.raw_started = threading.Event()
        self.raw_release = threading.Event()
        self.requests: list[tuple[str, dict[str, list[str]], float, bool]] = []


@contextmanager
def _gmail_server(
    raw_messages: dict[str, bytes],
) -> Iterator[tuple[str, _WorkerGmailScenario]]:
    scenario = _WorkerGmailScenario(raw_messages)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: bytes, **headers: str) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            scenario.requests.append(
                (
                    parsed.path,
                    query,
                    time.monotonic(),
                    self.headers.get("Authorization")
                    == f"Bearer {_WORKER_OAUTH_MARKER}",
                )
            )
            if parsed.path.endswith("/profile"):
                self._send(200, b'{"historyId":"200"}')
                return
            if parsed.path.endswith("/history"):
                if scenario.mode == "rate_limit":
                    self._send(429, b"worker-private-error", **{"Retry-After": "1"})
                    return
                added = ",".join(
                    f'{{"message":{{"id":"{message_ref}"}}}}'
                    for message_ref in scenario.raw_messages
                )
                self._send(
                    200,
                    (
                        '{"history":[{"messagesAdded":['
                        + added
                        + ']}],"historyId":"201"}'
                    ).encode("ascii"),
                )
                return
            if parsed.path.endswith("/messages"):
                messages = ",".join(
                    f'{{"id":"{message_ref}"}}'
                    for message_ref in scenario.raw_messages
                )
                self._send(200, (f'{{"messages":[{messages}]}}').encode("ascii"))
                return
            marker = "/gmail/v1/users/me/messages/"
            if marker in parsed.path:
                message_ref = parsed.path.split(marker, 1)[1]
                if scenario.block_first_raw and not scenario.raw_started.is_set():
                    scenario.raw_started.set()
                    if not scenario.raw_release.wait(timeout=10):
                        self._send(500, b"worker-timeout")
                        return
                self._send(200, _raw_payload(scenario.raw_messages[message_ref]))
                return
            self._send(404, b"worker-not-found")

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", scenario
    finally:
        scenario.raw_release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _worker_environment(
    database_url: str,
    summary: dict[str, object],
    base_url: str,
    health_port: int,
) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_WORKER_OAUTH_TOKEN",
        "GMAIL_WORKER_OAUTH_TOKEN": _WORKER_OAUTH_MARKER,
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_WORKER_FINGERPRINT_KEY",
        "TOOL_WORKER_FINGERPRINT_KEY": "w" * 32,
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "feedback-v1",
        "TRADEOS_DEV_MODE": "true",
        "TRADEOS_TENANT_ID": str(summary["tenant_id"]),
        "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL": base_url,
        "TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS": "worker-feedback",
        "TRADEOS_EMAIL_FEEDBACK_SENDING_IDENTITY_ID": str(summary["identity_id"]),
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-v1",
        "TRADEOS_EMAIL_FEEDBACK_ENABLED": "true",
        "TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT": str(health_port),
        "TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS": "5",
        "TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT": "100",
    }


def _start_worker(environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "apps.email_feedback_worker.main"],
        cwd=_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


async def _stop_worker(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.terminate()
    try:
        return await asyncio.to_thread(process.communicate, timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        return await asyncio.to_thread(process.communicate, timeout=10)


async def _health(port: int) -> tuple[int, dict[str, str]]:
    def request() -> tuple[int, dict[str, str]]:
        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1) as response:
            return response.status, json.loads(response.read())

    return await asyncio.to_thread(request)


async def _attempt_correlations(
    database_url: str, summary: dict[str, object]
) -> list[tuple[str, str]]:
    tenant = TenantId(summary["tenant_id"])
    attempt_ids = summary["message_attempt_ids"]
    assert isinstance(attempt_ids, list)
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(OutreachMessageAttemptRow).where(
                        OutreachMessageAttemptRow.tenant_id == tenant,
                        OutreachMessageAttemptRow.attempt_id.in_(attempt_ids),
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()
    by_id = {row.attempt_id: row for row in rows}
    assert set(by_id) == set(attempt_ids)
    values: list[tuple[str, str]] = []
    for attempt_id in attempt_ids:
        row = by_id[attempt_id]
        assert row.deterministic_message_id is not None
        assert row.idempotency_header is not None
        values.append((row.deterministic_message_id, row.idempotency_header))
    return values


async def _worker_state(
    database_url: str, tenant: str
) -> dict[str, object]:
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            cursor = await session.get(
                EmailFeedbackCursorRow, (tenant, "worker-feedback")
            )
            receipts = (
                await session.execute(
                    select(EmailFeedbackReceiptRow).where(
                        EmailFeedbackReceiptRow.tenant_id == tenant,
                        EmailFeedbackReceiptRow.mailbox_alias == "worker-feedback",
                    )
                )
            ).scalars().all()
            quarantines = (
                await session.execute(
                    select(EmailFeedbackQuarantineRow).where(
                        EmailFeedbackQuarantineRow.tenant_id == tenant,
                        EmailFeedbackQuarantineRow.mailbox_alias
                        == "worker-feedback",
                    )
                )
            ).scalars().all()
            reputation_count = len(
                (
                    await session.execute(
                        select(ReputationEventRow).where(
                            ReputationEventRow.tenant_id == tenant
                        )
                    )
                ).scalars().all()
            )
            suppression_count = len(
                (
                    await session.execute(
                        select(OutreachSuppressionRow).where(
                            OutreachSuppressionRow.tenant_id == tenant
                        )
                    )
                ).scalars().all()
            )
    finally:
        await engine.dispose()
    return {
        "cursor_version": cursor.version if cursor is not None else None,
        "quarantines": len(quarantines),
        "receipts": len(receipts),
        "reputation": reputation_count,
        "suppressions": suppression_count,
    }


async def _wait_for_worker_state(
    database_url: str,
    tenant: str,
    *,
    cursor_version: int,
    timeout: float = 15,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await _worker_state(database_url, tenant)
        if state["cursor_version"] == cursor_version:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError("邮件反馈 worker 未在限定时间内持久化 cursor")


async def _wait_for_health(
    port: int, *, provider: str, timeout: float = 15
) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, payload = await _health(port)
        except (OSError, URLError):
            await asyncio.sleep(0.1)
            continue
        if status == 200 and payload.get("provider") == provider:
            return payload
        await asyncio.sleep(0.1)
    raise AssertionError("邮件反馈 worker health 未在限定时间内达到预期")


def _summary(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, "邮件反馈演示子进程失败（输出已脱敏）"
    assert result.stderr == ""
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    value = json.loads(result.stdout)
    assert isinstance(value, dict) and set(value) == _SUMMARY_KEYS
    assert value["cursor_version"] == 1
    assert value["duplicate_count"] == 3
    assert value["receipt_count"] == 3
    assert value["quarantine_count"] == 1
    assert value["reputation_count"] == 1
    assert value["suppression_count"] == 2
    assert value["token_consumed_count"] == 1
    assert value["action_count"] == 17
    assert value["enrollment_states"] == [
        "stopped_bounced",
        "stopped_suppressed",
    ]
    assert value["outbox_counts"] == {"MessageSent": 2, "SuppressionAdded": 2}
    assert isinstance(value["tenant_id"], str)
    assert isinstance(value["identity_id"], str)
    assert isinstance(value["campaign_id"], str)
    assert isinstance(value["enrollment_ids"], list)
    assert isinstance(value["message_attempt_ids"], list)
    assert len(value["enrollment_ids"]) == len(set(value["enrollment_ids"])) == 2
    assert len(value["message_attempt_ids"]) == len(
        set(value["message_attempt_ids"])
    ) == 2
    combined_output = result.stdout + result.stderr
    assert all(marker not in combined_output for marker in _FORBIDDEN_MARKERS)
    return value


async def _readback(
    database_url: str, summary: dict[str, object]
) -> dict[str, object]:
    tenant = TenantId(summary["tenant_id"])
    engine = create_engine_from(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            cursor = await session.get(EmailFeedbackCursorRow, (tenant, "feedback"))
            receipts = (
                await session.execute(
                    select(EmailFeedbackReceiptRow)
                    .where(EmailFeedbackReceiptRow.tenant_id == tenant)
                    .order_by(EmailFeedbackReceiptRow.ordinal)
                )
            ).scalars().all()
            quarantines = (
                await session.execute(
                    select(EmailFeedbackQuarantineRow).where(
                        EmailFeedbackQuarantineRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            suppressions = (
                await session.execute(
                    select(OutreachSuppressionRow).where(
                        OutreachSuppressionRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            enrollments = (
                await session.execute(
                    select(OutreachEnrollmentRow)
                    .where(OutreachEnrollmentRow.tenant_id == tenant)
                    .order_by(OutreachEnrollmentRow.enrollment_id)
                )
            ).scalars().all()
            reputation = (
                await session.execute(
                    select(ReputationEventRow).where(
                        ReputationEventRow.tenant_id == tenant
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
                    select(OutboxEventRow).where(OutboxEventRow.tenant_id == tenant)
                )
            ).scalars().all()
            tokens = (
                await session.execute(
                    select(UnsubscribeTokenRow).where(
                        UnsubscribeTokenRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    assert cursor is not None
    assert cursor.version == 1
    assert isinstance(cursor.provider_cursor, str)
    assert cursor.provider_cursor.startswith("gfc1.")
    assert Counter((row.kind, row.result) for row in receipts) == Counter(
        {
            ("hard_bounce", "applied"): 1,
            ("soft_bounce", "recorded"): 1,
            ("unparseable", "quarantined"): 1,
        }
    )
    assert len(quarantines) == 1 and quarantines[0].reason == "malformed"
    assert Counter(row.reason for row in suppressions) == Counter(
        {"hard_bounce": 1, "unsubscribe": 1}
    )
    assert {row.state for row in enrollments} == {
        "stopped_bounced",
        "stopped_suppressed",
    }
    assert len(reputation) == 1
    assert reputation[0].event_type == "hard_bounced"
    assert reputation[0].identity_id == summary["identity_id"]
    hard_receipt = next(row for row in receipts if row.kind == "hard_bounce")
    assert reputation[0].dedup_key == hard_receipt.provider_event_id
    assert len(actions) == summary["action_count"]
    assert Counter(row.event_type for row in outbox) == Counter(
        summary["outbox_counts"]
    )
    assert len(tokens) == 1 and tokens[0].consumed_at is not None
    persisted = repr(
        [
            (row.kind, row.result, row.provider_event_id) for row in receipts
        ]
        + [(row.reason, row.provider_ref_digest) for row in quarantines]
        + [(row.reason, row.source_ref) for row in suppressions]
    )
    assert all(marker not in persisted for marker in _FORBIDDEN_MARKERS)
    return {
        "actions": len(actions),
        "cursor_version": cursor.version,
        "outbox": Counter(row.event_type for row in outbox),
        "quarantines": len(quarantines),
        "receipts": len(receipts),
        "reputation": len(reputation),
        "states": sorted(row.state for row in enrollments),
        "suppressions": len(suppressions),
        "tokens": sum(row.consumed_at is not None for row in tokens),
    }


@pytest.mark.asyncio
async def test_demo_email_feedback_runs_twice_with_isolated_readback(
    db_url: str,
) -> None:
    first = _summary(await asyncio.to_thread(_run_demo, db_url))
    first_before = await _readback(db_url, first)
    second = _summary(await asyncio.to_thread(_run_demo, db_url))
    second_state = await _readback(db_url, second)
    first_after = await _readback(db_url, first)

    assert first_before == first_after == second_state
    assert first["tenant_id"] != second["tenant_id"]
    assert first["identity_id"] != second["identity_id"]
    assert set(first["enrollment_ids"]).isdisjoint(second["enrollment_ids"])
    assert set(first["message_attempt_ids"]).isdisjoint(
        second["message_attempt_ids"]
    )


@pytest.mark.asyncio
async def test_real_worker_subprocess_bootstrap_restart_signal_and_degraded_health(
    db_url: str,
) -> None:
    summary = _summary(await asyncio.to_thread(_run_demo, db_url))
    correlations = await _attempt_correlations(db_url, summary)
    raw_messages = {
        "worker-hard-ref": _dsn("5.1.1", *correlations[0]),
        "worker-soft-ref": _dsn("4.2.2", *correlations[1]),
        "worker-malformed-ref": (
            b"Content-Type: multipart/report; report-type=delivery-status\r\n\r\n"
            b"worker-private-malformed"
        ),
    }
    tenant = str(summary["tenant_id"])
    health_port = _free_port()
    captured: list[str] = []

    with _gmail_server(raw_messages) as (base_url, scenario):
        environment = _worker_environment(db_url, summary, base_url, health_port)
        first = _start_worker(environment)
        try:
            assert await asyncio.to_thread(scenario.raw_started.wait, 15)
            health = await _wait_for_health(health_port, provider="ok")
            assert health == {"status": "ready", "provider": "ok"}
            first.terminate()
            scenario.raw_release.set()
            stdout, stderr = await _stop_worker(first)
            captured.extend((stdout, stderr))
            assert first.returncode == 0
        finally:
            if first.poll() is None:
                await _stop_worker(first)
            scenario.raw_release.set()

        first_state = await _wait_for_worker_state(
            db_url, tenant, cursor_version=1
        )
        assert first_state == {
            "cursor_version": 1,
            "quarantines": 1,
            "receipts": 3,
            "reputation": 2,
            "suppressions": 3,
        }
        bootstrap_requests = [
            item for item in scenario.requests if item[0].endswith("/messages")
        ]
        assert len(bootstrap_requests) == 1
        after_epoch = int(bootstrap_requests[0][1]["q"][0].removeprefix("after:"))
        assert abs((int(time.time()) - 30 * 24 * 60 * 60) - after_epoch) <= 3
        assert all(item[3] for item in scenario.requests)

        scenario.mode = "incremental"
        scenario.block_first_raw = False
        scenario.requests.clear()
        second = _start_worker(environment)
        try:
            duplicate_state = await _wait_for_worker_state(
                db_url, tenant, cursor_version=2
            )
            stdout, stderr = await _stop_worker(second)
            captured.extend((stdout, stderr))
            assert second.returncode == 0
        finally:
            if second.poll() is None:
                await _stop_worker(second)
        assert duplicate_state == {
            "cursor_version": 2,
            "quarantines": 1,
            "receipts": 3,
            "reputation": 2,
            "suppressions": 3,
        }
        history_requests = [
            item for item in scenario.requests if item[0].endswith("/history")
        ]
        assert len(history_requests) == 1
        assert history_requests[0][1]["startHistoryId"] == ["200"]
        assert not any(item[0].endswith("/messages") for item in scenario.requests)

        scenario.mode = "rate_limit"
        scenario.requests.clear()
        third = _start_worker(environment)
        try:
            degraded = await _wait_for_health(health_port, provider="degraded")
            assert degraded == {"status": "ready", "provider": "degraded"}
            deadline = time.monotonic() + 5
            rate_requests: list[tuple[str, dict[str, list[str]], float, bool]] = []
            while time.monotonic() < deadline:
                rate_requests = [
                    item
                    for item in scenario.requests
                    if item[0].endswith("/history")
                ]
                if len(rate_requests) >= 2:
                    break
                await asyncio.sleep(0.05)
            assert len(rate_requests) >= 2
            retry_delay = rate_requests[1][2] - rate_requests[0][2]
            assert 0.8 <= retry_delay <= 3
            stdout, stderr = await _stop_worker(third)
            captured.extend((stdout, stderr))
            assert third.returncode == 0
        finally:
            if third.poll() is None:
                await _stop_worker(third)

        assert await _worker_state(db_url, tenant) == duplicate_state
        combined = "".join(captured)
        for marker in (
            _WORKER_OAUTH_MARKER,
            "worker-private-customer-body-marker",
            "worker-private-customer@example.test",
            "worker-private-malformed",
            "worker-private-error",
            db_url,
        ):
            assert marker not in combined

    assert first.poll() is not None
    assert second.poll() is not None
    assert third.poll() is not None
    with pytest.raises((OSError, URLError)):
        await _health(health_port)


def test_demo_email_feedback_invalid_dsn_is_fixed_and_redacted() -> None:
    marker = "feedback_invalid_dsn_marker"
    result = _run_demo(
        f"postgresql+asyncpg://{marker}@127.0.0.1:1/db"
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == _FAILURE
    assert marker not in result.stdout + result.stderr


def test_demo_email_feedback_invalid_secret_ref_is_fixed_and_redacted(
    db_url: str,
) -> None:
    marker = "MISSING_SECRET_MARKER"
    result = _run_demo(db_url, secret_ref=marker)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == _FAILURE
    assert marker not in result.stdout + result.stderr


def test_email_feedback_docs_describe_shipped_and_deferred_scope() -> None:
    overview = (_ROOT / "docs/architecture/00-overview.md").read_text("utf-8")
    gateway = (_ROOT / "docs/architecture/04-tool-gateway.md").read_text("utf-8")
    outreach = (_ROOT / "docs/architecture/09-outreach-campaign.md").read_text(
        "utf-8"
    )
    environment = (_ROOT / "infra/.env.example").read_text("utf-8")

    assert "八个进程（七个已实现＋一个未来桌面进程）" in overview
    assert "apps/email-feedback-worker" in overview
    assert "Gmail RFC 3464 typed 反馈读取" in gateway
    assert "原始 MIME/header/address" in gateway
    assert "4C2 的反馈运营 UI" in outreach and "均未实现" in outreach
    assert (
        "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL=https://gmail.googleapis.com"
        in environment
    )
