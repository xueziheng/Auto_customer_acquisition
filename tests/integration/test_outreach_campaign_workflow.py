"""outreach_campaign 工作流经真实 PostgreSQL 引擎的序列自动推进验收。

严格按 workflows/outreach_campaign/AGENTS.md：每个 enrollment 一条 run
（subject_ref = enrollment_id），5 步 draft_content → prepare_send → send →
record_sent → wait_for_reply；wait_for_reply 是真实 WAITING_EVENT(ReplyReceived)，
超时 = 该 enrollment 当前步骤 wait_days（域 next_send_at）；身份不可用
wait_event(SendingIdentityActivated)；暂停/额度等非终态拒绝 → 确定性超时复查，
绝无指数退避休眠。客户可见内容全英文。

TDD RED：5 步单 run 流程尚不存在，本文件导入即失败。
"""

from __future__ import annotations

import importlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
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

_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


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
    step_count: int = 3,
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
        daily_new_contact_limit=min(5, message_limit),
        daily_total_message_limit=message_limit,
        handoff_triggers=(),
    )
    campaign = Campaign(
        tenant, campaign_id, CampaignState.ACTIVE, 1, APPROVER, NOW,
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
    *,
    senders: FakeSenders | None = None,
):
    trace = Trace()
    approvals = FakeApprovals(trace)
    approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
        tenant, campaign_id, 1, approval_id, CampaignApprovalState.APPROVED, APPROVER, NOW
    )
    if senders is None:
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
    *,
    senders: FakeSenders | None = None,
) -> tuple[object, _FakeSender, object]:
    service = _service(
        factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock,
        senders=senders,
    )
    sender_adapter = _FakeSender(service, tenant)
    flow = importlib.import_module("workflows.outreach_campaign.flow")
    engine_type = importlib.import_module("infra.db.workflow_engine").PostgresWorkflowEngine
    engine = engine_type(
        factory,
        {**flow.build_outreach_campaign_handlers(service, sender_adapter, clock.now)},
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


async def _advance_and_poll(
    engine: object, tenant: TenantId, clock: MutableClock, times: int = 12
) -> None:
    # 每个等待步骤的首次到期 = 推进时钟时点 + 重试间隔：逐轮推时钟再轮询
    for _ in range(times):
        clock.value = clock.value + timedelta(seconds=1)
        await engine.poll_due(tenant, 10)  # type: ignore[attr-defined]


async def _start_run(
    engine: object, tenant: TenantId, enrollment: object, campaign_id: CampaignId
) -> str:
    """每个 enrollment 一条 run：幂等键 per enrollment（单 run 跨全序列）。"""
    return await engine.start(  # type: ignore[attr-defined]
        tenant,
        "outreach_campaign",
        str(enrollment.enrollment_id),
        {
            "enrollment_id": str(enrollment.enrollment_id),
            "campaign_id": str(campaign_id),
        },
        f"campaign:{enrollment.enrollment_id}:run1",
    )


async def _attempts(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> list[object]:
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        attempts = (
            await session.execute(
                select(rows.OutreachMessageAttemptRow).where(
                    rows.OutreachMessageAttemptRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    return list(attempts)


async def _enrollment_row(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    enrollment_id: str,
) -> object:
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        return await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), enrollment_id)
        )


async def _run_row(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, run_id: str
) -> object:
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        return await session.get(rows.WorkflowRunRow, run_id)


async def test_single_run_spans_all_steps_by_wait_days_then_completes(
    campaign_engine: AsyncEngine,
) -> None:
    """单 run ID 跨三步：wait_for_reply 超时按 wait_days 推进下一封；末步后 complete。"""
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
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-1")
    run_id = await _start_run(engine, tenant, enrollment, campaign_id)
    assert await _start_run(engine, tenant, enrollment, campaign_id) == run_id  # 幂等

    # 第 1 封：draft → prepare → send → record → wait_for_reply
    await _advance_and_poll(engine, tenant, clock)
    attempts = await _attempts(factory, tenant)
    enrollment_row = await _enrollment_row(factory, tenant, enrollment.enrollment_id)
    assert len(attempts) == 1
    assert attempts[0].state == "sent"
    assert attempts[0].provider_ref == "gmail-fake-1"
    assert enrollment_row.state == "in_sequence"
    assert enrollment_row.current_step == 1
    # 记录发生在轮询推进后的时钟点：next_send_at = 记录时刻 + 下一步 wait_days
    next_at = enrollment_row.next_send_at - NOW
    assert timedelta(days=1) - timedelta(seconds=30) <= next_at <= timedelta(days=1) + timedelta(seconds=30)
    assert sender_adapter.sent == ["gmail-fake-1"]

    # wait_days 真实时间推进：未到期前不推进（+12h 无新发送）
    clock.value = NOW + timedelta(hours=12)
    await _advance_and_poll(engine, tenant, clock)
    assert len(await _attempts(factory, tenant)) == 1

    # 第 2 封：跨过 wait_days 后 wait_for_reply 超时 → 下一封（同一 run）
    clock.value = NOW + timedelta(days=1)
    await _advance_and_poll(engine, tenant, clock)
    attempts = await _attempts(factory, tenant)
    enrollment_row = await _enrollment_row(factory, tenant, enrollment.enrollment_id)
    assert len(attempts) == 2
    assert enrollment_row.current_step == 2
    assert sender_adapter.sent == ["gmail-fake-1", "gmail-fake-2"]

    # 第 3 封 + 完成：末步发送后 enrollment completed，run completed（同一 run ID）
    clock.value = NOW + timedelta(days=2)
    await _advance_and_poll(engine, tenant, clock)
    attempts = await _attempts(factory, tenant)
    enrollment_row = await _enrollment_row(factory, tenant, enrollment.enrollment_id)
    run_row = await _run_row(factory, tenant, run_id)
    assert len(attempts) == 3
    assert attempts[2].state == "sent"
    assert enrollment_row.state == "completed"
    assert enrollment_row.next_send_at is None
    assert run_row.status == "running"  # 末步回复窗口未过，run 尚未收束
    assert sender_adapter.sent == ["gmail-fake-1", "gmail-fake-2", "gmail-fake-3"]
    # 末步回复窗口超时 → draft 终态检查 → run complete
    clock.value = NOW + timedelta(days=3)
    await _advance_and_poll(engine, tenant, clock)
    run_row = await _run_row(factory, tenant, run_id)
    assert run_row.status == "completed"
    assert len(await _attempts(factory, tenant)) == 3


async def test_reply_received_completes_run_immediately_and_no_more_sends(
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
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-reply")
    run_id = await _start_run(engine, tenant, enrollment, campaign_id)
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]
    # 回复到达 wait_for_reply → 立即 complete，不再发
    accepted = await engine.deliver_event(  # type: ignore[attr-defined]
        tenant, run_id, "ReplyReceived", {"occurred_at": NOW.isoformat()}
    )
    assert accepted is True
    run_row = await _run_row(factory, tenant, run_id)
    assert run_row.status == "completed"
    # 跨越多天也绝不再发
    clock.value = NOW + timedelta(days=5)
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]
    assert len(await _attempts(factory, tenant)) == 1


async def test_identity_unavailable_waits_for_sending_identity_activated(
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
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_wf_identity", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    trace = Trace()
    senders = FakeSenders(
        {
            sender: SendingIdentityEligibilitySnapshot(
                tenant, sender, OutreachSenderRole.COLD_OUTREACH, True, True, 100, NOW
            )
        },
        trace,
    )
    engine, sender_adapter, service = await _build_engine(
        factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock,
        senders=senders,
    )
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-identity")
    run_id = await _start_run(engine, tenant, enrollment, campaign_id)
    # 翻转快照：身份不可用 → prepare 拒绝 → wait_event(SendingIdentityActivated)
    senders.snapshots[sender] = SendingIdentityEligibilitySnapshot(
        tenant, sender, OutreachSenderRole.COLD_OUTREACH, False, True, 100, NOW
    )
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == []
    run_row = await _run_row(factory, tenant, run_id)
    assert run_row.status == "running"
    assert run_row.retry_count == 0  # wait 不是重试：无指数退避
    # 身份可用 → SendingIdentityActivated 唤醒 → 继续发送
    senders.snapshots[sender] = SendingIdentityEligibilitySnapshot(
        tenant, sender, OutreachSenderRole.COLD_OUTREACH, True, True, 100, NOW
    )
    accepted = await engine.deliver_event(  # type: ignore[attr-defined]
        tenant, run_id, "SendingIdentityActivated", {"occurred_at": NOW.isoformat()}
    )
    assert accepted is True
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]
    assert len(await _attempts(factory, tenant)) == 1


async def test_quota_exhausted_stops_then_resumes_next_day(
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
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id,
                         message_limit=1, step_count=2)
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
    run1 = await _start_run(engine, tenant, first, campaign_id)
    await _advance_and_poll(engine, tenant, clock)
    # 第 1 天：额度 1 → run1 发第 1 封；run2 prepare 拒绝 → wait（非失败）
    assert len(sender_adapter.sent) == 1
    clock.value = NOW + timedelta(days=1)
    run2 = await _start_run(engine, tenant, second, campaign_id)
    await _advance_and_poll(engine, tenant, clock)
    # 第 2 天：run1 发第 2 封（run 完成）；run2 额度拒绝 → wait
    assert len(sender_adapter.sent) == 2
    run2_row = await _run_row(factory, tenant, run2)
    assert run2_row.status == "running"
    assert run2_row.retry_count == 0
    # 第 3 天：run1 超时复查 → 额度重置 → 发送
    clock.value = NOW + timedelta(days=2)
    await _advance_and_poll(engine, tenant, clock)
    assert len(sender_adapter.sent) == 3
    # 第 4 天：run2 超时复查 → 补发第 2 封；两 run 经末步回复窗口后收束
    clock.value = NOW + timedelta(days=3)
    await _advance_and_poll(engine, tenant, clock)
    assert len(sender_adapter.sent) == 4
    # 第 5 天：run2 末步回复窗口超时 → 收束
    clock.value = NOW + timedelta(days=4)
    await _advance_and_poll(engine, tenant, clock)
    assert len(sender_adapter.sent) == 4
    run1_row = await _run_row(factory, tenant, run1)
    run2_row = await _run_row(factory, tenant, run2)
    assert run1_row.status == "completed"
    assert run2_row.status == "completed"


async def test_paused_campaign_blocks_without_backoff_until_recheck(
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
    run_id = await _start_run(engine, tenant, enrollment, campaign_id)
    boss = Actor("boss:campaign", OutreachScope(level=ScopeLevel.TENANT), "boss")
    await service.pause_campaign(  # type: ignore[attr-defined]
        tenant, campaign_id, "paused-for-workflow", actor=boss
    )
    # 暂停 → prepare 拒绝 → wait（确定性复查，不是指数退避轮询；run 不失败）
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == []
    run_row = await _run_row(factory, tenant, run_id)
    assert run_row.status == "running"
    assert run_row.retry_count == 0
    # 跨 3 天仍不发；恢复激活后下一次复查通过并发送
    clock.value = NOW + timedelta(days=3)
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == []
    await service.activate_campaign(  # type: ignore[attr-defined]
        tenant, campaign_id, actor=boss
    )
    # 生产恢复由驱动起新 run（立即 prepare）；本测试验证无驱动时的兜底：
    # 确定性复查（非退避）在下一次截止到达后通过并发送
    clock.value = NOW + timedelta(days=4, hours=1)
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]


async def test_customer_facing_content_is_english_only(
    campaign_engine: AsyncEngine,
) -> None:
    """客户可见 subject/body 全英文：模板与 run context 均无中文字符。"""
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
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_wf_lang", True,
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
    enrollment = await _enroll(service, tenant, campaign_id, contact, account, "wf-enroll-lang")
    run_id = await _start_run(engine, tenant, enrollment, campaign_id)
    await _advance_and_poll(engine, tenant, clock)
    assert sender_adapter.sent == ["gmail-fake-1"]
    run_row = await _run_row(factory, tenant, run_id)
    draft = (run_row.context or {}).get("draft")
    assert isinstance(draft, dict)
    subject = draft.get("subject")
    body = draft.get("body")
    assert isinstance(subject, str) and isinstance(body, str)
    assert _CJK.search(subject) is None, f"subject 含中文：{subject}"
    assert _CJK.search(body) is None, f"body 含中文：{body}"
    assert subject and body  # 非空英文内容
