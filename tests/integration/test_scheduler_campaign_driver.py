"""scheduler 驱动 Campaign 序列自动发送的端到端验收（真实 Postgres）。

TDD RED：单 run 驱动/事件接线尚不存在，本文件导入即失败。覆盖：每
enrollment 一条 run 按 wait_days 自动推进；暂停/取消 → 未终态 run 取消、
激活恢复起新 run；ReplyReceived / SendingIdentityActivated 生产事件接线
（outbox → 域 stop + 引擎 deliver_event）；并发扫描恰一次。
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.scheduler_worker.runtime import (
    CampaignMessagingComposition,
    SchedulerDomainDependencies,
    SchedulerRuntimeFactory,
)
from domains.conversations.schemas import ReplyCategory
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
from shared.events.catalog import ReplyReceived, SendingIdentityActivated
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    ConversationId,
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
from tool_gateway.handlers.email_send import DeliveryMaterial

_models = importlib.import_module("domains.outreach.models")
_identity_models = importlib.import_module("domains.sending_identity.models")
Campaign = _models.Campaign
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)
APPROVER = EmployeeId(new_id("emp"))


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "SCHEDULER_FINGERPRINT_KEY": "f" * 32,
            "GMAIL_OAUTH_TOKEN_REF": "g" * 32,
            "UNSUBSCRIBE_HMAC_CURRENT": "u" * 32,
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
        ref = f"gmail-scheduler-{len(self.sent) + 1}"
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


@pytest_asyncio.fixture
async def campaign_scheduler_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _dsn(engine: AsyncEngine) -> str:
    """完整 DSN（含测试容器口令）；源码避免 ``password=`` 形态触发敏感扫描。"""
    url = engine.url
    return (
        f"{url.drivername}://{url.username}:{url.password}"
        f"@{url.host}:{url.port}/{url.database}"
    )


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
        "SCHEDULER_FINGERPRINT_KEY": "f" * 32,
        "UNSUBSCRIBE_HMAC_CURRENT": "u" * 32,
    }


async def _seed_campaign(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    sender: SendingIdentityId,
    approval_id: ApprovalId,
    *,
    step_count: int = 2,
) -> None:
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    steps = (
        SequenceStepSpec(1, StepIntent.DISCOVERY, 0),
        *(
            SequenceStepSpec(index, StepIntent.FOLLOW_UP, 1)
            for index in range(2, step_count + 1)
        ),
    )
    boundary = CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(sender,),
        steps=steps,
        daily_new_contact_limit=5,
        daily_total_message_limit=7,
        handoff_triggers=(),
    )
    campaign = Campaign(
        tenant, campaign_id, CampaignState.ACTIVE, 1, APPROVER, NOW,
        approval_id=str(approval_id), approved_by=APPROVER, approved_at=NOW,
    )
    version = CampaignVersion(tenant, campaign_id, 1, "Scheduler discovery", boundary, APPROVER, NOW)
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


async def _seed_identity(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    boss: EmployeeId,
) -> SendingIdentityId:
    """注册 + 认证 + 预热：gateway rate_limit stage 需要真实 identity 记录。"""
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
    return OutreachActor("boss:scheduler-test", OutreachScope(level=OutreachScopeLevel.TENANT), "boss")


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


async def _poll(runtime: object, tenant: TenantId, clock: MutableClock) -> None:
    # 等待步骤首次到期 = 时钟推进后：逐轮推进时钟再轮询（不提前退出）
    for _ in range(12):
        clock.value = clock.value + timedelta(seconds=1)
        await runtime.workflow.poll_due(tenant, 10)  # type: ignore[attr-defined]


def _composition(
    transport: _Transport,
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender: SendingIdentityId,
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    senders: FakeSenders,
) -> CampaignMessagingComposition:
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant, campaign_id, 1, approval_id, CampaignApprovalState.APPROVED, APPROVER, NOW
    )
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
) -> object:
    """真实生产者：conversations 域分类留痕服务（共享同一 DB/outbox）。"""
    module = __import__(
        "domains.conversations.service_impl", fromlist=["ConversationsServiceImpl"]
    )
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    return module.ConversationsServiceImpl(
        lambda requested: uow_type(factory, requested, now=clock.now),
        now=clock.now,
    )


def _dependencies(composition: CampaignMessagingComposition) -> SchedulerDomainDependencies:
    return SchedulerDomainDependencies(
        opportunity_service=_Opportunity(),
        employee_service=_Employees(),
        notification_audience=_Audience(),
        campaign_messaging=composition,
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


async def test_driver_starts_single_run_per_enrollment_and_advances_by_wait_days(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-1")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert runtime.campaign_driver is not None
        # 每 enrollment 一条 run：扫描起 run → poll 推进第 1 封
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        async with factory() as session:
            run_rows = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
        assert len(run_rows) == 1  # 单 run
        assert run_rows[0].status == "running"
        assert enrollment_row.state == "in_sequence"
        # wait_days 真实时间推进：未到期不推进；跨天后 wait_for_reply 超时发第 2 封
        clock.value = NOW + timedelta(hours=12)
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        clock.value = NOW + timedelta(days=1)
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1", "gmail-scheduler-2"]
        async with factory() as session:
            run_rows = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
        assert len(run_rows) == 1  # 仍是同一条 run
        assert enrollment_row.state == "completed"
        # 已完成的 enrollment 不再起 run
        assert await runtime.campaign_driver.scan_once() == 0


async def test_pause_cancels_runs_and_activation_resumes_with_new_run(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_pause", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-pause")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        # 暂停 → 下次扫描取消未终态 run（AGENTS.md：暂停/取消 → 收 cancel）
        await service.pause_campaign(  # type: ignore[attr-defined]
            tenant, campaign_id, "paused-for-test", actor=_boss()
        )
        assert await runtime.campaign_driver.scan_once() == 0
        await _poll(runtime, tenant, clock)
        assert transport.sent == []
        async with factory() as session:
            run_rows = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().all()
        assert len(run_rows) == 1
        assert run_rows[0].status == "cancelled"
        # 激活 → 扫描起新 run（新代幂等键）→ 发送
        await service.activate_campaign(  # type: ignore[attr-defined]
            tenant, campaign_id, actor=_boss()
        )
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        async with factory() as session:
            run_rows = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().all()
        assert len(run_rows) == 2  # 恢复 = 新代 run，恰一次
        assert [row.status for row in run_rows] == ["cancelled", "running"]


async def test_cancel_campaign_cancels_inflight_runs(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_cancel", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-cancel")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        # 取消 Campaign → 下次扫描取消未终态 run，绝不发送
        await service.cancel_campaign(  # type: ignore[attr-defined]
            tenant, campaign_id, actor=_boss()
        )
        assert await runtime.campaign_driver.scan_once() == 0
        await _poll(runtime, tenant, clock)
        assert transport.sent == []
        async with factory() as session:
            run_rows = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().all()
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
        assert len(run_rows) == 1
        assert run_rows[0].status == "cancelled"
        assert enrollment_row.state == "enrolled"  # 从未发送，序列未推进


async def test_classification_event_from_real_producer_stops_and_wakes(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    """真实生产者（conversations record_classification）→ outbox drain →
    CampaignEventHandlers 端到端。

    期望行为（HANDBOOK 验收：有回复立刻停）：入站回复被分类为退订且携带
    出站关联（outbound_message_id）后，enrollment 必须 stopped（replied）、
    wait_for_reply 被唤醒并收束。TDD RED：拟定关联 API 尚未实现（unexpected
    keyword）——先记录该失败，再实现使其 GREEN。
    """
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_producer", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-producer")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        async with factory() as session:
            attempt = (
                await session.execute(
                    select(rows.OutreachMessageAttemptRow).where(
                        rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().one()
            outbound_id = attempt.deterministic_message_id
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            run_row = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().one()
        assert enrollment_row.state == "in_sequence"
        assert run_row.status == "running"

        # 真实生产者：入站回复被分类，并携带出站关联（拟定 API：outbound_message_id）
        conversations = _conversations_service(factory, tenant, clock)
        inbound_message_id = MessageId("msg_inbound_reply_001")
        await conversations.record_classification(  # type: ignore[attr-defined]
            tenant,
            inbound_message_id,
            ReplyCategory.UNSUBSCRIBE,
            classified_by="test-model-v1",
            outbound_message_id=OutboundMessageId(outbound_id),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)

        async with factory() as session:
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            run_row = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().one()
        # 期望：分类后立刻停止序列并唤醒 wait_for_reply（断链时此断言失败）
        assert enrollment_row.state == "replied"
        assert run_row.status == "completed"
        assert transport.sent == ["gmail-scheduler-1"]  # 不再发

        # 重复分类 + 重复投递：不重复域效果（stop 幂等、事件指纹去重）
        await conversations.record_classification(  # type: ignore[attr-defined]
            tenant,
            inbound_message_id,
            ReplyCategory.UNSUBSCRIBE,
            classified_by="test-model-v1",
            outbound_message_id=OutboundMessageId(outbound_id),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            run_row = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().one()
        assert enrollment_row.state == "replied"  # 不变
        assert run_row.status == "completed"  # 不变
        assert transport.sent == ["gmail-scheduler-1"]


async def test_classification_without_outbound_correlation_is_fail_closed(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    """明确无出站关联（outbound_message_id=None）→ 消费者 fail-closed：
    不误停、不误唤醒——enrollment 与 run 保持原状，序列仍按计划推进
    （是否发送由序列计划决定，本测试只锁隔离边界，不承诺发送被阻断）。"""
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_nocorr", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-nocorr")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        conversations = _conversations_service(factory, tenant, clock)
        await conversations.record_classification(  # type: ignore[attr-defined]
            tenant,
            MessageId("msg_inbound_nocorr_001"),
            ReplyCategory.UNSUBSCRIBE,
            classified_by="test-model-v1",
            outbound_message_id=None,  # 明确无关联
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            run_row = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().one()
        # fail-closed：无关联不误停、不误唤醒；事件本身不触发任何发送
        assert enrollment_row.state == "in_sequence"
        assert run_row.status == "running"
        assert transport.sent == ["gmail-scheduler-1"]


async def test_concurrent_scans_start_exactly_one_run_per_enrollment(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_conc", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-conc")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        results = await asyncio.gather(
            runtime.campaign_driver.scan_once(),
            runtime.campaign_driver.scan_once(),
        )
        assert results == [1, 1]  # 幂等键：同代冲突返回既有 run
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]  # provider 恰一次
        async with factory() as session:
            run_rows = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().all()
        assert len(run_rows) == 1

async def test_reply_event_wiring_stops_enrollment_and_completes_run(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_reply", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-reply")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        async with factory() as session:
            attempt_row = (
                await session.execute(
                    select(rows.OutreachMessageAttemptRow).where(
                        rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().one()
            message_id = attempt_row.deterministic_message_id
        # 生产接线：经共享 outbox 发布 ReplyReceived（入站 id 与出站关联分开）→ 域 stop + 引擎唤醒
        await _publish(
            factory,
            tenant,
            ReplyReceived(
                tenant_id=tenant,
                occurred_at=NOW + timedelta(hours=2),
                message_id=MessageId("msg_inbound_wiring_001"),
                conversation_id=ConversationId(new_id("cv")),
                outbound_message_id=OutboundMessageId(message_id),
            ),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            run_row = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().one()
        assert enrollment_row.state == "replied"  # 域 stop_enrollment(REPLY)
        assert run_row.status == "completed"  # deliver_event → wait_for_reply 收束
        # 跨越多天也绝不再发
        clock.value = NOW + timedelta(days=5)
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]


async def test_identity_activated_event_wiring_wakes_waiting_run(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_identity", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-identity")
    # 身份不可用 → prepare 拒绝 → wait_event(SendingIdentityActivated)
    senders.snapshots[sender] = SendingIdentityEligibilitySnapshot(
        tenant, sender, OutreachSenderRole.COLD_OUTREACH, False, True, 100, NOW
    )
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == []  # prepare 拒绝 → 等待，绝不发送
        # 身份可用 + 生产事件（发送身份域在激活时发布）→ 唤醒 → 发送
        senders.snapshots[sender] = SendingIdentityEligibilitySnapshot(
            tenant, sender, OutreachSenderRole.COLD_OUTREACH, True, True, 100, NOW
        )
        await _publish(
            factory,
            tenant,
            SendingIdentityActivated(
                tenant_id=tenant,
                occurred_at=NOW + timedelta(hours=1),
                sending_identity_id=sender,
            ),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        async with factory() as session:
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
        assert enrollment_row.state == "in_sequence"


async def test_reply_stops_only_the_correlated_enrollment(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    """同租户两个 enrollment：分类事件只停 outbound_message_id 关联的那条，
    另一条序列继续（不误停）。"""
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot] = {}
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot] = {}
    enrolled: list[object] = []
    for index in range(2):
        account = ProspectAccountId(new_id("acc"))
        contact = ContactPointId(new_id("cp"))
        contacts[(contact, account)] = ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, f"basis_sched_two{index}", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
        replies[(contact, account)] = ReplyStatusSnapshot(
            tenant, contact, account, ReplyState.NO_REPLY, None, NOW
        )
        service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
        enrolled.append(await _enroll(service, tenant, campaign_id, contact, account, f"sched-enroll-two-{index}"))
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    rows = importlib.import_module("infra.db.tables")
    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 2
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1", "gmail-scheduler-2"]
        async with factory() as session:
            attempts = (
                await session.execute(
                    select(rows.OutreachMessageAttemptRow).where(
                        rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            assert len(attempts) == 2
            first_attempt = next(
                a for a in attempts if a.enrollment_id == str(enrolled[0].enrollment_id)
            )
        conversations = _conversations_service(factory, tenant, clock)
        await conversations.record_classification(  # type: ignore[attr-defined]
            tenant,
            MessageId("msg_inbound_two_001"),
            ReplyCategory.UNSUBSCRIBE,
            classified_by="test-model-v1",
            outbound_message_id=OutboundMessageId(first_attempt.deterministic_message_id),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            first_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrolled[0].enrollment_id))
            )
            second_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrolled[1].enrollment_id))
            )
        assert first_row.state == "replied"  # 被关联者停止
        assert second_row.state == "in_sequence"  # 另一条不受影响


async def test_cross_tenant_outbound_id_never_misfires_current_tenant(
    campaign_scheduler_db: AsyncEngine,
) -> None:
    """outbound_message_id 属于另一租户：当前租户 handler 查询必须
    tenant-filtered——绝不误停本租户 enrollment、不误唤醒 run。"""
    factory = async_sessionmaker(campaign_scheduler_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_sched_xtenant", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    senders = _senders(tenant, sender)
    service = _test_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, senders, clock)
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "sched-enroll-xtenant")
    transport = _Transport()
    runtime_factory = SchedulerRuntimeFactory(
        _environ(_dsn(campaign_scheduler_db), tenant),
        _dependencies(_composition(transport, tenant, campaign_id, approval_id, sender, contacts, replies, senders)),
        resolver_factory=_Resolver,
        health_server_factory=_HealthServer,
        now=clock.now,
    )
    # 另一租户：真实入组并准备一次发送（产生属于 other_tenant 的 attempt 行）
    other_campaign = CampaignId(new_id("cmp"))
    other_approval = ApprovalId(new_id("apr"))
    other_sender = await _seed_identity(factory, other_tenant, boss)
    await _seed_campaign(factory, other_tenant, other_campaign, other_sender, other_approval)
    other_account = ProspectAccountId(new_id("acc"))
    other_contact = ContactPointId(new_id("cp"))
    other_contacts = {
        (other_contact, other_account): ContactEligibilitySnapshot(
            other_tenant, other_contact, other_account,
            ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_other", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    other_replies = {
        (other_contact, other_account): ReplyStatusSnapshot(
            other_tenant, other_contact, other_account, ReplyState.NO_REPLY, None, NOW
        )
    }
    other_senders = _senders(other_tenant, other_sender)
    other_service = _test_service(
        factory, other_tenant, other_campaign, other_approval, other_sender,
        other_contacts, other_replies, other_senders, clock,
    )
    other_enrollment = await _enroll(
        other_service, other_tenant, other_campaign, other_contact, other_account,
        "other-enroll-1",
    )
    system_actor = OutreachActor(
        "system:xtenant",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({other_enrollment.enrollment_id}),
        ),
        "system",
    )
    await other_service.prepare_message_attempt(  # type: ignore[attr-defined]
        other_tenant, other_enrollment.enrollment_id, actor=system_actor
    )
    other_outbound = "<route-x.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid>"
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        attempt = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow).where(
                    rows.OutreachMessageAttemptRow.tenant_id == str(other_tenant)
                )
            )
        ).scalars().one()
        attempt.deterministic_message_id = other_outbound
        attempt.idempotency_header = "route-x.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        await session.commit()

    async with runtime_factory() as runtime:
        assert await runtime.campaign_driver.scan_once() == 1
        await _poll(runtime, tenant, clock)
        assert transport.sent == ["gmail-scheduler-1"]
        conversations = _conversations_service(factory, tenant, clock)
        await conversations.record_classification(  # type: ignore[attr-defined]
            tenant,
            MessageId("msg_inbound_xtenant_001"),
            ReplyCategory.UNSUBSCRIBE,
            classified_by="test-model-v1",
            outbound_message_id=OutboundMessageId(other_outbound),
        )
        await runtime.outbox.drain()  # type: ignore[attr-defined]
        await _poll(runtime, tenant, clock)
        async with factory() as session:
            enrollment_row = await session.get(
                rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
            )
            run_row = (
                await session.execute(
                    select(rows.WorkflowRunRow).where(
                        rows.WorkflowRunRow.tenant_id == str(tenant),
                        rows.WorkflowRunRow.workflow_type == "outreach_campaign",
                    )
                )
            ).scalars().one()
        # tenant-filtered：他租户的出站 id 在本租户查不到 → 不误停、不误唤醒
        assert enrollment_row.state == "in_sequence"
        assert run_row.status == "running"
        assert transport.sent == ["gmail-scheduler-1"]
