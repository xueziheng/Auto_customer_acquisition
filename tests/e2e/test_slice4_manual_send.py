"""Task 7 Browser E2E：真实 Vite + 真实 API + fake Gmail HTTP 的固定旅程。

旅程：open outreach → prepare → send once → DB sent；identity center → auth
check → result visible；hard bounce + complaint 注入 → identity suspended →
retry fail-closed；notifications → in-app alert → mark read。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import dns.asyncresolver
import dns.message
import dns.rdatatype
import dns.rrset
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright, expect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from apps.api.runtime_config import Phase1RuntimeSettings
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from domains.sending_identity.permissions import Actor as SendingIdentityActor
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.permissions import SendingIdentityScope
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DeliveryEventRecord,
    DeliveryEventType,
    IdentityRegisterRequest,
)
from infra.db.session import create_engine_from
from infra.db.tables import (
    AuthenticationCheckRequestRow,
    AuthenticationCheckRow,
    InAppNotificationRow,
    NotificationJobRow,
    OutreachMessageAttemptRow,
    ToolCallRow,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    IdempotencyKey,
    NotificationId,
    NotificationJobId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tests.outreach_fakes import FakeApprovals, FakeContacts, FakeReplies, FakeSenders

_NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTAINER_IMAGE = "pgvector/pgvector:pg16"


class _FakeGmailServer:
    """本地 Gmail API 假服务：记录 send 次数，其余端点返回固定空结果。"""

    def __init__(self) -> None:
        self.send_count = 0
        self.sent_bodies: list[bytes] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self, payload: dict[str, object], status: int = 200) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/gmail/v1/users/me/profile":
                    return self._respond({"historyId": "100"})
                if parsed.path == "/gmail/v1/users/me/messages":
                    return self._respond({"messages": []})
                if parsed.path == "/gmail/v1/users/me/history":
                    return self._respond({"history": [], "historyId": "101"})
                if parsed.path.startswith("/gmail/v1/users/me/messages/"):
                    return self._respond(
                        {"raw": base64.urlsafe_b64encode(b"empty").decode("ascii")}
                    )
                return self._respond({"error": "not_found"}, 404)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/gmail/v1/users/me/messages/send":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    server.send_count += 1
                    server.sent_bodies.append(raw)
                    return self._respond({"id": f"gmail-sent-{server.send_count}"})
                return self._respond({"error": "not_found"}, 404)

            def log_message(self, _format: str, *_args: object) -> None:
                return None

        return Handler

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _runtime_env(
    database_url: str,
    tenant_id: TenantId,
    vite_origin: str,
    fake_gmail_url: str,
    *,
    boss: EmployeeId,
    campaign: CampaignId,
    approval: ApprovalId,
    identity: SendingIdentityId,
    contact: ContactPointId,
    account: ProspectAccountId,
) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(_REPO_ROOT),
        "LANG": "C.UTF-8",
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": str(tenant_id),
        "TRADEOS_DEV_MODE": "true",
        "TRADEOS_CORS_ALLOWED_ORIGINS": json.dumps([vite_origin]),
        "TRADEOS_API_RETRY_AFTER_SECONDS": "2",
        "TRADEOS_HANDOFF_POLICY": json.dumps(
            {
                "sla_seconds": 30,
                "backlog_threshold": 10,
                "t1_seconds": 2,
                "t2_seconds": 2,
            }
        ),
        "TRADEOS_SCORING_POLICY": json.dumps(
            {
                "version": "e2e-v1",
                "currency": "USD",
                "value_band_boundaries": ["1000.00", "5000.00"],
                "bucket_map": {
                    "1": "low", "2": "low", "3": "mid", "4": "mid",
                    "5": "high", "6": "high", "7": "high",
                },
            }
        ),
        "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_SLICE4",
        "GMAIL_OAUTH_TOKEN_SLICE4": "g" * 32,
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_SLICE4",
        "TOOL_FINGERPRINT_SLICE4": "f" * 32,
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": '{"2026-v1":"UNSUBSCRIBE_HMAC_2026"}',
        "UNSUBSCRIBE_HMAC_2026": "u" * 32,
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TRADEOS_E2E_FAKE_GMAIL_URL": fake_gmail_url,
        "TRADEOS_E2E_BOSS_ID": str(boss),
        "TRADEOS_E2E_CAMPAIGN_ID": str(campaign),
        "TRADEOS_E2E_APPROVAL_ID": str(approval),
        "TRADEOS_E2E_IDENTITY_ID": str(identity),
        "TRADEOS_E2E_CONTACT_ID": str(contact),
        "TRADEOS_E2E_ACCOUNT_ID": str(account),
    }


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _FakeDnsProtocol(asyncio.DatagramProtocol):
    """本地 fake DNS（UDP）：按 fqdn 返回固定 TXT 记录并记录查询证据。"""

    def __init__(self, records: dict[str, tuple[str, ...]]) -> None:
        self.records = records
        self.queries: list[tuple[str, str]] = []
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: object) -> None:
        try:
            query = dns.message.from_wire(data)
            question = query.question[0]
            name = question.name.to_text()
            rdtype = dns.rdatatype.to_text(question.rdtype)
            self.queries.append((name, rdtype))
            response = dns.message.make_response(query)
            strings = self.records.get(name)
            if strings is not None:
                # TXT rdata 必须加引号：from_text 会把未引号值按空白拆成多个
                # character-string，破坏 SPF/DKIM/DMARC 记录内容
                response.answer.append(
                    dns.rrset.from_text(
                        name, 60, "IN", "TXT", *(f'"{value}"' for value in strings)
                    )
                )
            wire = response.to_wire()
        except Exception:  # noqa: BLE001 - fake 服务对畸形包静默丢弃
            return
        if self._transport is not None:
            self._transport.sendto(wire, addr)  # type: ignore[arg-type]


class _LocalDnsResolver:
    """把 dnspython 生产 resolver 指向本地 fake DNS 端口的注入实现。"""

    def __init__(self, port: int) -> None:
        self._port = port

    async def resolve(self, name: str, rdtype: str) -> object:
        resolver = dns.asyncresolver.Resolver()
        resolver.nameservers = ["127.0.0.1"]
        resolver.port = self._port
        resolver.lifetime = 2.0
        return await resolver.resolve(name, rdtype)


def _spawn_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout: object,
    stderr: object,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.Popen[bytes]:
    # 同步辅助：从 async fixture 中以 to_thread 语义调用，避免 ASYNC220
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        pass_fds=pass_fds,
    )


def _request_status(url: str, headers: dict[str, str] | None = None) -> int:
    request = Request(url, headers=headers or {})
    try:
        with urlopen(request, timeout=1) as response:
            response.read()
            return response.status
    except HTTPError as exc:
        return exc.code


async def _wait_ready(
    url: str,
    process: subprocess.Popen[bytes],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 25
    while loop.time() < deadline:
        if process.poll() is not None:
            raise AssertionError("服务进程提前退出")
        try:
            status = await asyncio.to_thread(_request_status, url, headers)
        except (OSError, URLError):
            status = 0
        if status == 200:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("服务 readiness 超时")


def _to_asyncpg(url: str) -> str:
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise ValueError("未知连接串 scheme")


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], capture_output=True, check=False, timeout=5
    )
    return result.returncode == 0


@pytest_asyncio.fixture(scope="session")
async def slice4_stack() -> AsyncIterator[dict[str, object]]:
    if not await asyncio.to_thread(_docker_available):
        if os.environ.get("TRADEOS_REQUIRE_E2E") == "1":
            pytest.fail("Docker 不可用，必需的真实浏览器 E2E 无法启动")
        pytest.skip("Docker 不可用")

    temporary = TemporaryDirectory(prefix="tradeos-slice4-e2e-")
    temporary_path = Path(temporary.name)
    container = PostgresContainer(_CONTAINER_IMAGE)
    container_started = False
    fake_gmail = _FakeGmailServer()
    engine: AsyncEngine | None = None
    processes: list[object] = []
    dns_resources: list[asyncio.DatagramTransport] = []
    try:
        await asyncio.to_thread(container.start)
        container_started = True
        database_url = _to_asyncpg(container.get_connection_url())
        migration = await asyncio.to_thread(
            subprocess.run,
            ["alembic", "upgrade", "head"],
            cwd=_REPO_ROOT,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            check=False,
        )
        assert migration.returncode == 0, "alembic upgrade head 失败"

        engine = create_engine_from(database_url)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        tenant = TenantId(new_id("tn"))
        boss = EmployeeId(new_id("emp"))
        campaign = CampaignId(new_id("cmp"))
        approval = ApprovalId(new_id("apr"))
        identity = SendingIdentityId(new_id("sid"))
        contact = ContactPointId(new_id("cp"))
        account = ProspectAccountId(new_id("acc"))
        yield_dict: dict[str, object] = {}

        def env_for(vite_origin: str) -> dict[str, str]:
            return _runtime_env(
                database_url,
                tenant,
                vite_origin,
                fake_gmail.base_url,
                boss=boss,
                campaign=campaign,
                approval=approval,
                identity=identity,
                contact=contact,
                account=account,
            )

        # 先建 engine 级 dependencies（无 manual_send）用于真实服务播种。
        from apps.api.composition.runtime import (
            ManualSendComposition,
            build_phase1_dependencies,
        )
        from tool_gateway.handlers.email_send import DeliveryMaterial

        base_env = env_for("http://127.0.0.1:1")
        settings = Phase1RuntimeSettings.from_environ(base_env)
        seeding_deps = build_phase1_dependencies(
            settings,
            factory,
            now=lambda: _NOW,
            secret_resolver=__import__("infra.secrets", fromlist=["EnvironmentSecretResolver"]).EnvironmentSecretResolver(base_env),
        )
        # employee（真实仓储）
        from infra.db.repositories.employees import EmployeeRepositoryImpl

        models = __import__("domains.employees.models", fromlist=["Employee", "Role"])
        async with factory() as session:
            await EmployeeRepositoryImpl(session, tenant).add(
                models.Employee(
                    employee_id=boss,
                    tenant_id=tenant,
                    user_id=UserId(new_id("usr")),
                    name="Slice4 Boss",
                    role=models.Role.BOSS,
                    created_at=_NOW,
                )
            )
            await session.commit()
        # sending identity（真实服务）
        boss_identity = SendingIdentityActor(
            str(boss),
            SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
            "boss",
        )
        identity = await seeding_deps.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address="sales@cold.example.com",
                domain="cold.example.com",
                role=__import__("domains.sending_identity.models", fromlist=["DomainRole"]).DomainRole.COLD_OUTREACH,
            ),
            actor=boss_identity,
        )
        system_identity = SendingIdentityActor(
            "system:slice4-e2e",
            SendingIdentityScope(
                level=SendingIdentityScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({identity}),
            ),
            "system",
        )
        await seeding_deps.sending_identities.begin_authentication(
            tenant, identity, actor=boss_identity
        )
        await seeding_deps.sending_identities.record_authentication_result(
            tenant,
            identity,
            AuthenticationResult(
                checked_at=_NOW,
                spf_passed=True,
                dkim_passed=True,
                dmarc_passed=True,
                failures=(),
                check_ref="auth_slice4_e2e",
            ),
            actor=system_identity,
        )
        await seeding_deps.sending_identities.start_warmup(
            tenant, identity, 5, actor=boss_identity
        )
        # campaign（真实仓储）
        outreach_models = __import__("domains.outreach.models", fromlist=["Campaign", "CampaignBoundary", "CampaignState", "CampaignVersion", "SequenceStepSpec", "StepIntent"])
        boundary = outreach_models.CampaignBoundary(
            markets=("US",),
            target_entity_types=("importer",),
            allowed_categories=("hardware",),
            sender_identity_ids=(identity,),
            steps=(outreach_models.SequenceStepSpec(1, outreach_models.StepIntent.DISCOVERY, 0),),
            daily_new_contact_limit=10,
            daily_total_message_limit=20,
            handoff_triggers=(),
        )
        campaign_row = outreach_models.Campaign(
            tenant,
            campaign,
            outreach_models.CampaignState.ACTIVE,
            1,
            boss,
            _NOW,
            approval_id=str(approval),
            approved_by=boss,
            approved_at=_NOW,
        )
        version = outreach_models.CampaignVersion(
            tenant, campaign, 1, "Slice4 E2E", boundary, boss, _NOW
        )
        from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork

        async with SqlAlchemyOutreachUnitOfWork(
            factory, tenant, now=lambda: _NOW
        ) as uow:
            await uow.campaigns.add(campaign_row, version)
        # enrollment（真实服务 + 确定性 current-fact providers）
        trace = __import__("tests.outreach_fakes", fromlist=["Trace"]).Trace()
        contact_snapshots = {
            (contact, account): ContactEligibilitySnapshot(
                tenant,
                contact,
                account,
                ContactVerificationStatus.VERIFIED,
                _NOW,
                ContactLegalBasis.LEGITIMATE_INTEREST,
                "basis_slice4_e2e",
                True,
                "US",
                "importer",
                frozenset({"hardware"}),
                _NOW,
            )
        }
        reply_snapshots = {
            (contact, account): ReplyStatusSnapshot(
                tenant, contact, account, ReplyState.NO_REPLY, None, _NOW
            )
        }
        approvals = FakeApprovals(trace)
        approvals.values[(campaign, 1)] = CampaignApprovalSnapshot(
            tenant, campaign, 1, approval, CampaignApprovalState.APPROVED, boss, _NOW
        )
        senders = FakeSenders(
            {
                identity: SendingIdentityEligibilitySnapshot(
                    tenant,
                    identity,
                    OutreachSenderRole.COLD_OUTREACH,
                    True,
                    True,
                    5,
                    _NOW,
                )
            },
            trace,
        )

        class _Materials:
            async def resolve(self, tenant_id: TenantId, preflight: object) -> DeliveryMaterial:
                return DeliveryMaterial(
                    tenant_id=tenant_id,
                    attempt_id=preflight.attempt_id,
                    account_id=preflight.account_id,
                    contact_point_id=preflight.contact_point_id,
                    sending_identity_id=preflight.sending_identity_id,
                    from_address="sales@cold.example.com",
                    recipient_address="customer@example.test",
                )

        manual = ManualSendComposition(
            contact_eligibility=FakeContacts(contact_snapshots, trace),
            sending_identity_eligibility=senders,
            campaign_approvals=approvals,
            reply_status=FakeReplies(reply_snapshots, trace),
            delivery_materials=_Materials(),
            secret_resolver=__import__("infra.secrets", fromlist=["EnvironmentSecretResolver"]).EnvironmentSecretResolver(base_env),
            gmail_transport=__import__("connectors.gmail.transport", fromlist=["GmailApiHttpTransport"]).GmailApiHttpTransport(fake_gmail.base_url),
        )
        app_deps = build_phase1_dependencies(
            settings,
            factory,
            now=lambda: _NOW,
            manual_send=manual,
            secret_resolver=__import__("infra.secrets", fromlist=["EnvironmentSecretResolver"]).EnvironmentSecretResolver(base_env),
        )
        enrollment = await app_deps.outreach.enroll(
            tenant,
            campaign,
            EnrollmentCreateRequest(account, contact, IdempotencyKey("slice4-e2e-enrollment")),
            actor=OutreachActor(str(boss), OutreachScope(level=OutreachScopeLevel.TENANT), "boss"),
        )
        # notification（真实表 + 真实 store append）
        from infra.db.repositories.in_app_notifications import (
            PostgresInAppNotificationStore,
        )
        from notification_gateway.inbox import InAppNotification
        from notification_gateway.jobs import NotificationContext, NotificationKind
        from notification_gateway.models import NotificationPriority

        job_id = NotificationJobId(new_id("njb"))
        notification_id = NotificationId(new_id("ntf"))
        async with factory() as session:
            session.add(
                NotificationJobRow(
                    tenant_id=str(tenant),
                    notification_job_id=str(job_id),
                    source_event_fingerprint="f" * 64,
                    source_event="HandoffRequested",
                    recipient_employee_id=str(boss),
                    priority="urgent",
                    context_kind="handoff_escalation",
                    primary_id=str(new_id("hnd")),
                    secondary_id=None,
                    reason_code="t1",
                    level=None,
                    dedup_key="slice4-e2e-notification",
                    status="completed",
                    available_at=_NOW,
                    created_at=_NOW,
                )
            )
            await session.commit()
        await PostgresInAppNotificationStore(factory).append(
            InAppNotification(
                notification_id=notification_id,
                tenant_id=tenant,
                recipient=boss,
                priority=NotificationPriority.URGENT,
                title="人工接管提醒（E2E）",
                context=NotificationContext(
                    NotificationKind.HANDOFF_ESCALATION,
                    str(new_id("hnd")),
                    None,
                    reason_code="t1",
                    level=None,
                ),
                relative_link="/crm/handoffs",
                source_job_id=job_id,
                created_at=_NOW,
            )
        )
        # 本地 fake DNS（UDP）：真实 connector 经注入 resolver 查询它
        dns_port = _free_port()
        fake_dns = _FakeDnsProtocol(
            {
                "cold.example.com.": ("v=spf1 -all",),
                "s1._domainkey.cold.example.com.": (
                    "v=DKIM1; k=rsa; p=" + "A" * 64,
                ),
                "_dmarc.cold.example.com.": ("v=DMARC1; p=reject",),
            }
        )
        dns_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: fake_dns, local_addr=("127.0.0.1", dns_port)
        )
        dns_resources.append(dns_transport)
        # 生产 scheduler_worker 运行时环境（真实 composition，仅注入本地 DNS resolver）
        scheduler_env = {
            **env_for("http://placeholder.invalid"),
            "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
            "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
            "TRADEOS_SCHEDULER_LOCK_KEY": "3110002",
            "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "7",
            "TRADEOS_HANDOFF_T1_SECONDS": "2",
            "TRADEOS_HANDOFF_T2_SECONDS": "2",
            "TRADEOS_DKIM_SELECTOR": "s1",
            "TRADEOS_SCHEDULER_HEALTH_PORT": str(_free_port()),
        }
        yield_dict.update(
            {
                "tenant": tenant,
                "boss": boss,
                "campaign": campaign,
                "approval": approval,
                "identity": identity,
                "contact": contact,
                "account": account,
                "enrollment": enrollment.enrollment_id,
                "notification_id": notification_id,
                "factory": factory,
                "fake_gmail": fake_gmail,
                "fake_dns": fake_dns,
                "dns_port": dns_port,
                "scheduler_env": scheduler_env,
                "senders": senders,
                "app_deps": app_deps,
                "seeding_deps": seeding_deps,
                "engine": engine,
            }
        )

        # 启动 API（临时模块工厂）与 Vite
        vite_port = _free_port()
        web_origin = f"http://127.0.0.1:{vite_port}"
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        api_port = int(listener.getsockname()[1])
        api_origin = f"http://127.0.0.1:{api_port}"
        runtime_env = env_for(web_origin)

        (temporary_path / "slice4_app_entry.py").write_text(
            _SLICE4_APP_FACTORY_SOURCE, encoding="utf-8"
        )
        entry_env = {
            **runtime_env,
            "PYTHONPATH": f"{_REPO_ROOT!s}:{temporary_path!s}",
        }
        logs = {
            name: (temporary_path / name).open("wb")
            for name in ("api.stdout", "api.stderr", "vite.stdout", "vite.stderr")
        }
        try:
            api_process = _spawn_process(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "slice4_app_entry:make_app",
                    "--factory",
                    "--fd",
                    str(listener.fileno()),
                    "--no-access-log",
                ],
                cwd=_REPO_ROOT,
                env=entry_env,
                stdout=logs["api.stdout"],
                stderr=logs["api.stderr"],
                pass_fds=(listener.fileno(),),
            )
        finally:
            listener.close()
        vite_process = _spawn_process(
            [
                "npm",
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                str(vite_port),
                "--strictPort",
            ],
            cwd=_REPO_ROOT / "apps/web",
            env={
                **runtime_env,
                "VITE_API_BASE_URL": api_origin,
                "VITE_TENANT_ID": str(tenant),
                "VITE_EMPLOYEE_ID": str(boss),
            },
            stdout=logs["vite.stdout"],
            stderr=logs["vite.stderr"],
        )
        processes.extend([api_process, vite_process])
        yield_dict["api_origin"] = api_origin
        yield_dict["web_origin"] = web_origin

        await _wait_ready(
            f"{api_origin}/health/ready",
            api_process,
            headers={"X-Tenant-Id": str(tenant)},
        )
        await _wait_ready(f"{web_origin}/crm/opportunities", vite_process)
        yield yield_dict
    finally:
        for process in processes:
            if isinstance(process, subprocess.Popen) and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        fake_gmail.stop()
        for transport in dns_resources:
            transport.close()
        if engine is not None:
            await engine.dispose()
        for handle in list(logs.values()) if "logs" in dir() else []:
            handle.close()
        temporary.cleanup()
        if container_started:
            await asyncio.to_thread(container.stop)

_SLICE4_APP_FACTORY_SOURCE = r"""
import os
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.runtime import DatabaseReadinessProbe
from apps.api.runtime_config import Phase1RuntimeSettings
from connectors.gmail.transport import GmailApiHttpTransport
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.secrets import EnvironmentSecretResolver
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    SendingIdentityId,
    TenantId,
    ProspectAccountId,
)
from tool_gateway.handlers.email_send import DeliveryMaterial
from tests.outreach_fakes import FakeApprovals, FakeContacts, FakeReplies, FakeSenders, Trace

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def build_app(environ):
    settings = Phase1RuntimeSettings.from_environ(environ)
    engine = create_engine_from(settings.database_url.get_secret_value())
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    tenant = TenantId(environ["TRADEOS_TENANT_ID"])
    boss = EmployeeId(environ["TRADEOS_E2E_BOSS_ID"])
    campaign = CampaignId(environ["TRADEOS_E2E_CAMPAIGN_ID"])
    approval = ApprovalId(environ["TRADEOS_E2E_APPROVAL_ID"])
    identity = SendingIdentityId(environ["TRADEOS_E2E_IDENTITY_ID"])
    contact = ContactPointId(environ["TRADEOS_E2E_CONTACT_ID"])
    account = ProspectAccountId(environ["TRADEOS_E2E_ACCOUNT_ID"])
    trace = Trace()
    contact_snapshots = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_slice4_e2e", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    reply_snapshots = {
        (contact, account): ReplyStatusSnapshot(
            tenant, contact, account, ReplyState.NO_REPLY, None, NOW
        )
    }
    approvals = FakeApprovals(trace)
    approvals.values[(campaign, 1)] = CampaignApprovalSnapshot(
        tenant, campaign, 1, approval, CampaignApprovalState.APPROVED, boss, NOW
    )
    senders = FakeSenders(
        {
            identity: SendingIdentityEligibilitySnapshot(
                tenant, identity, OutreachSenderRole.COLD_OUTREACH, True, True, 5, NOW
            )
        },
        trace,
    )

    class Materials:
        async def resolve(self, tenant_id, preflight):
            return DeliveryMaterial(
                tenant_id=tenant_id,
                attempt_id=preflight.attempt_id,
                account_id=preflight.account_id,
                contact_point_id=preflight.contact_point_id,
                sending_identity_id=preflight.sending_identity_id,
                from_address="sales@cold.example.com",
                recipient_address="customer@example.test",
            )

    secrets = EnvironmentSecretResolver(environ)
    dependencies = build_phase1_dependencies(
        settings,
        factory,
        now=lambda: NOW,
        manual_send=ManualSendComposition(
            contact_eligibility=FakeContacts(contact_snapshots, trace),
            sending_identity_eligibility=senders,
            campaign_approvals=approvals,
            reply_status=FakeReplies(reply_snapshots, trace),
            delivery_materials=Materials(),
            secret_resolver=secrets,
            gmail_transport=GmailApiHttpTransport(environ["TRADEOS_E2E_FAKE_GMAIL_URL"]),
        ),
        secret_resolver=secrets,
    )

    from fastapi import FastAPI
    from starlette.types import Lifespan

    async def lifespan(app: FastAPI):
        del app
        try:
            await assert_database_schema_current(engine)
            yield
        finally:
            await engine.dispose()

    return create_app(
        settings=ApiSettings(
            tenant_id=settings.tenant_id,
            dev_mode=settings.dev_mode,
            retry_after_seconds=settings.retry_after_seconds,
        ),
        dependencies=dependencies,
        lifespan=lifespan,
        cors_allowed_origins=settings.cors_allowed_origins,
        readiness_probe=DatabaseReadinessProbe(engine),
    )


def make_app():
    import os

    return build_app(os.environ)
"""

@pytest.mark.e2e
@pytest.mark.asyncio(loop_scope="session")
async def test_slice4_manual_send_fixed_journey(slice4_stack: dict[str, object]) -> None:
    tenant = slice4_stack["tenant"]
    identity = slice4_stack["identity"]
    enrollment = slice4_stack["enrollment"]
    notification_id = slice4_stack["notification_id"]
    factory = slice4_stack["factory"]
    fake_gmail = slice4_stack["fake_gmail"]
    fake_dns = slice4_stack["fake_dns"]
    dns_port = slice4_stack["dns_port"]
    scheduler_env = slice4_stack["scheduler_env"]
    app_deps = slice4_stack["app_deps"]
    web_origin = slice4_stack["web_origin"]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        console_errors: list[str] = []
        page_errors: list[str] = []

        def capture(page) -> None:
            # 分开记录：application console error 与 page 未捕获异常
            page.on(
                "console",
                lambda message: (
                    console_errors.append(message.text)
                    if message.type == "error"
                    else None
                ),
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))

        async def assert_no_overflow(page) -> None:
            metrics = await page.evaluate(
                "() => ({w: document.documentElement.scrollWidth, c: document.documentElement.clientWidth})"
            )
            assert metrics["w"] <= metrics["c"], f"横向溢出 {metrics}"

        # Creative Production 视觉关卡：仅当 TRADEOS_E2E_SCREENSHOT_DIR 设置时，
        # 在三个关键交互态用同一真实栈抓取实际 Vue 实现（三页 × 两视口 = 6 张）；
        # 无该环境变量时（含 CI）不写任何文件、不改变旅程。
        screenshot_dir = os.environ.get("TRADEOS_E2E_SCREENSHOT_DIR")
        out_dir = Path(screenshot_dir) if screenshot_dir else None
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)

        async def focus_with_ring(page, locator) -> None:
            # 先产生一次键盘交互，再聚焦目标，确保 :focus-visible 命中
            # （3px solid #7c3aed 焦点环），并在截图前断言环样式确实可见。
            await page.keyboard.press("Tab")
            await locator.focus()
            ring = await page.evaluate(
                "() => {"
                "  const s = getComputedStyle(document.activeElement);"
                "  return s.outlineStyle === 'solid'"
                "    && s.outlineWidth === '3px'"
                "    && s.outlineColor === 'rgb(124, 58, 237)';"
                "}"
            )
            assert ring, "焦点环不可见（:focus-visible 未命中）"

        async def capture_visual(page, name: str) -> None:
            if out_dir is None:
                return
            # 同一 page 先 1440×900，再 1180×800，每档均断言无横向溢出
            for viewport, filename in (
                ("1440x900", f"{name}-1440x900.png"),
                ("1180x800", f"{name}-1180x800.png"),
            ):
                width, height = (int(part) for part in viewport.split("x"))
                await page.set_viewport_size({"width": width, "height": height})
                await assert_no_overflow(page)
                await page.screenshot(path=str(out_dir / filename), full_page=False)

        try:
            # 1) outreach：prepare → send once
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            capture(page)
            await page.goto(f"{web_origin}/crm/outreach", wait_until="networkidle")
            await expect(page.get_by_text(str(enrollment))).to_be_visible()
            await assert_no_overflow(page)
            # 选中入组记录后详情区才出现「准备发送」
            await page.locator('li[aria-label="入组记录（Enrollment）"]').first.click()
            await page.get_by_role("button", name="准备发送").click()
            await expect(page.get_by_text("邮件主题（Subject）")).to_be_visible()
            await page.locator("#send-subject").fill("Re: hardware sourcing needs")
            await page.locator("#send-body").fill(
                "Hello, would you be open to a short call this week?"
            )
            # 视觉态：drawer 打开、英文 subject/body 已填、subject 焦点环可见、尚未 send
            if out_dir is not None:
                await focus_with_ring(page, page.locator("#send-subject"))
                await capture_visual(page, "outreach")
                await page.set_viewport_size({"width": 1440, "height": 900})
            await page.get_by_role("button", name="发送", exact=True).click()
            await expect(page.get_by_text("发送成功")).to_be_visible(timeout=10000)
            await expect(page.locator('[role="dialog"]')).to_have_count(0)
            assert fake_gmail.send_count == 1
            async with factory() as session:
                attempt = (
                    await session.execute(
                        select(OutreachMessageAttemptRow).where(
                            OutreachMessageAttemptRow.tenant_id == str(tenant)
                        )
                    )
                ).scalars().one()
                assert (attempt.state, attempt.provider_ref) == (
                    "sent",
                    "gmail-sent-1",
                )
                calls = (
                    await session.execute(
                        select(func.count())
                        .select_from(ToolCallRow)
                        .where(ToolCallRow.tenant_id == str(tenant))
                    )
                ).scalar_one()
                assert calls == 1
            await context.close()

            # 2) identity center：auth check 提交 + 结果可见
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            capture(page)
            await page.goto(
                f"{web_origin}/crm/sending-identities", wait_until="networkidle"
            )
            await expect(page.get_by_text("cold.example.com", exact=True)).to_be_visible()
            await assert_no_overflow(page)
            # 种子认证全过；worker 经真实 DNS 查询后 UI 显示其结果
            await expect(page.get_by_text("SPF 通过")).to_be_visible()
            await page.get_by_role("button", name="重新检查认证").first.click()
            await expect(page.get_by_text("认证检查已提交")).to_be_visible()
            # 视觉态：提交反馈可见、「重新检查认证」按钮焦点环可见
            if out_dir is not None:
                await focus_with_ring(
                    page, page.get_by_role("button", name="重新检查认证").first
                )
                await capture_visual(page, "sending-identities")
                await page.set_viewport_size({"width": 1440, "height": 900})
            # 真实 scheduler/worker 消费：outbox drain → workflow → tool-gateway
            # → DNS connector → 本地 fake DNS；测试不直接写认证结果。
            from apps.scheduler_worker.main import _run_cycle
            from apps.scheduler_worker.runtime import (
                SchedulerDomainDependencies,
                SchedulerRuntimeFactory,
            )

            class _StubOpportunities:
                async def record_handoff_escalation(self, *args, **kwargs):
                    del args, kwargs

            class _StubEmployees:
                async def get_employee(self, *args, **kwargs):
                    del args, kwargs

            class _StubAudience:
                async def recipients_for(self, tenant_id, event):
                    del tenant_id, event
                    return ()

            async with SchedulerRuntimeFactory(
                scheduler_env,
                SchedulerDomainDependencies(
                    _StubOpportunities(), _StubEmployees(), _StubAudience()
                ),
                resolver_factory=lambda: _LocalDnsResolver(dns_port),
            )() as runtime:
                await _run_cycle(runtime, 1)
            # 证据一：本地 fake DNS 确实收到三个 TXT 查询（真实 connector 边界）
            assert {
                "cold.example.com.",
                "s1._domainkey.cold.example.com.",
                "_dmarc.cold.example.com.",
            } <= {name for name, _ in fake_dns.queries}, (
                f"fake DNS 查询缺失：{fake_dns.queries}"
            )
            # 证据二：tool gateway ledger 恰有一条 dns.auth.check 成功调用
            # 证据三：点击创建的认证检查请求经状态机推进到 succeeded
            async with factory() as session:
                dns_calls = (
                    await session.execute(
                        select(ToolCallRow).where(
                            ToolCallRow.tenant_id == str(tenant),
                            ToolCallRow.tool_id == "dns.auth.check",
                        )
                    )
                ).scalars().all()
                assert len(dns_calls) == 1, f"dns.auth.check 调用数异常：{len(dns_calls)}"
                assert dns_calls[0].status == "succeeded"
                requests = (
                    await session.execute(
                        select(AuthenticationCheckRequestRow).where(
                            AuthenticationCheckRequestRow.tenant_id == str(tenant)
                        )
                    )
                ).scalars().all()
                assert len(requests) == 1, f"认证检查请求数异常：{len(requests)}"
                assert requests[0].status == "succeeded"
                assert requests[0].completed_at is not None
                # 证据四：UI 渲染的认证状态来自 worker 的 DNS 结果——最新
                # auth check 记录为 dns_ 前缀（非测试直写）
                latest = (
                    await session.execute(
                        select(AuthenticationCheckRow)
                        .where(
                            AuthenticationCheckRow.tenant_id == str(tenant),
                            AuthenticationCheckRow.identity_id == str(identity),
                        )
                        .order_by(AuthenticationCheckRow.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one()
                assert latest.check_ref.startswith("dns_"), latest.check_ref
                assert latest.spf_passed and latest.dkim_passed and latest.dmarc_passed
            # 结果经真实服务落库后 UI 可见：卡片显示 DNS 派生结果（SPF 通过）
            await page.reload(wait_until="networkidle")
            await expect(page.get_by_text("SPF 通过")).to_be_visible()
            await assert_no_overflow(page)
            await context.close()

            # 3) hard bounce + complaint 注入 → suspended → retry fail-closed
            await app_deps.sending_identities.record_delivery_event(
                tenant,
                identity,
                DeliveryEventRecord(
                    tenant_id=str(tenant),
                    identity_id=str(identity),
                    event_type=DeliveryEventType.HARD_BOUNCED,
                    occurred_at=_NOW,
                    dedup_key=IdempotencyKey("b" * 64),
                    source_ref="feedback_e2e_bounce",
                ),
                actor=SendingIdentityActor(
                    "system:slice4-e2e",
                    SendingIdentityScope(
                        level=SendingIdentityScopeLevel.SYSTEM,
                        allowed_identity_ids=frozenset({identity}),
                    ),
                    "system",
                ),
            )
            await app_deps.sending_identities.record_delivery_event(
                tenant,
                identity,
                DeliveryEventRecord(
                    tenant_id=str(tenant),
                    identity_id=str(identity),
                    event_type=DeliveryEventType.COMPLAINT,
                    occurred_at=_NOW,
                    dedup_key=IdempotencyKey("c" * 64),
                    source_ref="feedback_e2e_complaint",
                ),
                actor=SendingIdentityActor(
                    "system:slice4-e2e",
                    SendingIdentityScope(
                        level=SendingIdentityScopeLevel.SYSTEM,
                        allowed_identity_ids=frozenset({identity}),
                    ),
                    "system",
                ),
            )
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()
            capture(page)
            await page.goto(f"{web_origin}/crm/outreach", wait_until="networkidle")
            await expect(page.get_by_text(str(enrollment))).to_be_visible()
            # 选中入组记录后详情区才出现「准备发送」
            await page.locator('li[aria-label="入组记录（Enrollment）"]').first.click()
            await page.get_by_role("button", name="准备发送").click()
            await expect(page.get_by_text("无法准备发送，请稍后重试")).to_be_visible(
                timeout=10000
            )
            assert fake_gmail.send_count == 1
            await context.close()

            # 4) notifications：in-app 可见 → mark read
            context = await browser.new_context(viewport={"width": 1180, "height": 800})
            page = await context.new_page()
            capture(page)
            await page.goto(f"{web_origin}/notifications", wait_until="networkidle")
            await expect(page.get_by_text(str(notification_id))).to_be_visible()
            await assert_no_overflow(page)
            row = page.locator("li", has_text=str(notification_id))
            await expect(row.locator(".unread-tag")).to_be_visible()
            await row.click()
            # 视觉态：未读通知被选中、右侧详情 + 安全 relative_link 可见、
            # 「前往处理」焦点环可见、尚未 mark-read
            if out_dir is not None:
                await expect(page.get_by_role("link", name="前往处理")).to_be_visible()
                await focus_with_ring(page, page.get_by_role("link", name="前往处理"))
                await capture_visual(page, "notifications")
                await page.set_viewport_size({"width": 1180, "height": 800})
            await page.get_by_role("button", name="标记为已读").click()
            await expect(row.locator(".read-tag")).to_be_visible(timeout=10000)
            async with factory() as session:
                row = await session.get(
                    InAppNotificationRow,
                    (str(tenant), str(notification_id)),
                )
                assert row.read_at is not None
            # 徽标：重载后未读计数归零（唯一一条通知已读）
            await page.reload(wait_until="networkidle")
            await expect(
                page.get_by_role("link", name="通知，0 条未读")
            ).to_be_visible(timeout=10000)
            await assert_no_overflow(page)
            await context.close()
        finally:
            await browser.close()

    # console 证据分开报告：
    # - page 未捕获异常必须为 0；
    # - application console error 仅允许恰一条固定行：步骤 3 故意 409
    #   （身份熔断后 prepare 被拒）触发的浏览器资源加载日志；
    #   精确 allowlist，不与其他错误混过滤。
    expected_409 = (
        "Failed to load resource: the server responded with a status of 409 (Conflict)"
    )
    assert page_errors == [], f"page 未捕获异常必须为 0：{page_errors}"
    assert console_errors == [expected_409], (
        "application console error 应仅为步骤 3 预期的 409 资源加载行："
        f"{console_errors}"
    )
