"""Slice 4 手动发送演示：真实 Postgres + 本地受控 Gmail/DNS。

旅程：真实 Sending Identity 认证 workflow（本地 fake DNS）→ 预热 → 手工
单封发送（本地 fake Gmail HTTP）→ hard bounce + complaint → 熔断 → 第二次
发送被真实身份门禁阻断 → 熔断事件经真实 scheduler 投影成通知 job →
真实 notification worker 双渠道投递（站内 + 事务邮件）。只直插 employee
与受控 connector 配置前置；不直插任何业务行。输出单条安全 JSON。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import socket
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import dns.asyncresolver
import dns.message
import dns.rdatatype
import dns.rrset
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
from apps.api.runtime_config import Phase1RuntimeSettings
from apps.scheduler_worker.notification_projection import (
    NotificationAudienceMember,
)
from connectors.gmail.transport import GmailApiHttpTransport
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.service import OpportunityService
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    MessageSendPreflight,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
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
    DomainRole,
    IdentityRegisterRequest,
)
from infra.db.repositories.employees import EmployeeRepositoryImpl
from infra.db.session import create_engine_from
from infra.db.tables import (
    AuthenticationCheckRequestRow,
    AuthenticationCheckRow,
    InAppNotificationRow,
    NotificationDeliveryRow,
    NotificationJobRow,
    ToolCallRow,
)
from shared.errors import PermissionDenied
from shared.events.catalog import DomainEvent
from shared.schemas.identifiers import (
    ApprovalId,
    ContactPointId,
    EmployeeId,
    HandoffId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.errors import ToolCallStatus
from tool_gateway.handlers.email_send import DeliveryMaterial
from tool_gateway.pipeline import ToolCallContext

_FAILURE_MESSAGE = "Slice 4 演示运行失败"
_DEMO_MODE_ENV = "TRADEOS_SLICE4_DEMO_MODE"
_DEMO_MODE_VALUE = "controlled"
_GMAIL_REF_OVERRIDE_ENV = "TRADEOS_SLICE4_GMAIL_REF"
_DNS_PORT_OVERRIDE_ENV = "TRADEOS_SLICE4_DNS_PORT"
_DEFAULT_GMAIL_REF = "GMAIL_OAUTH_TOKEN_SLICE4"
_DNS_DOMAIN = "cold.example.com"
_DNS_SELECTOR = "s1"
_DNS_NAMES = (
    f"{_DNS_DOMAIN}.",
    f"{_DNS_SELECTOR}._domainkey.{_DNS_DOMAIN}.",
    f"_dmarc.{_DNS_DOMAIN}.",
)
_SUBJECT = "demo-subject-marker"
_BODY = "demo-body-marker"
_RECIPIENT = "demo-recipient-marker@example.test"
# 发件地址必须落在登记域名内（normalize_sending_address 强制）
_SENDER = "sales@cold.example.com"


class _CountingGmailServer(ThreadingHTTPServer):
    """带类型计数属性的受控 Gmail HTTP 服务（handler 经 self.server 访问）。"""

    send_count: int = 0
    cold_send_count: int = 0
    search_count: int = 0


class _FakeGmailServer:
    """本地受控 Gmail HTTP 服务：只记发送与搜索计数，返回固定 provider ref。"""

    def __init__(self) -> None:
        self._server: _CountingGmailServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def send_count(self) -> int:
        return self._server.send_count if self._server is not None else 0

    @property
    def cold_send_count(self) -> int:
        return self._server.cold_send_count if self._server is not None else 0

    @property
    def search_count(self) -> int:
        return self._server.search_count if self._server is not None else 0

    def start(self) -> str:
        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def _json(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path
                server = cast(_CountingGmailServer, self.server)
                if path.startswith("/gmail/v1/users/me/messages?") and "q=" in path:
                    server.search_count += 1
                    self._json(200, {"messages": [], "resultSizeEstimate": 0})
                    return
                if path.startswith("/gmail/v1/users/me/messages/"):
                    self._json(404, {"error": {"code": 404}})
                    return
                if path == "/gmail/v1/users/me/profile":
                    self._json(200, {"emailAddress": "demo@example.test"})
                    return
                if path.startswith("/gmail/v1/users/me/history"):
                    self._json(200, {"history": []})
                    return
                self._json(404, {"error": {"code": 404}})

            def do_POST(self) -> None:
                if self.path == "/gmail/v1/users/me/messages/send":
                    length = int(self.headers.get("content-length", "0"))
                    payload = self.rfile.read(length)
                    server = cast(_CountingGmailServer, self.server)
                    server.send_count += 1
                    # 冷开发发送按 MIME 内的固定 subject marker 区分（内存计数，
                    # 不落库不输出）；事务通知邮件走同一端点但不带该 marker。
                    raw = ""
                    try:
                        raw = json.loads(payload).get("raw", "")
                    except ValueError:
                        raw = ""
                    try:
                        decoded = base64.urlsafe_b64decode(raw + "==")
                    except (ValueError, TypeError):
                        decoded = b""
                    if b"demo-subject-marker" in decoded:
                        server.cold_send_count += 1
                    self._json(200, {"id": f"gmail-slice4-sent-{server.send_count}"})
                    return
                self._json(404, {"error": {"code": 404}})

        self._server = _CountingGmailServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


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


class _DemoContacts:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[
            tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot
        ] = {}

    async def get_contact_eligibility(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ContactEligibilitySnapshot:
        value = self.values.get((contact_point_id, account_id))
        if tenant_id != self._tenant or value is None:
            raise PermissionDenied("演示联系人事实拒绝")
        return value


class _DemoSenders:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[SendingIdentityId, SendingIdentityEligibilitySnapshot] = {}

    async def get_sending_identity_eligibility(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> SendingIdentityEligibilitySnapshot:
        value = self.values.get(identity_id)
        if tenant_id != self._tenant or value is None:
            raise PermissionDenied("演示发件身份事实拒绝")
        return value


class _DemoApprovals:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[tuple[str, int], CampaignApprovalSnapshot] = {}

    async def get_campaign_approval(
        self, tenant_id: TenantId, campaign_id: str, version: int
    ) -> CampaignApprovalSnapshot | None:
        if tenant_id != self._tenant:
            raise PermissionDenied("演示审批事实拒绝")
        return self.values.get((campaign_id, version))


class _DemoReplies:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[
            tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot
        ] = {}

    async def get_reply_status(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ReplyStatusSnapshot:
        value = self.values.get((contact_point_id, account_id))
        if tenant_id != self._tenant or value is None:
            raise PermissionDenied("演示回复事实拒绝")
        return value


class _DemoMaterials:
    async def resolve(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> DeliveryMaterial:
        return DeliveryMaterial(
            tenant_id=tenant_id,
            attempt_id=preflight.attempt_id,
            account_id=preflight.account_id,
            contact_point_id=preflight.contact_point_id,
            sending_identity_id=preflight.sending_identity_id,
            from_address=_SENDER,
            recipient_address=_RECIPIENT,
        )


class _DemoSecrets:
    def __init__(self, environ: dict[str, str], gmail_ref: str) -> None:
        self._environ = environ
        self._gmail_ref = gmail_ref

    def resolve(self, secret_ref: str) -> str:
        if secret_ref == self._gmail_ref:
            value = self._environ.get(self._gmail_ref, "")
            if len(value) < 16:
                raise PermissionDenied("演示 Gmail 凭证引用无效")
            return value
        values = {
            "TOOL_FINGERPRINT_SLICE4": "f" * 32,
            "UNSUBSCRIBE_HMAC_SLICE4": "u" * 32,
        }
        if secret_ref not in values:
            raise PermissionDenied("演示凭证引用拒绝")
        return values[secret_ref]


class _DemoAudience:
    """scheduler 通知投影的受控受众：熔断事件只通知演示 boss。"""

    def __init__(self, tenant: TenantId, employee_id: EmployeeId) -> None:
        self._member = NotificationAudienceMember(tenant, employee_id)
        self._tenant = tenant

    async def recipients_for(
        self, tenant_id: TenantId, event: DomainEvent
    ) -> tuple[NotificationAudienceMember, ...]:
        if tenant_id != self._tenant:
            raise PermissionDenied("演示通知受众拒绝")
        # 受控受众配置：只对熔断事件通知演示 boss；ReputationThresholdBreached
        # 是同一熔断的派生信号，不重复打扰（真实部署由受众解析器按偏好路由）。
        if type(event).__name__ != "SendingIdentitySuspended":
            return ()
        return (self._member,)


class _StubOpportunities:
    """机会域窄桩：满足 SchedulerDomainDependencies 的 Protocol；演示不触发接管。"""

    async def record_handoff_escalation(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        level: int,
        escalated_at: datetime,
        *,
        actor: OpportunityActor,
    ) -> None:
        del tenant_id, handoff_id, level, escalated_at, actor
        raise NotImplementedError("演示不触发人工接管")


class _StubEmployees:
    """员工读取窄桩：满足 HumanHandoffEmployeeReader；演示不查询员工。"""

    async def get_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: EmployeeActor,
    ) -> EmployeeView:
        del tenant_id, employee_id, actor
        raise NotImplementedError("演示不提供员工查询")


def _settings(tenant: TenantId, fake_gmail_url: str) -> Phase1RuntimeSettings:
    return Phase1RuntimeSettings.from_environ(
        {
            "DATABASE_URL": "postgresql+asyncpg://unused.invalid/tradeos",
            "TRADEOS_TENANT_ID": str(tenant),
            "TRADEOS_DEV_MODE": "true",
            "TRADEOS_CORS_ALLOWED_ORIGINS": '["http://127.0.0.1:4173"]',
            "TRADEOS_API_RETRY_AFTER_SECONDS": "30",
            "TRADEOS_HANDOFF_POLICY": (
                '{"sla_seconds":300,"backlog_threshold":20,'
                '"t1_seconds":120,"t2_seconds":180}'
            ),
            "TRADEOS_SCORING_POLICY": (
                '{"version":"phase1-v1","currency":"USD",'
                '"value_band_boundaries":["1000","5000"],'
                '"bucket_map":{"1":"low","2":"low","3":"mid",'
                '"4":"mid","5":"high","6":"high","7":"high"}}'
            ),
            "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
            "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_SLICE4",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_SLICE4",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
                '{"2026-v1":"UNSUBSCRIBE_HMAC_SLICE4"}'
            ),
            "TRADEOS_TOOL_LEASE_SECONDS": "60",
            "TRADEOS_E2E_FAKE_GMAIL_URL": fake_gmail_url,
        }
    )


def _outreach_boss(employee_id: EmployeeId) -> OutreachActor:
    return OutreachActor(
        str(employee_id),
        OutreachScope(level=OutreachScopeLevel.TENANT),
        "boss",
    )


def _sending_boss(employee_id: EmployeeId) -> SendingIdentityActor:
    return SendingIdentityActor(
        str(employee_id),
        SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
        "boss",
    )


def _sending_system(identity_id: SendingIdentityId) -> SendingIdentityActor:
    return SendingIdentityActor(
        "system:slice4-demo",
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _seed_employee(
    factory: async_sessionmaker, tenant: TenantId, employee_id: EmployeeId
) -> None:
    models = __import__("domains.employees.models", fromlist=["Employee", "Role"])
    async with factory() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee_id=employee_id,
                tenant_id=tenant,
                user_id=UserId(str(employee_id)),
                name="Slice 4 Demo Operator",
                role=models.Role.BOSS,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def _scheduler_cycle(
    scheduler_env: dict[str, str],
    dependencies: object,
    audience: _DemoAudience,
    dns_port: int,
) -> None:
    from apps.scheduler_worker.main import _run_cycle
    from apps.scheduler_worker.runtime import (
        SchedulerDomainDependencies,
        SchedulerRuntimeFactory,
    )

    # _StubOpportunities 是演示专用的窄桩（演示路径不触发人工接管，方法
    # 永不被调用）；OpportunityService Protocol 方法众多，此处用最小精确
    # cast 满足组合构造，不实现整份 Protocol。
    async with SchedulerRuntimeFactory(
        scheduler_env,
        SchedulerDomainDependencies(
            cast(OpportunityService, _StubOpportunities()),
            _StubEmployees(),
            audience,
        ),
        resolver_factory=lambda: _LocalDnsResolver(dns_port),
    )() as runtime:
        await _run_cycle(runtime, 1)


def _scheduler_env(
    database_url: str, tenant: TenantId
) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": str(tenant),
        "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
        "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
        "TRADEOS_SCHEDULER_LOCK_KEY": "3110003",
        "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "7",
        "TRADEOS_HANDOFF_T1_SECONDS": "2",
        "TRADEOS_HANDOFF_T2_SECONDS": "2",
        "TRADEOS_DKIM_SELECTOR": _DNS_SELECTOR,
        "TRADEOS_SCHEDULER_HEALTH_PORT": str(_free_port()),
        "TRADEOS_TOOL_LEASE_SECONDS": "60",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_SLICE4",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TOOL_FINGERPRINT_SLICE4": "f" * 32,
        "GMAIL_OAUTH_TOKEN_SLICE4": "g" * 32,
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_SLICE4",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "route-slice4-scheduler",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": '{"2026-v1":"UNSUBSCRIBE_HMAC_2026"}',
        "UNSUBSCRIBE_HMAC_2026": "u" * 32,
        "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "30",
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
    }


async def _dispatch_notifications(
    worker_env: dict[str, str], fake_gmail_url: str
) -> None:
    from apps.notification_worker.config import NotificationWorkerConfig
    from apps.notification_worker.runtime import (
        notification_worker_runtime,
        run_notification_worker,
    )

    async def _once(interval: float, stop_event: asyncio.Event) -> None:
        del interval
        stop_event.set()

    async with notification_worker_runtime(
        NotificationWorkerConfig.from_environ(worker_env),
        transport_factory=lambda url: GmailApiHttpTransport(url),
    ) as worker:
        result = await run_notification_worker(worker, wait=_once)
        if result.cycles_completed != 1:
            raise RuntimeError("通知投递未完成")


def _worker_env(
    database_url: str,
    tenant: TenantId,
    boss: EmployeeId,
    transactional_identity: SendingIdentityId,
    fake_gmail_url: str,
) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": str(tenant),
        "TRADEOS_DEV_MODE": "true",
        "TRADEOS_NOTIFICATION_POLL_INTERVAL_SECONDS": "5",
        "TRADEOS_NOTIFICATION_BATCH_LIMIT": "10",
        "TRADEOS_NOTIFICATION_HEALTH_PORT": str(_free_port()),
        "TRADEOS_NOTIFICATION_LEASE_OWNER": "slice4-demo",
        "TRADEOS_NOTIFICATION_GMAIL_BASE_URL": fake_gmail_url,
        "TRADEOS_NOTIFICATION_SENDING_IDENTITY_ID": str(transactional_identity),
        "TRADEOS_NOTIFICATION_RECIPIENTS_JSON": json.dumps(
            [
                {
                    "tenant_id": str(tenant),
                    "employee_id": str(boss),
                    "address": "boss-notify-marker@example.test",
                }
            ]
        ),
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_SLICE4",
        "GMAIL_OAUTH_TOKEN_SLICE4": "g" * 32,
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_FINGERPRINT_SLICE4",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TOOL_FINGERPRINT_SLICE4": "f" * 32,
        "TRADEOS_TOOL_LEASE_SECONDS": "60",
    }


async def _exercise(database_url: str) -> dict[str, object]:
    environ = dict(os.environ)
    # 默认受控凭证值：显式覆盖引用缺失时才判为无效配置
    environ.setdefault(_DEFAULT_GMAIL_REF, "g" * 32)
    gmail_ref = environ.get(_GMAIL_REF_OVERRIDE_ENV, _DEFAULT_GMAIL_REF)
    if gmail_ref != _DEFAULT_GMAIL_REF and len(environ.get(gmail_ref, "")) < 16:
        raise RuntimeError("Gmail OAuth 配置无效")

    fake_gmail = _FakeGmailServer()
    fake_gmail_url = fake_gmail.start()
    dns_records: dict[str, tuple[str, ...]] = {
        f"{_DNS_DOMAIN}.": ("v=spf1 -all",),
        f"{_DNS_SELECTOR}._domainkey.{_DNS_DOMAIN}.": (
            "v=DKIM1; k=rsa; p=" + "A" * 64,
        ),
        f"_dmarc.{_DNS_DOMAIN}.": ("v=DMARC1; p=reject",),
    }
    fake_dns = _FakeDnsProtocol(dns_records)
    dns_port = int(environ.get(_DNS_PORT_OVERRIDE_ENV, "0"))
    if dns_port == 0:
        dns_port = _free_port()
        dns_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: fake_dns, local_addr=("127.0.0.1", dns_port)
        )
    else:
        dns_transport = None

    tenant = TenantId(new_id("tn"))
    employee_id = EmployeeId(new_id("emp"))
    contacts = _DemoContacts(tenant)
    senders = _DemoSenders(tenant)
    approvals = _DemoApprovals(tenant)
    replies = _DemoReplies(tenant)
    materials = _DemoMaterials()
    secrets = _DemoSecrets(environ, gmail_ref)
    engine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_employee(factory, tenant, employee_id)
        dependencies = build_phase1_dependencies(
            _settings(tenant, fake_gmail_url),
            factory,
            now=lambda: datetime.now(UTC),
            manual_send=ManualSendComposition(
                contact_eligibility=contacts,
                sending_identity_eligibility=senders,
                campaign_approvals=approvals,
                reply_status=replies,
                delivery_materials=materials,
                secret_resolver=secrets,
                gmail_transport=GmailApiHttpTransport(fake_gmail_url),
            ),
            secret_resolver=secrets,
        )
        scheduler_env = _scheduler_env(database_url, tenant)
        audience = _DemoAudience(tenant, employee_id)

        # 1) 冷开发身份：登记 → 认证请求 → 真实 scheduler/workflow/DNS
        identity_id = await dependencies.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address=_SENDER,
                domain=_DNS_DOMAIN,
                role=DomainRole.COLD_OUTREACH,
                connector_ref="gmail_slice4_demo",
            ),
            actor=_sending_boss(employee_id),
        )
        await dependencies.sending_identities.begin_authentication(
            tenant, identity_id, actor=_sending_boss(employee_id)
        )
        auth_request = await dependencies.sending_identities.request_authentication_check(
            tenant,
            identity_id,
            IdempotencyKey(f"slice4-demo-auth-{tenant}"),
            actor=_sending_boss(employee_id),
        )
        await _scheduler_cycle(scheduler_env, dependencies, audience, dns_port)
        async with factory() as session:
            auth_rows = (
                await session.execute(
                    select(AuthenticationCheckRequestRow).where(
                        AuthenticationCheckRequestRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            if (
                len(auth_rows) != 1
                or auth_rows[0].status != "succeeded"
                or auth_rows[0].request_id != str(auth_request.request_id)
            ):
                raise RuntimeError("认证检查未被真实 worker 消费")
        dns_queries = [name for name, _ in fake_dns.queries]
        if set(dns_queries) != set(_DNS_NAMES):
            raise RuntimeError(f"DNS 查询不符合预期：{dns_queries}")
        await dependencies.sending_identities.start_warmup(
            tenant, identity_id, 5, actor=_sending_boss(employee_id)
        )
        senders.values[identity_id] = SendingIdentityEligibilitySnapshot(
            tenant,
            identity_id,
            OutreachSenderRole.COLD_OUTREACH,
            True,
            True,
            5,
            datetime.now(UTC),
        )

        # 2) 事务通知身份（独立域，角色 transactional）
        transactional_identity = await dependencies.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address="notify@notify.example.com",
                domain="notify.example.com",
                role=DomainRole.TRANSACTIONAL,
                connector_ref="gmail_slice4_demo",
            ),
            actor=_sending_boss(employee_id),
        )
        await dependencies.sending_identities.begin_authentication(
            tenant, transactional_identity, actor=_sending_boss(employee_id)
        )
        await dependencies.sending_identities.record_authentication_result(
            tenant,
            transactional_identity,
            AuthenticationResult(
                checked_at=datetime.now(UTC),
                spf_passed=True,
                dkim_passed=True,
                dmarc_passed=True,
                failures=(),
                check_ref="slice4_demo_transactional",
            ),
            actor=_sending_system(transactional_identity),
        )
        await dependencies.sending_identities.start_warmup(
            tenant, transactional_identity, 5, actor=_sending_boss(employee_id)
        )

        # 3) Campaign + 入组 + 真实网关单封发送
        boss = _outreach_boss(employee_id)
        campaign = await dependencies.outreach.create_campaign(
            tenant,
            CampaignCreateRequest(
                name="Slice 4 controlled demo",
                markets=("US",),
                target_entity_types=("importer",),
                allowed_categories=("hardware",),
                sender_identity_ids=(identity_id,),
                steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
                daily_new_contact_limit=5,
                daily_total_message_limit=5,
                handoff_triggers=(),
            ),
            actor=boss,
        )
        await dependencies.outreach.submit_campaign(
            tenant, campaign.campaign_id, actor=boss
        )
        approval = CampaignApprovalSnapshot(
            tenant,
            campaign.campaign_id,
            1,
            ApprovalId(new_id("apr")),
            CampaignApprovalState.APPROVED,
            employee_id,
            datetime.now(UTC),
        )
        approvals.values[(str(campaign.campaign_id), 1)] = approval
        await dependencies.outreach.activate_campaign(
            tenant, campaign.campaign_id, actor=boss
        )

        async def make_enrollment(index: int) -> str:
            account_id = ProspectAccountId(new_id("acc"))
            contact_id = ContactPointId(new_id("cp"))
            contacts.values[(contact_id, account_id)] = ContactEligibilitySnapshot(
                tenant,
                contact_id,
                account_id,
                ContactVerificationStatus.VERIFIED,
                datetime.now(UTC),
                ContactLegalBasis.LEGITIMATE_INTEREST,
                f"slice4_demo_basis_{index}",
                True,
                "US",
                "importer",
                frozenset({"hardware"}),
                datetime.now(UTC),
            )
            replies.values[(contact_id, account_id)] = ReplyStatusSnapshot(
                tenant,
                contact_id,
                account_id,
                ReplyState.NO_REPLY,
                None,
                datetime.now(UTC),
            )
            enrollment = await dependencies.outreach.enroll(
                tenant,
                campaign.campaign_id,
                EnrollmentCreateRequest(
                    account_id,
                    contact_id,
                    IdempotencyKey(f"slice4-demo-enrollment-{index}"),
                ),
                actor=boss,
            )
            system = OutreachActor(
                "system:slice4-demo",
                OutreachScope(
                    level=OutreachScopeLevel.SYSTEM,
                    allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
                ),
                "system",
            )
            attempt = await dependencies.outreach.prepare_message_attempt(
                tenant, enrollment.enrollment_id, actor=system
            )
            return str(attempt.attempt_id)

        def context(attempt_id: str) -> ToolCallContext:
            return ToolCallContext(
                tenant_id=tenant,
                user_id=UserId(str(employee_id)),
                tool_id="email.send",
                params={
                    "attempt_id": attempt_id,
                    "subject": _SUBJECT,
                    "body": _BODY,
                },
            )

        first_attempt = await make_enrollment(1)
        first = await dependencies.tool_gateway.invoke(context(first_attempt))
        if first.status is not ToolCallStatus.SUCCEEDED or first.tool_call_id is None:
            raise RuntimeError("首次发送未成功")

        # 4) hard bounce + complaint → 熔断
        await dependencies.sending_identities.record_delivery_event(
            tenant,
            identity_id,
            DeliveryEventRecord(
                tenant_id=tenant,
                identity_id=identity_id,
                event_type=DeliveryEventType.HARD_BOUNCED,
                occurred_at=datetime.now(UTC),
                dedup_key=IdempotencyKey("b" * 64),
                source_ref="slice4_demo_bounce",
            ),
            actor=_sending_system(identity_id),
        )
        await dependencies.sending_identities.record_delivery_event(
            tenant,
            identity_id,
            DeliveryEventRecord(
                tenant_id=tenant,
                identity_id=identity_id,
                event_type=DeliveryEventType.COMPLAINT,
                occurred_at=datetime.now(UTC),
                dedup_key=IdempotencyKey("c" * 64),
                source_ref="slice4_demo_complaint",
            ),
            actor=_sending_system(identity_id),
        )
        # 速率型熔断需 50+ 样本窗口；spam trap 命中不要求样本数、立即熔断，
        # 是唯一能在「每 run 恰一次发送」约束下产生真实 suspended 的路径。
        await dependencies.sending_identities.record_delivery_event(
            tenant,
            identity_id,
            DeliveryEventRecord(
                tenant_id=tenant,
                identity_id=identity_id,
                event_type=DeliveryEventType.SPAM_TRAP,
                occurred_at=datetime.now(UTC),
                dedup_key=IdempotencyKey("t" * 64),
                source_ref="slice4_demo_spam_trap",
            ),
            actor=_sending_system(identity_id),
        )

        # 5) 第二次发送：真实身份门禁阻断（reserve_send_slot → suspended）
        second_attempt = await make_enrollment(2)
        second = await dependencies.tool_gateway.invoke(context(second_attempt))
        if second.tool_call_id is None:
            raise RuntimeError("第二次发送无工具调用记录")
        if second.status is not ToolCallStatus.FAILED_PERMANENT:
            raise RuntimeError("第二次发送未被真实门禁阻断")
        if second.error_category is None or second.error_category.value != "provider_permanent":
            raise RuntimeError("第二次发送拒绝分类不符")

        # 6) 熔断事件 → 真实 scheduler 投影 → 通知 job
        await _scheduler_cycle(scheduler_env, dependencies, audience, dns_port)

        # 7) 真实 notification worker 双渠道投递
        await _dispatch_notifications(
            _worker_env(
                database_url, tenant, employee_id, transactional_identity, fake_gmail_url
            ),
            fake_gmail_url,
        )

        # 8) 汇总与不变量
        async with factory() as session:
            identity_view = await dependencies.sending_identities.get(
                tenant, identity_id, actor=_sending_boss(employee_id)
            )
            auth_rows = (
                await session.execute(
                    select(AuthenticationCheckRequestRow).where(
                        AuthenticationCheckRequestRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            auth_check_rows = (
                await session.execute(
                    select(AuthenticationCheckRow).where(
                        AuthenticationCheckRow.tenant_id == str(tenant),
                        AuthenticationCheckRow.identity_id == str(identity_id),
                    )
                )
            ).scalars().all()
            tool_rows = (
                await session.execute(
                    select(ToolCallRow).where(ToolCallRow.tenant_id == str(tenant))
                )
            ).scalars().all()
            inbox_count = (
                await session.execute(
                    select(func.count())
                    .select_from(InAppNotificationRow)
                    .where(InAppNotificationRow.tenant_id == str(tenant))
                )
            ).scalar_one()
            jobs = (
                await session.execute(
                    select(NotificationJobRow).where(
                        NotificationJobRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            deliveries = (
                await session.execute(
                    select(NotificationDeliveryRow).where(
                        NotificationDeliveryRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()

        if fake_gmail.cold_send_count != 1 or fake_gmail.send_count != 2:
            raise RuntimeError("发送次数不符")
        if identity_view.state != "suspended":
            raise RuntimeError("身份未进入熔断状态")
        if len(auth_check_rows) != 1 or not auth_check_rows[0].check_ref.startswith(
            "dns_"
        ):
            raise RuntimeError("认证事实非 DNS 派生")
        dns_tool_ids = [
            row.tool_call_id
            for row in tool_rows
            if row.tool_id == "dns.auth.check" and row.status == "succeeded"
        ]
        email_tool_ids = [
            row.tool_call_id
            for row in tool_rows
            if row.tool_id == "notification.email.send"
            and row.status == "succeeded"
        ]
        if len(dns_tool_ids) != 1 or len(email_tool_ids) != 1:
            raise RuntimeError("工具账本记录不符")
        if len(jobs) != 1 or jobs[0].status != "completed":
            raise RuntimeError("通知 job 状态不符")
        if int(inbox_count or 0) != 1:
            raise RuntimeError("站内通知数量不符")
        if len(deliveries) != 2 or {row.status for row in deliveries} != {
            "delivered"
        } or {row.channel_name for row in deliveries} != {"in_app", "email"}:
            raise RuntimeError("通知投递状态不符")
        delivery_by_channel = {row.channel_name: row.status for row in deliveries}
        if first.tool_call_id is None:
            raise RuntimeError("首次发送无工具调用 ID")

        return {
            "auth_check_ref_prefix": auth_check_rows[0].check_ref[:4],
            "auth_request_id": str(auth_request.request_id),
            "auth_request_status": auth_rows[0].status,
            "complaint_recorded": True,
            "dns_queries": dns_queries,
            "email_delivery_status": delivery_by_channel["email"],
            "gmail_search_count": fake_gmail.search_count,
            "hard_bounce_recorded": True,
            "identity_id": str(identity_id),
            "identity_state": identity_view.state,
            "in_app_delivery_status": delivery_by_channel["in_app"],
            "inbox_notification_count": int(inbox_count or 0),
            "provider_send_count": fake_gmail.cold_send_count,
            "second_send_blocked": True,
            "spam_trap_recorded": True,
            "second_send_category": "provider_permanent",
            "send_status": "succeeded",
            "tenant_id": str(tenant),
            "tool_call_ids": [
                first.tool_call_id,
                dns_tool_ids[0],
                email_tool_ids[0],
            ],
        }
    finally:
        if dns_transport is not None:
            dns_transport.close()
        fake_gmail.stop()
        await engine.dispose()


def main() -> int:
    try:
        if os.environ.get(_DEMO_MODE_ENV) != _DEMO_MODE_VALUE:
            raise RuntimeError("Slice 4 演示模式未启用")
        database_url = os.environ["DATABASE_URL"]
        logging.getLogger().handlers.clear()
        logging.getLogger().addHandler(logging.NullHandler())
        summary = asyncio.run(_exercise(database_url))
    except Exception:  # noqa: BLE001 - 进程边界只能输出固定脱敏消息
        print(_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
