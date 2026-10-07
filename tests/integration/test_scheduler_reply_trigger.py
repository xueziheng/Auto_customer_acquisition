"""scheduler 消费 InboundMessageStored → 启动 reply_qualification 的生产接线验收。

TDD RED：``reply_events`` / ``ReplyQualificationComposition`` / runtime 接线尚不存在，
本文件导入或构造即失败。

契约（docs/superpowers/plans/2026-08-16-reply-qualification-production-trigger.md）：
- handler 先校验 ``event.tenant_id == self._tenant_id``；``outbound_message_id`` 非 None；
  消息行存在且 ``direction == "inbound"``；``row.outbound_message_id`` 非空且与事件
  完全一致；attempt 按 ``deterministic_message_id`` 精确匹配；enrollment 可取
  account/contact。任一不满足 fail-closed（不起 run、零副作用）。
- 启动 context 只含五个 typed ID：message_id / outbound_message_id / enrollment_id /
  account_id / contact_point_id。
- reply 业务链全部真实：ArtifactMessageContentReader（真实 PG+MinIO email_raw）、
  CredentialMarkerGuard、真实 ConversationServiceImpl / OutreachServiceImpl /
  真实 artifact store；模型调用允许 fake ReplyModelPort；runtime 外围
  （DNS resolver/health/Gmail transport）用受控测试适配器。
- 幂等：同一事件重复投递 → 恰一条 run/一次分类/一条抑制/一条 ReplyReceived。
- ReplyReceived 是分类落库后的结果事件，不回触新 reply run。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.qualification_agent.agent import QualificationAgent
from domains.conversations.service import ConversationService
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
    Phase1OutreachAuthorizer,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
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
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    Phase1SendingIdentityAuthorizer,
    SendingIdentityScope,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.permissions import (
    StandardAuditLogger as SendingIdentityStandardAuditLogger,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    IdentityRegisterRequest,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.db.outbox import PostgresEventBus
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
from infra.pilot.resources import PILOT_MINIO_IMAGE
from shared.events.catalog import InboundMessageStored
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    IdempotencyKey,
    MessageId,
    OutboundMessageId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.outreach_fakes import (
    FakeApprovals,
    FakeAudit,
    FakeContacts,
    FakeReplies,
    FakeSenders,
    Trace,
)
from tests.runtime_database_fixtures import RuntimeDatabaseFactory
from tests.runtime_database_fixtures import (
    runtime_database_url as runtime_database_url,  # noqa: PLC0414 - pytest fixture
)
from tool_gateway.handlers.email_send import DeliveryMaterial

_models = importlib.import_module("domains.outreach.models")
_identity_models = importlib.import_module("domains.sending_identity.models")
Campaign = _models.Campaign
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
APPROVER = EmployeeId(new_id("emp"))
#: 正文 marker：不得出现在 workflow context / outbox payload。
BODY_MARKER = "Please unsubscribe me from your emails. UNSUBSCRIBE-MARKER-77"

#: 测试外围真实 secret marker（_Secrets/_environ 中 fingerprint/gmail/
#: unsubscribe 密钥的测试值）：不得泄漏进任何 outbox payload / run context /
#: last_error。绝不放进邮件正文或模型输入。
_SECRET_FINGERPRINT = "f" * 32
_SECRET_GMAIL = "g" * 32
_SECRET_UNSUBSCRIBE = "u" * 32
_SECRET_MARKERS = (
    _SECRET_FINGERPRINT,
    _SECRET_GMAIL,
    _SECRET_UNSUBSCRIBE,
)

#: InboundMessageStored 事件契约键（最小披露，禁止多余字段）
_INBOUND_EVENT_KEYS = {
    "tenant_id",
    "occurred_at",
    "run_id",
    "message_id",
    "outbound_message_id",
}
#: reply run context 契约键（五个 typed ID，无 category/正文/artifact 引用）
_REPLY_CONTEXT_KEYS = {
    "message_id",
    "outbound_message_id",
    "enrollment_id",
    "account_id",
    "contact_point_id",
}


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "SCHEDULER_FINGERPRINT_KEY": _SECRET_FINGERPRINT,
            "GMAIL_OAUTH_TOKEN_REF": _SECRET_GMAIL,
            "UNSUBSCRIBE_HMAC_CURRENT": _SECRET_UNSUBSCRIBE,
        }
        return values[secret_ref]


class _Transport:
    """受控 Gmail 传输：记录发送请求，不打开网络。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def search(self, **kwargs: object) -> None:
        del kwargs

    async def send(self, **kwargs: object) -> str:
        del kwargs
        ref = f"gmail-reply-trigger-{len(self.sent) + 1}"
        self.sent.append(ref)
        return ref


class _Materials:
    async def resolve(
        self, tenant_id: TenantId, preflight: object
    ) -> DeliveryMaterial:
        return DeliveryMaterial(
            tenant_id=tenant_id,
            attempt_id=preflight.attempt_id,
            account_id=preflight.account_id,
            contact_point_id=preflight.contact_point_id,
            sending_identity_id=preflight.sending_identity_id,
            from_address="scheduler-sender@example.test",
            recipient_address="scheduler-recipient@example.test",
        )


class _Opportunity:
    async def record_handoff_escalation(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


class _Employees:
    async def get_employee(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


class _Audience:
    async def recipients_for(self, tenant_id: TenantId, event: object) -> tuple:
        del tenant_id, event
        return ()


class _Resolver:
    async def resolve(self, name: str, rdtype: str) -> None:
        del name, rdtype
        raise AssertionError("composition 不得触发真实 DNS")


class _HealthServer:
    def __init__(self, state: object, port: int) -> None:
        self.state = state
        self.port = port
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def serve(self) -> None:
        self.started.set()
        await self.closed.wait()

    async def wait_started(self) -> None:
        await self.started.wait()

    async def close(self) -> None:
        self.closed.set()


class _FakeModelPort:
    """模型调用端口的唯一测试替身（ReplyModelPort，固定返回指定类别）。"""

    def __init__(self, category: str) -> None:
        self._category = category
        self.calls = 0

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        del system_prompt, message
        self.calls += 1
        return json.dumps({"category": self._category, "candidate_fields": []})


@dataclass(frozen=True)
class _MinioRuntime:
    settings: Any
    secrets: Any


class _MinioSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, secret_ref: str) -> str:
        return self._values[secret_ref]


@pytest.fixture(scope="module")
def minio_runtime() -> Iterator[_MinioRuntime]:
    import secrets as stdlib_secrets
    import time

    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    from connectors.object_store.s3 import S3ObjectStoreSettings

    access = f"access{stdlib_secrets.token_hex(12)}"
    secret = f"secret{stdlib_secrets.token_urlsafe(24)}"
    bucket = f"artifacts-{stdlib_secrets.token_hex(8)}"
    container = (
        DockerContainer(PILOT_MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", access)
        .with_env("MINIO_ROOT_PASSWORD", secret)
        .with_command("server /bitnami/minio/data --address :9000 --console-address 127.0.0.1:9001")
        .with_exposed_ports(9000)
    )
    client = None
    try:
        container.start()
        endpoint = f"http://127.0.0.1:{container.get_exposed_port(9000)}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"},
                proxies={}, connect_timeout=2, read_timeout=2,
                retries={"max_attempts": 0},
            ),
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                client.create_bucket(Bucket=bucket)
                break
            except (BotoCoreError, ClientError):
                if time.monotonic() >= deadline:
                    pytest.fail("MinIO 未在限定时间内就绪")
                time.sleep(0.2)
        settings = S3ObjectStoreSettings(
            True,
            endpoint,
            bucket,
            "TEST_MINIO_ACCESS",
            "TEST_MINIO_SECRET",
            "us-east-1",
            1024 * 1024,
            1024 * 1024,
        )
        yield _MinioRuntime(
            settings,
            _MinioSecrets({"TEST_MINIO_ACCESS": access, "TEST_MINIO_SECRET": secret}),
        )
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            container.stop()


@pytest_asyncio.fixture
async def scheduler_reply_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


RuntimeEngineFactory = Callable[[str], Awaitable[tuple[AsyncEngine, str]]]


@pytest_asyncio.fixture
async def scheduler_reply_runtime(
    runtime_database_url: RuntimeDatabaseFactory,
) -> AsyncIterator[RuntimeEngineFactory]:
    """每个注入业务依赖也绑定真实企业角色，连接池先于角色回收。"""
    engines: list[AsyncEngine] = []

    async def provision(tenant: str) -> tuple[AsyncEngine, str]:
        database_url = await runtime_database_url(tenant)
        engine = create_engine_from(database_url)
        engines.append(engine)
        return engine, database_url

    try:
        yield provision
    finally:
        for engine in reversed(engines):
            await engine.dispose()


def _environ(db_url: str, tenant: TenantId) -> dict[str, str]:
    return {
        "DATABASE_URL": db_url,
        "TRADEOS_TENANT_ID": str(tenant),
        "TRADEOS_SCHEDULER_INTERVAL_SECONDS": "5",
        "TRADEOS_SCHEDULER_BATCH_LIMIT": "20",
        "TRADEOS_SCHEDULER_LOCK_KEY": "3111001",
        "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS": "3",
        "TRADEOS_HANDOFF_T1_SECONDS": "3600",
        "TRADEOS_HANDOFF_T2_SECONDS": "7200",
        "TRADEOS_DKIM_SELECTOR": "s1",
        "TRADEOS_SCHEDULER_HEALTH_PORT": "8094",
        "TRADEOS_TOOL_LEASE_SECONDS": "120",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "SCHEDULER_FINGERPRINT_KEY",
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
        "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS": "1",
        "GMAIL_OAUTH_TOKEN_REF": "GMAIL_OAUTH_TOKEN_REF",
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "route-scheduler-test",
        "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsub.example",
        "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "k1",
        "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": '{"k1": "UNSUBSCRIBE_HMAC_CURRENT"}',
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "false",
        "SCHEDULER_FINGERPRINT_KEY": "f" * 32,
        "UNSUBSCRIBE_HMAC_CURRENT": "u" * 32,
    }


async def _seed_identity(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    boss: EmployeeId,
) -> SendingIdentityId:
    sending = SendingIdentityServiceImpl(
        lambda requested: SqlAlchemySendingIdentityUnitOfWork(
            factory, requested, now=lambda: NOW
        ),
        Phase1SendingIdentityAuthorizer(tenant),
        SendingIdentityStandardAuditLogger(),
        now=lambda: NOW,
    )
    boss_identity = SendingIdentityActor(
        str(boss), SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT), "boss"
    )
    identity_id = await sending.register(
        tenant,
        IdentityRegisterRequest(
            address="scheduler-sender@example.test",
            domain="example.test",
            role=_identity_models.DomainRole.COLD_OUTREACH,
        ),
        actor=boss_identity,
    )
    await sending.begin_authentication(tenant, identity_id, actor=boss_identity)
    await sending.record_authentication_result(
        tenant,
        identity_id,
        AuthenticationResult(
            checked_at=NOW,
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="auth_scheduler_1",
        ),
        actor=SendingIdentityActor(
            "system:scheduler-test",
            SendingIdentityScope(
                level=SendingIdentityScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({identity_id}),
            ),
            "system",
        ),
    )
    await sending.start_warmup(tenant, identity_id, 5, actor=boss_identity)
    return identity_id


async def _seed_campaign(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    sender: SendingIdentityId,
    approval_id: ApprovalId,
) -> None:
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    # 真实两步 campaign：DISCOVERY(wait 0) → FOLLOW_UP(wait 1)。首封发出后
    # wait_for_reply 按下一步 wait_days(1 天) 超时，时钟只推进秒级 →
    # enrollment 保持 in_sequence（与 campaign driver harness 一致）。
    boundary = CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(sender,),
        steps=(
            SequenceStepSpec(1, StepIntent.DISCOVERY, 0),
            SequenceStepSpec(2, StepIntent.FOLLOW_UP, 1),
        ),
        daily_new_contact_limit=5,
        daily_total_message_limit=7,
        handoff_triggers=(),
    )
    campaign = Campaign(
        tenant, campaign_id, CampaignState.ACTIVE, 1, APPROVER, NOW,
        approval_id=str(approval_id), approved_by=APPROVER, approved_at=NOW,
    )
    version = CampaignVersion(tenant, campaign_id, 1, "Reply trigger discovery", boundary, APPROVER, NOW)
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


def _test_service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender: SendingIdentityId,
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    senders: FakeSenders,
    clock: MutableClock,
):
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant, campaign_id, 1, approval_id, CampaignApprovalState.APPROVED, APPROVER, NOW
    )
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    service_type = importlib.import_module("domains.outreach.service_impl").OutreachServiceImpl
    return service_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        FakeContacts(contacts, trace),
        senders,
        approvals,
        FakeReplies(replies, trace),
        Phase1OutreachAuthorizer(tenant),
        FakeAudit(trace),
        now=clock.now,
    )


def _boss() -> OutreachActor:
    return OutreachActor(
        "boss:scheduler-test", OutreachScope(level=OutreachScopeLevel.TENANT), "boss"
    )


async def _enroll(
    service: object,
    tenant: TenantId,
    campaign_id: CampaignId,
    contact: ContactPointId,
    account: ProspectAccountId,
    key: str,
) -> object:
    return await service.enroll(  # type: ignore[attr-defined]
        tenant,
        campaign_id,
        EnrollmentCreateRequest(account, contact, IdempotencyKey(key)),
        actor=_boss(),
    )


def _senders(
    tenant: TenantId, sender: SendingIdentityId, *, sendable: bool = True
) -> FakeSenders:
    trace = Trace()
    return FakeSenders(
        {
            sender: SendingIdentityEligibilitySnapshot(
                tenant, sender, OutreachSenderRole.COLD_OUTREACH, sendable, True, 100, NOW
            )
        },
        trace,
    )


def _composition(
    transport: _Transport,
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender: SendingIdentityId,
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    senders: FakeSenders,
):
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant, campaign_id, 1, approval_id, CampaignApprovalState.APPROVED, APPROVER, NOW
    )
    from apps.scheduler_worker.runtime import CampaignMessagingComposition

    return CampaignMessagingComposition(
        contact_eligibility=FakeContacts(contacts, trace),
        sending_identity_eligibility=senders,
        campaign_approvals=approvals,
        reply_status=FakeReplies(replies, trace),
        delivery_materials=_Materials(),
        secret_resolver=_Secrets(),
        gmail_transport=transport,
    )


def _conversations_service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    clock: MutableClock,
) -> ConversationService:
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    impl_type = importlib.import_module(
        "domains.conversations.service_impl"
    ).ConversationServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


def _raw_stores(engine: AsyncEngine, runtime: _MinioRuntime):
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.object_store.bounded import S3BoundedObjectBlobTransport
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from shared.schemas.evidence_read import ObjectReadLimits

    factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = lambda tenant: SqlAlchemyArtifactUnitOfWork(factory, tenant)
    transport = S3ObjectBlobTransport(runtime.settings, runtime.secrets)
    return RawArtifactStoreImpl(
        uow_factory,
        transport,
        runtime.settings.raw_max_bytes,
        lambda: NOW,
        new_id,
        bounded_transport=S3BoundedObjectBlobTransport(
            runtime.settings, runtime.secrets,
            limits=ObjectReadLimits(connect_timeout_ms=1000, read_timeout_ms=1000,
                total_timeout_ms=5000, chunk_bytes=65536, maximum_attempts=1),
        ),
    )


def _content_reader(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    store: Any,
    clock: MutableClock,
) -> Any:
    """真实生产 reader：message_id → raw_artifact_ref → MinIO artifact → RFC822。"""
    from apps.scheduler_worker.adapters.message_content_reader import (
        ArtifactMessageContentReader,
    )
    from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork

    return ArtifactMessageContentReader(
        lambda requested: SqlAlchemyConversationsUnitOfWork(
            factory, requested, now=clock.now
        ),
        store,
        max_raw_bytes=1024 * 1024,
        max_subject_chars=200,
        max_body_chars=65536,
    )


async def _store_email(store: Any, tenant: TenantId, raw: bytes) -> str:
    from artifact_store.store import RawArtifactKind

    meta = await store.put(tenant, RawArtifactKind.EMAIL_RAW, raw, "message/rfc822")
    return str(meta.artifact_id)


def _rfc822(subject: str, body: str) -> bytes:
    return (
        f"From: customer@example.test\r\n"
        f"To: sales@example.test\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: <reply-trigger@example.test>\r\n"
        f"Date: Tue, 19 Aug 2026 09:00:00 +0000\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: 8bit\r\n"
        f"\r\n"
        f"{body}"
    ).encode()


def _reply_composition(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    outreach: Any,
    store: Any,
    clock: MutableClock,
    model_category: str = "unsubscribe",
) -> Any:
    """测试专用 reply 组合（仅测试；生产 main 不注入 fake）。

    唯一 fake 是 ReplyModelPort；reader/guard/conversations/outreach 全真实。
    """
    from apps.scheduler_worker.runtime import ReplyQualificationComposition

    port = _FakeModelPort(model_category)
    classifier = QualificationAgent(
        model="reply-scheduler-test-v1", model_client=port, gateway=None, guardrails=None
    )

    class _ReplyActions:
        async def route_bounce(self, *args: object) -> None:
            del args

        async def record_complaint(self, *args: object) -> None:
            del args

        async def request_handoff(self, *args: object) -> None:
            del args

        async def start_qualification(self, *args: object) -> None:
            del args

        async def extract_need_fields(self, *args: object) -> None:
            del args

        async def mark_future_restart(self, *args: object) -> None:
            del args

        async def create_follow_up(self, *args: object) -> None:
            del args

        async def intake_new_contact(self, *args: object) -> None:
            del args

    return ReplyQualificationComposition(
        classifier=classifier,
        content_reader=_content_reader(factory, tenant, store, clock),
        input_guard=CredentialMarkerGuard(),
        conversations=_conversations_service(factory, tenant, clock),
        outreach=outreach,
        action_ports=_ReplyActions(),
    )


def _dependencies(composition: Any, reply_composition: Any) -> Any:
    from apps.scheduler_worker.runtime import SchedulerDomainDependencies

    return SchedulerDomainDependencies(
        opportunity_service=_Opportunity(),
        employee_service=_Employees(),
        notification_audience=_Audience(),
        campaign_messaging=composition,
        reply_qualification=reply_composition,
    )


async def _publish(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    event: object,
) -> None:
    session = factory()
    try:
        bus = PostgresEventBus(session, tenant)
        await bus.publish(event)  # type: ignore[arg-type]
        await session.commit()
    finally:
        await session.close()


async def _poll(runtime: object, tenant: TenantId, clock: MutableClock) -> None:
    for _ in range(12):
        clock.value = clock.value + timedelta(seconds=1)
        await runtime.workflow.poll_due(tenant, 10)  # type: ignore[attr-defined]


async def _seed_reply_scenario(
    scheduler_reply_db: AsyncEngine,
    minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
    *,
    second_enrollment: bool = False,
) -> tuple[dict[str, object], _Transport, Any]:
    """种子：identity/campaign/enrollment + 真实 attempt（campaign driver 发送）。

    ``second_enrollment=True`` 时额外入组第二联系人（不同 account/contact），
    供「两条真实 attempt」错配关联用例使用。返回 env 字典；attempt 行由调用方查询。
    """
    factory = async_sessionmaker(scheduler_reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account_a = ProspectAccountId(new_id("acc"))
    contact_a = ContactPointId(new_id("cp"))
    contacts = {
        (contact_a, account_a): ContactEligibilitySnapshot(
            tenant, contact_a, account_a, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_trig", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    if second_enrollment:
        account_b = ProspectAccountId(new_id("acc"))
        contact_b = ContactPointId(new_id("cp"))
        contacts[(contact_b, account_b)] = ContactEligibilitySnapshot(
            tenant, contact_b, account_b, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_trig_b", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
        for contact, account in contacts
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    outreach = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact_a, account_a, "sched-enroll-reply-trigger")
    if second_enrollment:
        await _enroll(outreach, tenant, campaign_id, contact_b, account_b, "sched-enroll-reply-trigger-b")
    transport = _Transport()
    campaign_comp = _composition(
        transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders
    )
    runtime_engine, database_url = await scheduler_reply_runtime(str(tenant))
    factory = async_sessionmaker(runtime_engine, expire_on_commit=False)
    outreach = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    store = _raw_stores(runtime_engine, minio_runtime)
    reply_comp = _reply_composition(factory, tenant, outreach, store, clock)
    runtime_factory = importlib.import_module(
        "apps.scheduler_worker.runtime"
    ).SchedulerRuntimeFactory(
        _environ(database_url, tenant),
        _dependencies(campaign_comp, reply_comp),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    return {
        "factory": factory,
        "tenant": tenant,
        "clock": clock,
        "enrollment": enrollment,
        "runtime_factory": runtime_factory,
        "conversations": _conversations_service(factory, tenant, clock),
        "store": store,
    }, transport, reply_comp


async def _attempt_ids(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[str]:
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        attempts = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow).where(
                    rows.OutreachMessageAttemptRow.tenant_id == str(tenant),
                    rows.OutreachMessageAttemptRow.deterministic_message_id.is_not(None),
                )
            )
        ).scalars().all()
    return [row.deterministic_message_id for row in attempts]  # type: ignore[return-value]


async def test_inbound_stored_starts_reply_run_and_applies_actions(
    scheduler_reply_db: AsyncEngine, minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
) -> None:
    """成功链：真实 ingest→outbox→handler→workflow→reader→guard→model(fake)→
    分类→停序列+抑制；context/事件键集精确；无正文/凭证 marker。"""
    env, transport, reply_comp = await _seed_reply_scenario(
        scheduler_reply_db, minio_runtime, scheduler_reply_runtime
    )
    del reply_comp
    factory: async_sessionmaker[AsyncSession] = env["factory"]
    tenant: TenantId = env["tenant"]
    clock: MutableClock = env["clock"]
    enrollment = env["enrollment"]
    runtime_factory = env["runtime_factory"]
    conversations: ConversationService = env["conversations"]
    store = env["store"]

    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-reply-trigger-1"]
        attempt_id = (await _attempt_ids(factory, tenant))[0]

        # 真实入站：原文先落 MinIO → ingest_inbound 存引用并发布 InboundMessageStored
        artifact_ref = await _store_email(
            store, tenant, _rfc822("Re: hinges", BODY_MARKER)
        )
        message_id = await conversations.ingest_inbound(
            tenant,
            None,
            ProspectAccountId(str(enrollment.account_id)),
            artifact_ref,
            "<inbound-trigger@example.test>",
            NOW,
            outbound_message_id=OutboundMessageId(attempt_id),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]  投递 InboundMessageStored → 起 reply run
        # 启动后、推进前：初始 context 必须精确等于五个 typed ID（无 category/actions）
        async with factory() as session:
            started_run = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "reply_qualification",
                    )
                )
            ).scalars().one()
        assert set(started_run.context) == _REPLY_CONTEXT_KEYS
        assert BODY_MARKER not in json.dumps(started_run.context)

        await _poll(runtime, tenant, clock)  # classify + apply_actions
        await runtime.outbox.drain()  # type: ignore[attr-defined]  投递 ReplyReceived → campaign 收束
        await _poll(runtime, tenant, clock)

        async with factory() as session:
            reply_runs = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "reply_qualification",
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            suppressions = (
                await session.execute(
                    select(rows.OutreachSuppressionRow).where(
                        rows.OutreachSuppressionRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            classification = await session.get(
                rows.ConversationClassificationRow, (str(tenant), str(message_id))
            )
            inbound_events = (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == str(tenant),
                        rows.OutboxEventRow.event_type == "InboundMessageStored",
                    )
                )
            ).scalars().all()
            reply_events = (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == str(tenant),
                        rows.OutboxEventRow.event_type == "ReplyReceived",
                    )
                )
            ).scalars().all()
            all_outbox = (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        assert len(reply_runs) == 1
        assert reply_runs[0].status == "completed", reply_runs[0].last_error
        # 完成后只增加分类/动作/抑制范围及耐久分类时刻，不携带原文。
        assert set(reply_runs[0].context) == _REPLY_CONTEXT_KEYS | {
            "category",
            "actions",
            "classification_occurred_at",
            "suppress_scope",
        }
        # 最小披露：completed run 的 context 与 last_error（应为 None）
        # 不含正文 marker 与真实 secret marker。
        assert reply_runs[0].last_error is None
        context_dump = json.dumps(reply_runs[0].context)
        assert BODY_MARKER not in context_dump
        for marker in _SECRET_MARKERS:
            assert marker not in context_dump
        assert enrollment_row.state == "replied"  # apply_actions stop_sequence
        assert len(suppressions) == 1  # apply_actions suppress（unsubscribe）
        assert suppressions[0].reason == "unsubscribe"
        assert classification is not None
        assert classification.category == "unsubscribe"
        assert classification.classified_by == "reply-scheduler-test-v1"
        # 事件最小披露与无 marker
        assert len(inbound_events) == 1
        assert set(inbound_events[0].event_payload) == _INBOUND_EVENT_KEYS
        assert len(reply_events) == 1  # ReplyReceived 恰一次（分类落库发布）
        # 该 tenant 全部 outbox payload：无正文 marker、无真实 secret marker
        for event_row in all_outbox:
            payload_dump = json.dumps(event_row.event_payload)
            assert BODY_MARKER not in payload_dump
            for marker in _SECRET_MARKERS:
                assert marker not in payload_dump


async def test_inbound_stored_without_outbound_correlation_fails_closed(
    scheduler_reply_db: AsyncEngine, minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
) -> None:
    """无出站关联：不起 reply run、不停序列、不抑制。"""
    env, _transport, reply_comp = await _seed_reply_scenario(
        scheduler_reply_db, minio_runtime, scheduler_reply_runtime
    )
    del reply_comp, _transport
    factory = env["factory"]
    tenant: TenantId = env["tenant"]
    clock = env["clock"]
    enrollment = env["enrollment"]
    runtime_factory = env["runtime_factory"]
    conversations = env["conversations"]
    store = env["store"]

    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        artifact_ref = await _store_email(
            store, tenant, _rfc822("Re: x", "body")
        )
        await conversations.ingest_inbound(
            tenant, None, ProspectAccountId(str(enrollment.account_id)),
            artifact_ref, "<no-outbound@example.test>", NOW,
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            reply_runs = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "reply_qualification",
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            suppressions = (
                await session.execute(
                    select(rows.OutreachSuppressionRow).where(
                        rows.OutreachSuppressionRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        assert reply_runs == []
        assert enrollment_row.state == "in_sequence"
        assert suppressions == []


async def test_inbound_stored_unknown_outbound_fails_closed(
    scheduler_reply_db: AsyncEngine, minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
) -> None:
    """未知出站关联（attempt 无匹配）：不起 run、零副作用。"""
    env, _transport, reply_comp = await _seed_reply_scenario(
        scheduler_reply_db, minio_runtime, scheduler_reply_runtime
    )
    del reply_comp, _transport
    factory = env["factory"]
    tenant: TenantId = env["tenant"]
    clock = env["clock"]
    enrollment = env["enrollment"]
    runtime_factory = env["runtime_factory"]
    conversations = env["conversations"]
    store = env["store"]

    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        artifact_ref = await _store_email(
            store, tenant, _rfc822("Re: y", "body")
        )
        await conversations.ingest_inbound(
            tenant, None, ProspectAccountId(str(enrollment.account_id)),
            artifact_ref, "<unknown-outbound@example.test>", NOW,
            outbound_message_id=OutboundMessageId(
                "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid"
            ),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            reply_runs = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "reply_qualification",
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            suppressions = (
                await session.execute(
                    select(rows.OutreachSuppressionRow).where(
                        rows.OutreachSuppressionRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        assert reply_runs == []
        assert enrollment_row.state == "in_sequence"
        assert suppressions == []


async def test_inbound_stored_missing_or_forged_message_fails_closed(
    scheduler_reply_db: AsyncEngine, minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
) -> None:
    """五类伪造/缺失/跨租户消息事件：全部 0 run / 0 副作用。"""
    env, _transport, reply_comp = await _seed_reply_scenario(
        scheduler_reply_db, minio_runtime, scheduler_reply_runtime, second_enrollment=True
    )
    del reply_comp
    factory = env["factory"]
    tenant: TenantId = env["tenant"]
    clock = env["clock"]
    enrollment = env["enrollment"]
    runtime_factory = env["runtime_factory"]
    conversations = env["conversations"]
    store = env["store"]

    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        # 两条真实 enrollment 都入组 → campaign driver 发送两封 → 两条真实 attempt
        assert await runtime.campaign_driver.scan_once() == 2  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        assert len(_transport.sent) == 2
        attempt_ids = await _attempt_ids(factory, tenant)
        assert len(attempt_ids) == 2
        attempt_a, attempt_b = attempt_ids[0], attempt_ids[1]

        # 真实 inbound 行：持久化关联 = attempt A
        artifact_ref = await _store_email(
            store, tenant, _rfc822("Re: forged", "body")
        )
        real_message_id = await conversations.ingest_inbound(
            tenant, None, ProspectAccountId(str(enrollment.account_id)),
            artifact_ref, "<forged-base@example.test>", NOW,
            outbound_message_id=OutboundMessageId(attempt_a),
        )

        async def assert_zero_side_effects() -> None:
            async with factory() as session:
                reply_runs = (
                    await session.execute(
                        select(rows.WorkflowRunRow).where(
                            rows.WorkflowRunRow.tenant_id == str(tenant),
                            rows.WorkflowRunRow.workflow_type == "reply_qualification",
                        )
                    )
                ).scalars().all()
                enrollment_row = await session.get(
                    rows.OutreachEnrollmentRow,
                    (str(tenant), str(enrollment.enrollment_id)),
                )
                suppressions = (
                    await session.execute(
                        select(rows.OutreachSuppressionRow).where(
                            rows.OutreachSuppressionRow.tenant_id == str(tenant)
                        )
                    )
                ).scalars().all()
            assert reply_runs == []
            assert enrollment_row.state == "in_sequence"
            assert suppressions == []

        def _handler(handler_tenant: TenantId) -> object:
            from apps.scheduler_worker.reply_events import (
                ReplyQualificationEventHandlers,
            )
            from infra.db.workflow_engine import PostgresWorkflowEngine

            engine = PostgresWorkflowEngine(factory, {}, now=clock.now)
            return ReplyQualificationEventHandlers(
                engine=engine, factory=factory, tenant_id=handler_tenant
            )

        # a) 事件 tenant 不匹配：直投 handler（事件租户 != 绑定租户）
        await _handler(tenant).handle(  # type: ignore[attr-defined]
            InboundMessageStored(
                tenant_id=TenantId(new_id("tn")),
                occurred_at=NOW,
                message_id=MessageId(str(real_message_id)),
                outbound_message_id=OutboundMessageId(attempt_a),
            )
        )
        await assert_zero_side_effects()

        # b) 消息不存在：伪造 message_id
        await _handler(tenant).handle(  # type: ignore[attr-defined]
            InboundMessageStored(
                tenant_id=tenant,
                occurred_at=NOW,
                message_id=MessageId("msg_01KZXT00000000000000000000"),
                outbound_message_id=OutboundMessageId(attempt_a),
            )
        )
        await assert_zero_side_effects()

        # c) direction 非 inbound：翻转真实行方向后投递
        async with factory() as session:
            row = await session.get(rows.MessageRow, (str(tenant), str(real_message_id)))
            row.direction = "outbound"
            await session.commit()
        await _handler(tenant).handle(  # type: ignore[attr-defined]
            InboundMessageStored(
                tenant_id=tenant,
                occurred_at=NOW,
                message_id=MessageId(str(real_message_id)),
                outbound_message_id=OutboundMessageId(attempt_a),
            )
        )
        await assert_zero_side_effects()
        async with factory() as session:
            row = await session.get(rows.MessageRow, (str(tenant), str(real_message_id)))
            row.direction = "inbound"
            await session.commit()

        # d) 持久化关联与事件错配：真实 inbound 行关联 attempt A，
        #    事件把真实 attempt B 的 outbound id 拼到该 message（row 关联 != 事件）
        await _handler(tenant).handle(  # type: ignore[attr-defined]
            InboundMessageStored(
                tenant_id=tenant,
                occurred_at=NOW,
                message_id=MessageId(str(real_message_id)),
                outbound_message_id=OutboundMessageId(attempt_b),
            )
        )
        await assert_zero_side_effects()

        # e) 消息仅存在于另一 tenant：事件用真实 other_message_id，
        #    本 tenant 无该行 → fail-closed
        other_tenant = TenantId(new_id("tn"))
        other_account = ProspectAccountId(new_id("acc"))
        other_engine, _other_database_url = await scheduler_reply_runtime(str(other_tenant))
        other_factory = async_sessionmaker(other_engine, expire_on_commit=False)
        artifact_ref_other = await _store_email(
            _raw_stores(other_engine, minio_runtime), other_tenant, _rfc822("Re: other", "body")
        )
        conversations_other = _conversations_service(other_factory, other_tenant, clock)
        other_message_id = await conversations_other.ingest_inbound(
            other_tenant, None, other_account,
            artifact_ref_other, "<other-tenant@example.test>", NOW,
        )
        await _handler(tenant).handle(  # type: ignore[attr-defined]
            InboundMessageStored(
                tenant_id=tenant,
                occurred_at=NOW,
                message_id=MessageId(str(other_message_id)),
                outbound_message_id=OutboundMessageId(attempt_a),
            )
        )
        await assert_zero_side_effects()


async def test_reply_trigger_consumer_redelivery_is_idempotent(
    scheduler_reply_db: AsyncEngine, minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
) -> None:
    """同一事件重复投递（outbox 两行）→ 恰一条 run/一次分类/一条抑制/一条 ReplyReceived。"""
    env, _transport, reply_comp = await _seed_reply_scenario(
        scheduler_reply_db, minio_runtime, scheduler_reply_runtime
    )
    del reply_comp, _transport
    factory = env["factory"]
    tenant: TenantId = env["tenant"]
    clock = env["clock"]
    enrollment = env["enrollment"]
    runtime_factory = env["runtime_factory"]
    conversations = env["conversations"]
    store = env["store"]

    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        attempt_id = (await _attempt_ids(factory, tenant))[0]
        artifact_ref = await _store_email(
            store, tenant, _rfc822("Re: idem", BODY_MARKER)
        )
        message_id = await conversations.ingest_inbound(
            tenant, None, ProspectAccountId(str(enrollment.account_id)),
            artifact_ref, "<idem@example.test>", NOW,
            outbound_message_id=OutboundMessageId(attempt_id),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        # 消费者重投同一事件（第二份 outbox 行，同 payload）
        await _publish(
            factory,
            tenant,
            InboundMessageStored(
                tenant_id=tenant,
                occurred_at=NOW,
                message_id=MessageId(str(message_id)),
                outbound_message_id=OutboundMessageId(attempt_id),
            ),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)

        async with factory() as session:
            reply_runs = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "reply_qualification",
                    )
                )
            ).scalars().all()
            classifications = (
                await session.execute(
                    select(rows.ConversationClassificationRow).where(
                        rows.ConversationClassificationRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            suppressions = (
                await session.execute(
                    select(rows.OutreachSuppressionRow).where(
                        rows.OutreachSuppressionRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            reply_events = (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == str(tenant),
                        rows.OutboxEventRow.event_type == "ReplyReceived",
                    )
                )
            ).scalars().all()
        assert len(reply_runs) == 1  # engine 幂等键 reply:{message_id}
        assert reply_runs[0].status == "completed", reply_runs[0].last_error
        assert len(classifications) == 1  # record_classification at-most-once
        assert len(suppressions) == 1  # add_suppression 幂等键
        assert len(reply_events) == 1  # ReplyReceived 恰一次


async def test_reply_trigger_does_not_retrigger_on_reply_received(
    scheduler_reply_db: AsyncEngine, minio_runtime: _MinioRuntime,
    scheduler_reply_runtime: RuntimeEngineFactory,
) -> None:
    """ReplyReceived 是分类落库后的结果事件：不回触新 reply run，无二次分类/抑制。"""
    env, _transport, reply_comp = await _seed_reply_scenario(
        scheduler_reply_db, minio_runtime, scheduler_reply_runtime
    )
    del reply_comp, _transport
    factory = env["factory"]
    tenant: TenantId = env["tenant"]
    clock = env["clock"]
    enrollment = env["enrollment"]
    runtime_factory = env["runtime_factory"]
    conversations = env["conversations"]
    store = env["store"]

    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        attempt_id = (await _attempt_ids(factory, tenant))[0]
        artifact_ref = await _store_email(
            store, tenant, _rfc822("Re: no-retrigger", BODY_MARKER)
        )
        await conversations.ingest_inbound(
            tenant, None, ProspectAccountId(str(enrollment.account_id)),
            artifact_ref, "<no-retrigger@example.test>", NOW,
            outbound_message_id=OutboundMessageId(attempt_id),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        await runtime.outbox.drain()  # type: ignore[attr-defined]  投递 ReplyReceived
        await _poll(runtime, tenant, clock)

        async with factory() as session:
            reply_runs = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "reply_qualification",
                    )
                )
            ).scalars().all()
            inbound_events = (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == str(tenant),
                        rows.OutboxEventRow.event_type == "InboundMessageStored",
                    )
                )
            ).scalars().all()
            reply_events = (
                await session.execute(
                    select(rows.OutboxEventRow).where(
                        rows.OutboxEventRow.tenant_id == str(tenant),
                        rows.OutboxEventRow.event_type == "ReplyReceived",
                    )
                )
            ).scalars().all()
            classifications = (
                await session.execute(
                    select(rows.ConversationClassificationRow).where(
                        rows.ConversationClassificationRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            suppressions = (
                await session.execute(
                    select(rows.OutreachSuppressionRow).where(
                        rows.OutreachSuppressionRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
        # 对象级断言：事件计数与单 run/单分类/单抑制，不依赖推理
        assert len(inbound_events) == 1
        assert len(reply_runs) == 1
        assert reply_runs[0].status == "completed", reply_runs[0].last_error
        assert len(reply_events) == 1
        assert len(classifications) == 1
        assert len(suppressions) == 1
        assert enrollment_row.state == "replied"
