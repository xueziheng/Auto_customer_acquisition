"""邮件反馈只读工具经真实 PostgreSQL ledger 与本地 Gmail HTTP 的全链证明。"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.gmail.client import GmailConnector
from connectors.gmail.transport import GmailApiHttpTransport
from infra.db.session import create_engine_from
from infra.db.tables import ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolGateway

NOW = datetime(2026, 8, 13, 10, tzinfo=UTC)
TOKEN = "oauth-private-token-marker"
ADDRESS = "private-recipient@example.com"
DSN_MARKER = "private-dsn-marker"


def _dsn() -> bytes:
    boundary = "gateway-feedback-boundary"
    return "\r\n".join(
        [
            "Date: Thu, 13 Aug 2026 10:00:00 +0000",
            f'Content-Type: multipart/report; report-type="delivery-status"; boundary="{boundary}"',
            "MIME-Version: 1.0",
            "",
            f"--{boundary}",
            "Content-Type: message/delivery-status",
            "",
            "Reporting-MTA: dns; mx.example.invalid",
            "",
            f"Final-Recipient: rfc822; {ADDRESS}",
            "Status: 5.1.1",
            f"Diagnostic-Code: smtp; {DSN_MARKER}",
            "",
            f"--{boundary}",
            "Content-Type: message/rfc822",
            "",
            "Message-ID: <" + "a" * 64 + "@messages.tradeos.invalid>",
            "X-TradeOS-Idempotency-V1: " + "b" * 64,
            "",
            f"--{boundary}--",
            "",
        ]
    ).encode("ascii")


def _raw_payload(raw: bytes) -> bytes:
    return json.dumps(
        {"raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")},
        separators=(",", ":"),
    ).encode("ascii")


class _Scenario:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.responses = [
            b'{"historyId":"100"}',
            b'{"messages":[{"id":"gmail-safe-1"}]}',
            _raw_payload(_dsn()),
        ]


@contextmanager
def _gmail_server() -> Iterator[tuple[str, _Scenario]]:
    scenario = _Scenario()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            scenario.requests.append(
                (urlparse(self.path).path, self.headers.get("Authorization"))
            )
            body = scenario.responses.pop(0)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", scenario
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "GMAIL_OAUTH_TOKEN_REF"
        return TOKEN


class _TenantStage:
    name = "tenant"

    def __init__(self, expected: TenantId) -> None:
        self.expected = expected

    async def check(self, ctx, state):
        del state
        if ctx.tenant_id != self.expected:
            return CheckRejection("tenant", "tenant:mismatch", "租户不匹配")
        return None


class _PermissionStage:
    name = "permission"

    def __init__(self, allowed_user: UserId) -> None:
        self.allowed_user = allowed_user

    async def check(self, ctx, state):
        del state
        if ctx.user_id != self.allowed_user:
            return CheckRejection(
                "permission", "permission:denied", "当前操作者无权读取反馈"
            )
        return None


def _context(tenant: TenantId, user: UserId) -> ToolCallContext:
    return ToolCallContext(
        tenant_id=tenant,
        user_id=user,
        tool_id="email.feedback.fetch",
        params={
            "mailbox_alias": "feedback-primary",
            "cursor": None,
            "page_limit": 10,
        },
    )


@pytest.mark.asyncio
async def test_real_gateway_checks_precede_lazy_gmail_and_ledger_is_safe(
    db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = __import__(
        "tool_gateway.handlers.email_feedback",
        fromlist=["EmailFeedbackFetchHandler"],
    )
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    wrong_tenant = TenantId(new_id("tn"))
    user = UserId(new_id("usr"))
    denied_user = UserId(new_id("usr"))
    connector_factory_calls: list[TenantId] = []
    try:
        with _gmail_server() as (base_url, scenario):
            def connector_factory(requested_tenant: TenantId) -> GmailConnector:
                connector_factory_calls.append(requested_tenant)
                return GmailConnector(
                    GmailApiHttpTransport(base_url),
                    now=lambda: NOW,
                )

            provider_reader = module._GmailProviderEmailFeedbackReader(
                connector_factory,
                _Secrets(),
            )
            slot = module.FeedbackPageSlot()
            handler = module.EmailFeedbackFetchHandler(
                provider_reader,
                slot,
                HmacFingerprintProvider("feedback-v1", b"f" * 32),
            )
            registry = ToolRegistry()
            registry.register(module.MANIFEST, handler)
            gateway = ToolGateway(
                registry,
                {
                    "tenant": _TenantStage(tenant),
                    "permission": _PermissionStage(user),
                },
                lambda requested: SqlAlchemyToolGatewayUnitOfWork(
                    factory,
                    requested,
                    now=lambda: NOW,
                ),
                lease_duration=timedelta(minutes=1),
                lease_owner="feedback-reader",
                now=lambda: NOW,
                id_factory=new_id,
            )

            wrong = await gateway.invoke(_context(wrong_tenant, user))
            denied = await gateway.invoke(_context(tenant, denied_user))
            assert wrong.status is ToolCallStatus.REJECTED
            assert denied.status is ToolCallStatus.REJECTED
            assert connector_factory_calls == []
            assert scenario.requests == []

            reader = module.ToolGatewayEmailFeedbackReader(gateway, slot, user)
            page = await reader.fetch(tenant, "feedback-primary", None, 10)
            assert [item.kind.value for item in page.items] == ["hard_bounce"]
            assert slot.is_empty
            assert connector_factory_calls == [tenant]
            assert all(auth == f"Bearer {TOKEN}" for _, auth in scenario.requests)
            assert scenario.responses == []

        async with factory() as session:
            rows = (
                await session.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.tenant_id.in_((tenant, wrong_tenant))
                    )
                )
            ).scalars().all()
            events = (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id.in_((tenant, wrong_tenant))
                    )
                )
            ).scalars().all()
        assert len(rows) == 3
        assert {row.status for row in rows} == {"rejected", "succeeded"}
        succeeded = next(row for row in rows if row.status == "succeeded")
        assert isinstance(succeeded.provider_ref, str)
        assert succeeded.provider_ref.startswith("fpg_")
        row_values = [
            {
                column.name: getattr(row, column.name)
                for column in ToolCallRow.__table__.columns
            }
            for row in rows
        ]
        event_values = [
            {
                column.name: getattr(event, column.name)
                for column in ToolCallEventRow.__table__.columns
            }
            for event in events
        ]
        standard_log_keys = {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "taskName",
            "thread",
            "threadName",
        }
        log_values = [
            {
                "message": record.getMessage(),
                "args": record.args,
                "extras": {
                    key: value
                    for key, value in vars(record).items()
                    if key not in standard_log_keys
                },
            }
            for record in caplog.records
        ]
        rendered = repr((row_values, event_values, log_values))
        for forbidden in (
            TOKEN,
            ADDRESS,
            DSN_MARKER,
            "messages.tradeos.invalid",
            "X-TradeOS-Idempotency-V1",
            base_url,
        ):
            assert forbidden not in rendered
    finally:
        await engine.dispose()
