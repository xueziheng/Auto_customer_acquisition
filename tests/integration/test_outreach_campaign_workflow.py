"""outreach_campaign 工作流经真实 PostgreSQL 引擎的序列自动推进验收。

每步一条 run（draft → prepare → send → record → complete）；步骤间等待
由 ``Enrollment.next_send_at`` + 驱动扫描负责（驱动在 commit 3 接线，本
文件直接起 run 模拟）。回复/暂停/额度语义走域服务与引擎退避重试。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
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
    SendAuthorization,
    SendingIdentityEligibilitySnapshot,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    IdempotencyKey,
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

_models = importlib.import_module("domains.outreach.models")
Campaign = _models.Campaign
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
EnrollmentState = _models.EnrollmentState
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)
APPROVER = EmployeeId(new_id("emp"))
RETRY_INTERVAL = timedelta(seconds=1)


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@pytest_asyncio.fixture
async def campaign_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_campaign(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    sender: SendingIdentityId,
    approval_id: ApprovalId,
    *,
    message_limit: int = 7,
    state: CampaignState = CampaignState.ACTIVE,
) -> None:
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    boundary = CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(sender,),
        steps=(
            SequenceStepSpec(1, StepIntent.DISCOVERY, 0),
            SequenceStepSpec(2, StepIntent.FOLLOW_UP, 1),
            SequenceStepSpec(3, StepIntent.FOLLOW_UP, 1),
        ),
        daily_new_contact_limit=min(5, message_limit),
        daily_total_message_limit=message_limit,
        handoff_triggers=(),
    )
    campaign = Campaign(
        tenant, campaign_id, state, 1, APPROVER, NOW,
        approval_id=str(approval_id), approved_by=APPROVER, approved_at=NOW,
    )
    version = CampaignVersion(tenant, campaign_id, 1, "Workflow discovery", boundary, APPROVER, NOW)
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


def _service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender: SendingIdentityId,
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    clock: MutableClock,
):
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant, campaign_id, 1, approval_id, CampaignApprovalState.APPROVED, APPROVER, NOW
    )
    senders = FakeSenders(
        {
            sender: SendingIdentityEligibilitySnapshot(
                tenant, sender, OutreachSenderRole.COLD_OUTREACH, True, True, 100, NOW
            )
        },
        trace,
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


class _FakeSender:
    """受控发送适配器：真实 claim + 固定 provider ref（不打开网络）。"""

    def __init__(self, service: object, tenant: TenantId) -> None:
        self._service = service
        self._tenant = tenant
        self.sent: list[str] = []

    async def send(
        self, *, tenant_id: TenantId, authorization: SendAuthorization
    ) -> str:
        assert tenant_id == self._tenant
        actor = Actor(
            "system:campaign-test",
            OutreachScope(
                level=ScopeLevel.SYSTEM,
                allowed_enrollment_ids=frozenset({authorization.enrollment_id}),
            ),
            "system",
        )
        await self._service.claim_message_send(  # type: ignore[attr-defined]
            tenant_id, authorization.attempt_id, actor=actor
        )
        ref = f"gmail-fake-{len(self.sent) + 1}"
        self.sent.append(ref)
        return ref


async def _build_engine(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender: SendingIdentityId,
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    clock: MutableClock,
) -> tuple[object, _FakeSender, object]:
    service = _service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    sender_adapter = _FakeSender(service, tenant)
    flow = importlib.import_module("workflows.outreach_campaign.flow")
    engine_type = importlib.import_module("infra.db.workflow_engine").PostgresWorkflowEngine
    engine = engine_type(
        factory,
        {**flow.build_outreach_campaign_handlers(service, sender_adapter)},
        now=clock.now,
    )
    engine.register(flow.build_outreach_campaign_definition(RETRY_INTERVAL))
    return engine, sender_adapter, service


async def _enroll(
    service: object,
    tenant: TenantId,
    campaign_id: CampaignId,
    contact: ContactPointId,
    account: ProspectAccountId,
    key: str,
) -> object:
    boss = Actor("boss:campaign", OutreachScope(level=ScopeLevel.TENANT), "boss")
    return await service.enroll(  # type: ignore[attr-defined]
        tenant, campaign_id,
        EnrollmentCreateRequest(account, contact, IdempotencyKey(key)),
        actor=boss,
    )


async def _earliest_due(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> datetime | None:
    """运行中 run 的最近到期步骤；无则返回 None（scheduler 无活可干）。"""
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        due = (
            await session.execute(
                select(rows.WorkflowStepRow.due_at)
                .join(
                    rows.WorkflowRunRow,
                    and_(
                        rows.WorkflowRunRow.tenant_id == rows.WorkflowStepRow.tenant_id,
                        rows.WorkflowRunRow.run_id == rows.WorkflowStepRow.run_id,
                    ),
                )
                .where(
                    rows.WorkflowStepRow.tenant_id == str(tenant),
                    rows.WorkflowRunRow.status == "running",
                    rows.WorkflowStepRow.status.in_(("pending", "running")),
                )
                .order_by(rows.WorkflowStepRow.due_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return due


async def _advance_and_poll(
    engine: object,
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    clock: MutableClock,
    times: int = 12,
) -> None:
    """模拟 scheduler：把时钟推进到最早到期步骤再轮询；无到期即停。"""
    for _ in range(times):
        next_due = await _earliest_due(factory, tenant)
        if next_due is None:
            return
        clock.value = max(clock.value, next_due)
        await engine.poll_due(tenant, 10)  # type: ignore[attr-defined]


async def _start_run(
    engine: object, tenant: TenantId, enrollment: object, campaign_id: CampaignId
) -> str:
    """每步一条 run：幂等键按 Enrollment.current_step + 1 分步。"""
    step_number = enrollment.current_step + 1
    return await engine.start(  # type: ignore[attr-defined]
        tenant,
        "outreach_campaign",
        str(enrollment.enrollment_id),
        {
            "enrollment_id": str(enrollment.enrollment_id),
            "campaign_id": str(campaign_id),
        },
        f"campaign:{enrollment.enrollment_id}:step{step_number}",
    )


async def _refresh_enrollment(service: object, tenant: TenantId, enrollment_id: str) -> object:
    boss = Actor("boss:campaign", OutreachScope(level=ScopeLevel.TENANT), "boss")
    return await service.get_enrollment(  # type: ignore[attr-defined]
        tenant, enrollment_id, actor=boss
    )


async def test_sequence_advances_all_steps_by_wait_days_then_completes(
    campaign_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_wf", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    engine, sender_adapter, service = await _build_engine(
        factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock
    )
    rows = importlib.import_module("infra.db.tables")
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-1")

    # 第 1 步：一条 run = draft → prepare → send → record → complete
    run1_id = await _start_run(engine, tenant, enrollment, campaign_id)
    await _advance_and_poll(engine, factory, tenant, clock)
    async with factory() as session:
        attempts = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow).where(
                    rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
        )
        run_row = await session.get(rows.WorkflowRunRow, run1_id)
    assert len(attempts) == 1
    assert attempts[0].state == "sent"
    assert attempts[0].provider_ref == "gmail-fake-1"
    assert enrollment_row.state == "in_sequence"
    assert enrollment_row.current_step == 1
    assert enrollment_row.next_send_at == NOW + timedelta(days=1)
    assert run_row.status == "completed"
    assert sender_adapter.sent == ["gmail-fake-1"]

    # 第 2 步：跨过 wait_days 后由驱动重新起 run（key 分步，绝不重复启动）
    clock.value = NOW + timedelta(days=1)
    enrollment = await _refresh_enrollment(service, tenant, enrollment.enrollment_id)
    run2_id = await _start_run(engine, tenant, enrollment, campaign_id)
    run2_again = await _start_run(engine, tenant, enrollment, campaign_id)
    assert run2_again == run2_id  # 幂等键：同一步不重复启动
    await _advance_and_poll(engine, factory, tenant, clock)
    async with factory() as session:
        attempts = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow).where(
                    rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
        )
    assert len(attempts) == 2
    assert enrollment_row.current_step == 2
    assert sender_adapter.sent == ["gmail-fake-1", "gmail-fake-2"]

    # 第 3 步 + 完成：末步发送后 enrollment completed，run completed
    clock.value = NOW + timedelta(days=2)
    enrollment = await _refresh_enrollment(service, tenant, enrollment.enrollment_id)
    run3_id = await _start_run(engine, tenant, enrollment, campaign_id)
    await _advance_and_poll(engine, factory, tenant, clock)
    async with factory() as session:
        attempts = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow).where(
                    rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
        )
        run_row = await session.get(rows.WorkflowRunRow, run3_id)
    assert len(attempts) == 3
    assert attempts[2].state == "sent"
    assert enrollment_row.state == "completed"
    assert enrollment_row.next_send_at is None
    assert run_row.status == "completed"
    assert sender_adapter.sent == ["gmail-fake-1", "gmail-fake-2", "gmail-fake-3"]


async def test_reply_stops_sequence_at_next_prepare(
    campaign_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_wf_reply", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    engine, sender_adapter, service = await _build_engine(
        factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock
    )
    rows = importlib.import_module("infra.db.tables")
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-reply")
    run1_id = await _start_run(engine, tenant, enrollment, campaign_id)
    await _advance_and_poll(engine, factory, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]

    # 回复在步间到达：下次 prepare 的竞态检查拦截（事件路径由回复管道负责）
    replies[(contact, account)] = ReplyStatusSnapshot(
        tenant, contact, account, ReplyState.REPLIED, NOW, NOW
    )
    clock.value = NOW + timedelta(days=1)
    enrollment = await _refresh_enrollment(service, tenant, enrollment.enrollment_id)
    run2_id = await _start_run(engine, tenant, enrollment, campaign_id)
    await _advance_and_poll(engine, factory, tenant, clock)
    async with factory() as session:
        run2_row = await session.get(rows.WorkflowRunRow, run2_id)
        run1_row = await session.get(rows.WorkflowRunRow, run1_id)
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
        )
    assert run2_row.status == "completed"  # 回复路径接管序列，本 run 正常收束
    assert run1_row.status == "completed"
    assert enrollment_row.state == "replied"
    assert sender_adapter.sent == ["gmail-fake-1"]


async def test_quota_exhausted_waits_then_resumes_next_day(
    campaign_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot] = {}
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot] = {}
    for index in range(2):
        account = ProspectAccountId(new_id("acc"))
        contact = ContactPointId(new_id("cp"))
        contacts[(contact, account)] = ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, f"basis_wf_q{index}", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
        replies[(contact, account)] = ReplyStatusSnapshot(
            tenant, contact, account, ReplyState.NO_REPLY, None, NOW
        )
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id, message_limit=1)
    clock = MutableClock(NOW)
    engine, sender_adapter, service = await _build_engine(
        factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock
    )
    first_key = next(iter(contacts))
    first = await _enroll(service, tenant, campaign_id, first_key[0], first_key[1], "wf-q1")
    # 每日新联系人额度 1：第二个 enrollment 次日入组（消息额度在发送日重查）
    clock.value = NOW + timedelta(days=1)
    second_key = next(iter(list(contacts)[1:]))
    second = await _enroll(service, tenant, campaign_id, second_key[0], second_key[1], "wf-q2")
    clock.value = NOW
    await _start_run(engine, tenant, first, campaign_id)
    await _start_run(engine, tenant, second, campaign_id)
    # 两条 run 同时推进：额度 1 → 一条发送、另一条 prepare 拒绝后退避重试
    await _advance_and_poll(engine, factory, tenant, clock)
    assert len(sender_adapter.sent) == 1
    # 次日额度重置 → 重试通过 → 另一条发送
    clock.value = NOW + timedelta(days=1)
    await _advance_and_poll(engine, factory, tenant, clock)
    assert len(sender_adapter.sent) == 2


async def test_paused_campaign_blocks_sends_until_reactivated(
    campaign_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(campaign_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_wf_pause", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    engine, sender_adapter, service = await _build_engine(
        factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock
    )
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-pause")
    await _start_run(engine, tenant, enrollment, campaign_id)
    # 暂停发生在发送前：prepare 拒绝 → 退避重试，绝不发送
    boss = Actor("boss:campaign", OutreachScope(level=ScopeLevel.TENANT), "boss")
    await service.pause_campaign(  # type: ignore[attr-defined]
        tenant, campaign_id, "paused-for-workflow", actor=boss
    )
    await _advance_and_poll(engine, factory, tenant, clock)
    assert sender_adapter.sent == []
    # 跨天仍不发：重试持续失败
    clock.value = NOW + timedelta(days=3)
    await _advance_and_poll(engine, factory, tenant, clock)
    assert sender_adapter.sent == []
    # 恢复激活 → prepare 通过 → 发送继续
    await service.activate_campaign(  # type: ignore[attr-defined]
        tenant, campaign_id, actor=boss
    )
    await _advance_and_poll(engine, factory, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]
