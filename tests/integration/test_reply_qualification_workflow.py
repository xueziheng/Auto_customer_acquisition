"""reply_qualification 工作流最小切片验收（真实 PostgreSQL）。

契约（workflows/reply_qualification/AGENTS.md + HANDBOOK 切片 6 验收）：
- classify：经只读 ``MessageContentReader`` 端口按 message_id 加载
  subject/body（仅内存，不落库/事件/日志），先过 ``InputContentGuard``，
  再交给注入的 ``ReplyClassifier`` 分类；随后
  conversations.record_classification 落库（带 outbound_message_id 关联 +
  模型版本），返回 REPLY_ACTIONS 动作；AUTO_REPLY 短路 complete。
- context 自报类别 fail-closed：workflow context 夹带 ``category`` 键时
  拒绝执行（无 Provenance、可伪造），不能绕过 reader/guard/model。
- apply_actions：按 REPLY_ACTIONS 经域服务逐个幂等执行（stop_sequence →
  outreach.stop_enrollment(REPLY)；suppress → outreach.add_suppression）；
  未接线动作显式 fail-closed（run FAILED 可观测），分类不丢。
- 事件时序：``ReplyReceived`` 是 record_classification 分类落库后才发布的
  结果事件，**不是**本流程触发（先分类才触发分类 = 循环）。本切片生产
  触发未接线：flow 只提供定义/handler。持久化断言：workflow context 与
  outbox 不得含正文/凭证（artifact_store 硬边界 4）。
- 输入护栏（硬边界 1）：内容送入 classifier/model port **之前**先过
  ``InputContentGuard``；凭证/API key/token-like 文本 fail-closed——port
  零调用、run 错误只含固定安全摘要、context/outbox 不含 secret。
- 幂等：同一 message_id 重复 start 同一 run（key ``reply:{message_id}``），
  ReplyReceived 事件投递不会再生新 run、不再分类（循环证明）。
"""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest_asyncio
from _pytest.logging import LogCaptureFixture
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
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
from workflows.reply_qualification.ports import ReplyMessageContent

_models = importlib.import_module("domains.outreach.models")
Campaign = _models.Campaign
CampaignBoundary = _models.CampaignBoundary
CampaignState = _models.CampaignState
CampaignVersion = _models.CampaignVersion
SequenceStepSpec = _models.SequenceStepSpec
StepIntent = _models.StepIntent
_si_models = importlib.import_module("domains.sending_identity.models")
DomainRole = _si_models.DomainRole

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
APPROVER = EmployeeId(new_id("emp"))

#: 正文 marker（成功路径，非凭证-like）：不得出现在 workflow context/outbox。
BODY_MARKER = "Please unsubscribe me from your emails. UNSUBSCRIBE-MARKER-77"
#: 凭证-like marker（仅护栏拒绝测试使用）：不得进入模型、context 或 outbox。
CRED_MARKER = "api_key=sk-test-reply-77"


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _FakeModelPort:
    """受控模型端口：固定返回指定类别（模拟生产 provider 输出）。"""

    def __init__(
        self,
        category: str,
        candidate_fields: list[dict[str, str]] | None = None,
    ) -> None:
        self._category = category
        self._candidate_fields = candidate_fields or []
        self.calls = 0

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        del system_prompt, message
        self.calls += 1
        return json.dumps(
            {
                "category": self._category,
                "candidate_fields": self._candidate_fields,
            }
        )


class _FakeContentReader:
    """只读内容端口 fake：按 message_id 返回内存中的 subject/body。"""

    def __init__(self, contents: dict[str, ReplyMessageContent]) -> None:
        self._contents = contents
        self.calls = 0

    async def load(
        self, tenant_id: object, message_id: str
    ) -> ReplyMessageContent | None:
        del tenant_id
        self.calls += 1
        return self._contents.get(str(message_id))


@pytest_asyncio.fixture
async def reply_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_identity(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, boss: EmployeeId
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
            address="reply-sender@example.test",
            domain="example.test",
            role=DomainRole.COLD_OUTREACH,
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
            check_ref="auth_reply_1",
        ),
        actor=SendingIdentityActor(
            "system:reply-test",
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
    boundary = CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(sender,),
        steps=(SequenceStepSpec(1, StepIntent.DISCOVERY, 0),),
        daily_new_contact_limit=5,
        daily_total_message_limit=7,
        handoff_triggers=(),
    )
    campaign = Campaign(
        tenant, campaign_id, CampaignState.ACTIVE, 1, APPROVER, NOW,
        approval_id=str(approval_id), approved_by=APPROVER, approved_at=NOW,
    )
    version = CampaignVersion(tenant, campaign_id, 1, "Reply discovery", boundary, APPROVER, NOW)
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        await uow.campaigns.add(campaign, version)


def _outreach_service(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    approval_id: ApprovalId,
    sender: SendingIdentityId,
    contacts: dict[tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot],
    replies: dict[tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot],
    clock: MutableClock,
) -> object:
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


def _conversations_service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, clock: MutableClock
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


async def _enroll(
    service: object,
    tenant: TenantId,
    campaign_id: CampaignId,
    contact: ContactPointId,
    account: ProspectAccountId,
) -> object:
    boss = OutreachActor(
        "boss:reply-test", OutreachScope(level=OutreachScopeLevel.TENANT), "boss"
    )
    return await service.enroll(  # type: ignore[attr-defined]
        tenant,
        campaign_id,
        EnrollmentCreateRequest(account, contact, IdempotencyKey(f"reply-{account}")),
        actor=boss,
    )


def _reply_run_context(
    message_id: str,
    outbound_message_id: str,
    enrollment_id: str,
    account_id: str,
    contact_point_id: str,
    *,
    category: str | None = None,
) -> dict[str, object]:
    # 只携带 typed ID；正文/主题经 MessageContentReader 端口内存加载
    return {
        "message_id": message_id,
        "outbound_message_id": outbound_message_id,
        "category": category,
        "enrollment_id": enrollment_id,
        "account_id": account_id,
        "contact_point_id": contact_point_id,
    }


async def _start_reply_run(
    engine: object,
    tenant: TenantId,
    context: dict[str, object],
) -> str:
    return await engine.start(  # type: ignore[attr-defined]
        tenant,
        "reply_qualification",
        context["message_id"],
        context,
        f"reply:{context['message_id']}",
    )


async def _poll(engine: object, tenant: TenantId) -> None:
    for _ in range(10):
        count = await engine.poll_due(tenant, 10)  # type: ignore[attr-defined]
        if count == 0:
            return


async def _build_engine(
    flow: object,
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    outreach: object,
    conversations: ConversationService,
    port: _FakeModelPort,
    reader: _FakeContentReader,
    clock: MutableClock,
) -> object:
    classifier = QualificationAgent(
        model="reply-test-model-v1", model_client=port, gateway=None, guardrails=None
    )
    guard_type = importlib.import_module(
        "agent_runtime.guardrails.input_guard"
    ).CredentialMarkerGuard
    engine_type = importlib.import_module("infra.db.workflow_engine").PostgresWorkflowEngine
    engine = engine_type(
        factory,
        flow.build_reply_qualification_handlers(
            classifier=classifier,
            content_reader=reader,
            input_guard=guard_type(),
            conversations=conversations,
            outreach=outreach,
            tenant_id=tenant,
            now=clock.now,
        ),
        now=clock.now,
    )
    engine.register(flow.build_reply_qualification_definition())
    return engine


async def _assert_no_content_leak(
    session: AsyncSession, tenant: TenantId, run_id: str
) -> None:
    """持久化面断言：workflow context 与 outbox 均不含正文/凭证 marker。"""
    rows = importlib.import_module("infra.db.tables")
    run_row = await session.get(rows.WorkflowRunRow, run_id)
    context_dump = json.dumps(run_row.context)
    assert BODY_MARKER not in context_dump
    assert CRED_MARKER not in context_dump
    assert "SECRET-BODY-MARKER-77" not in context_dump
    outbox = (
        await session.execute(
            select(rows.OutboxEventRow).where(
                rows.OutboxEventRow.tenant_id == str(tenant),
                rows.OutboxEventRow.event_type == "ReplyReceived",
            )
        )
    ).scalars().all()
    assert outbox, "ReplyReceived 应已发布（结果事件）"
    for event in outbox:
        payload = json.dumps(event.event_payload)
        assert BODY_MARKER not in payload
        assert CRED_MARKER not in payload


async def test_reply_run_classifies_persists_and_applies_actions(
    reply_db: AsyncEngine,
) -> None:
    """最终业务预期：未预分类 → 只读端口加载原文 → agent 分类退订 → 留痕
    （含 outbound 关联+模型版本）→ stop_sequence 停序列 + suppress 抑制 →
    run complete；持久化 context/outbox 不含正文/凭证。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("unsubscribe")
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_001": ReplyMessageContent(
                subject="Unsubscribe request", body=BODY_MARKER
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    outbound_id = "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid"
    context = _reply_run_context(
        "msg_inbound_reply_001", outbound_id,
        str(enrollment.enrollment_id), str(account), str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
        )
        classification_row = await session.get(
            rows.ConversationClassificationRow,
            (str(tenant), "msg_inbound_reply_001"),
        )
        suppressions = (
            await session.execute(
                select(rows.OutreachSuppressionRow).where(
                    rows.OutreachSuppressionRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        await _assert_no_content_leak(session, tenant, run_id)
    assert run_row.status == "completed"
    assert port.calls == 1
    assert reader.calls == 1
    assert classification_row is not None
    assert classification_row.category == "unsubscribe"
    assert classification_row.classified_by == "reply-test-model-v1"
    assert enrollment_row.state == "replied"  # stop_sequence
    assert len(suppressions) == 1  # suppress


async def test_classification_persists_verbatim_field_evidence_without_workflow_leak(
    reply_db: AsyncEngine,
    caplog: LogCaptureFixture,
) -> None:
    """字段候选必须随分类耐久保存；workflow 只保留 ID，不复制客户原话。

    这条测试会捕获 ``ClassifyStep`` 丢弃 ``candidate_fields``、仓储未保存
    quote，或把 quote 塞进 workflow context 的任一回归。动作端口尚未装配时
    run 可以 fail-closed，但分类证据必须可供崩溃重试后按 message_id 重读。
    """
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant,
            contact,
            account,
            ContactVerificationStatus.VERIFIED,
            NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST,
            "basis_reply_evidence",
            True,
            "US",
            "importer",
            frozenset({"hardware"}),
            NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(
            tenant, contact, account, ReplyState.NO_REPLY, None, NOW
        )
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(
        factory,
        tenant,
        campaign_id,
        approval_id,
        sender,
        contacts,
        replies,
        clock,
    )
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    quote = "We need 5000 stainless steel hinges."
    port = _FakeModelPort(
        "provides_specification",
        [
            {"field": "product_category", "value": "hinges", "quote": quote},
            {"field": "quantity", "value": "5000", "quote": quote},
        ],
    )
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_evidence": ReplyMessageContent(
                subject="Hinge requirements", body=quote
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    context = _reply_run_context(
        "msg_inbound_reply_evidence",
        "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid",
        str(enrollment.enrollment_id),
        str(account),
        str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        classification_row = await session.get(
            rows.ConversationClassificationRow,
            (str(tenant), "msg_inbound_reply_evidence"),
        )
        outbox_payloads = (
            await session.execute(
                select(rows.OutboxEventRow.event_payload).where(
                    rows.OutboxEventRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    assert classification_row is not None
    assert getattr(classification_row, "candidate_fields", None) == [
        {"field": "product_category", "value": "hinges", "quote": quote},
        {"field": "quantity", "value": "5000", "quote": quote},
    ]
    assert quote not in json.dumps(run_row.context)
    assert quote not in json.dumps(outbox_payloads)
    assert all(quote not in record.getMessage() for record in caplog.records)

    repeated_run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)
    assert repeated_run_id == run_id
    assert port.calls == 1


async def test_production_evidence_reader_reloads_tenant_bound_business_evidence(
    reply_db: AsyncEngine,
) -> None:
    """崩溃后只凭 tenant/message ID 重读分类字段与 artifact 引用。"""
    from domains.conversations.schemas import ReplyCategory, ReplyFieldEvidence
    from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
    from shared.schemas.identifiers import OutboundMessageId

    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    clock = MutableClock(NOW)
    conversations = _conversations_service(factory, tenant, clock)
    quote = "We need 5000 stainless steel hinges."
    outbound_id = OutboundMessageId(
        f"<reply-route.{'c' * 64}@messages.tradeos.invalid>"
    )
    message_id = await conversations.ingest_inbound(
        tenant,
        None,
        account,
        "art_reply_evidence_reader",
        "<reply-evidence-reader@example.test>",
        NOW,
        outbound_message_id=outbound_id,
    )
    await conversations.record_classification(
        tenant,
        message_id,
        ReplyCategory.PROVIDES_SPECIFICATION,
        "reply-model-v3",
        outbound_message_id=outbound_id,
        candidate_fields=(
            ReplyFieldEvidence("quantity", "5000", quote),
        ),
    )

    try:
        reader_type = importlib.import_module(
            "apps.scheduler_worker.adapters.reply_evidence_reader"
        ).ConversationReplyEvidenceReader
    except ModuleNotFoundError:
        raise AssertionError("生产 ReplyEvidenceReader 尚未实现") from None
    reader = reader_type(
        lambda requested: SqlAlchemyConversationsUnitOfWork(
            factory, requested, now=clock.now
        )
    )

    snapshot = await reader.load(tenant, message_id)
    assert snapshot is not None
    assert snapshot.message_id == message_id
    assert snapshot.category == "provides_specification"
    assert snapshot.classified_by == "reply-model-v3"
    assert snapshot.classified_at == NOW
    assert snapshot.raw_artifact_ref == "art_reply_evidence_reader"
    assert [(item.field, item.value, item.quote) for item in snapshot.candidate_fields] == [
        ("quantity", "5000", quote)
    ]
    assert await reader.load(other_tenant, message_id) is None


async def test_context_category_is_rejected_fail_closed(
    reply_db: AsyncEngine,
) -> None:
    """context 夹带自报类别（无 Provenance、可伪造）→ fail-closed 拒绝：
    不调 reader/guard/model、不落分类、不触发动作（enrollment 保持 enrolled）。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_ctx", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("rejection")  # 不应被调用
    reader = _FakeContentReader({})  # 不应被调用
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    context = _reply_run_context(
        "msg_inbound_reply_002",
        "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid",
        str(enrollment.enrollment_id), str(account), str(contact),
        category="unsubscribe",  # 伪造自报类别
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant), str(enrollment.enrollment_id))
        )
        classification_row = await session.get(
            rows.ConversationClassificationRow,
            (str(tenant), "msg_inbound_reply_002"),
        )
    assert run_row.status == "failed"
    assert port.calls == 0  # 伪造类别不能绕过模型
    assert reader.calls == 0  # 伪造类别不能绕过内容加载
    assert classification_row is None  # 未落分类
    assert enrollment_row.state == "enrolled"  # 未触发动作


async def test_auto_reply_short_circuits_without_actions(
    reply_db: AsyncEngine,
) -> None:
    """AUTO_REPLY：complete 且不停发/不抑制（自动回复不算回复）。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_auto", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("auto_reply")
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_003": ReplyMessageContent(
                subject="Out of office", body="I am on vacation until Monday."
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    context = _reply_run_context(
        "msg_inbound_reply_003",
        "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid",
        str(enrollment.enrollment_id), str(account), str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
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
    assert run_row.status == "completed"
    assert enrollment_row.state == "enrolled"  # 不停发
    assert suppressions == []  # 不抑制


async def test_unwired_action_fails_closed_with_classification_persisted(
    reply_db: AsyncEngine,
) -> None:
    """handoff 等未接线动作：run 显式 FAILED（可观测），分类已留痕不丢。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_quote", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("requests_quote")
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_004": ReplyMessageContent(
                subject="Price inquiry", body="Please send me a quote for hinges."
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    context = _reply_run_context(
        "msg_inbound_reply_004",
        "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid",
        str(enrollment.enrollment_id), str(account), str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        classification_row = await session.get(
            rows.ConversationClassificationRow,
            (str(tenant), "msg_inbound_reply_004"),
        )
    assert run_row.status == "failed"
    assert run_row.last_error is not None
    assert classification_row is not None  # 分类不丢
    assert classification_row.category == "requests_quote"


async def test_reply_received_does_not_retrigger_classification(
    reply_db: AsyncEngine,
) -> None:
    """循环证明：ReplyReceived 是分类落库后的结果事件，不是本流程触发。

    同一 message_id 重复 start → 返回同一 run（幂等键 no-op），模型不再被
    调用、不再新增分类/抑制；向已完成 run 投递 ReplyReceived 事件 → 无推进。
    """
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_loop", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("unsubscribe")
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_005": ReplyMessageContent(
                subject="Unsubscribe request", body=BODY_MARKER
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    outbound_id = "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid"
    context = _reply_run_context(
        "msg_inbound_reply_005", outbound_id,
        str(enrollment.enrollment_id), str(account), str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        assert run_row.status == "completed"
        await _assert_no_content_leak(session, tenant, run_id)

    # 同一 message 再次 start（模拟触发重放）：同一 run，模型/内容不再调用
    again = await _start_reply_run(engine, tenant, context)
    assert again == run_id
    assert port.calls == 1
    assert reader.calls == 1

    # 向已完成 run 投递 ReplyReceived 事件：本流程无 WAITING_EVENT，不推进
    advanced = await engine.deliver_event(  # type: ignore[attr-defined]
        tenant,
        run_id,
        "ReplyReceived",
        {"message_id": "msg_inbound_reply_005", "reply_category": "unsubscribe"},
    )
    assert advanced is False

    async with factory() as session:
        classification_rows = (
            await session.execute(
                select(rows.ConversationClassificationRow).where(
                    rows.ConversationClassificationRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
        run_rows = (
            await session.execute(
                select(rows.WorkflowRunRow).where(
                    rows.WorkflowRunRow.tenant_id == str(tenant),
                    rows.WorkflowRunRow.workflow_type == "reply_qualification",
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
    assert len(classification_rows) == 1  # 不重复分类
    assert len(run_rows) == 1  # 不重复起 run
    assert len(suppressions) == 1  # 不重复抑制


async def test_credential_content_fails_closed_before_model(
    reply_db: AsyncEngine,
) -> None:
    """硬边界 1：content_reader 返回凭证/API key-like 文本 → 输入护栏在
    classifier/model port 之前 fail-closed：port 零调用，run FAILED 且
    last_error 只含固定安全摘要（不回显内容），context/outbox 不含 secret。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_cred", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("unsubscribe")
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_006": ReplyMessageContent(
                subject="Re: inquiry",
                body="Here is my key: " + CRED_MARKER,
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    context = _reply_run_context(
        "msg_inbound_reply_006",
        "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid",
        str(enrollment.enrollment_id), str(account), str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        classification_row = await session.get(
            rows.ConversationClassificationRow,
            (str(tenant), "msg_inbound_reply_006"),
        )
        outbox = (
            await session.execute(
                select(rows.OutboxEventRow).where(
                    rows.OutboxEventRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    assert port.calls == 0  # 凭证内容绝不进模型
    assert run_row.status == "failed"
    assert run_row.last_error is not None
    assert CRED_MARKER not in run_row.last_error  # 固定安全摘要，不回显
    assert CRED_MARKER not in json.dumps(run_row.context)
    assert classification_row is None  # 未分类：护栏先于落库
    for event in outbox:
        assert CRED_MARKER not in json.dumps(event.event_payload)


async def test_no_subject_uses_fixed_placeholder(
    reply_db: AsyncEngine,
) -> None:
    """无主题消息：进模型前归一为固定非敏感占位，不因缺主题运行时失败。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_nosubj", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant, boss)
    await _seed_campaign(factory, tenant, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant, campaign_id, contact, account)
    conversations = _conversations_service(factory, tenant, clock)
    port = _FakeModelPort("rejection")
    reader = _FakeContentReader(
        {
            "msg_inbound_reply_007": ReplyMessageContent(
                subject=None, body="No thank you."
            )
        }
    )
    flow = importlib.import_module("workflows.reply_qualification.flow")
    engine = await _build_engine(
        flow, factory, tenant, outreach, conversations, port, reader, clock
    )
    context = _reply_run_context(
        "msg_inbound_reply_007",
        "route-v1.0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef@messages.tradeos.invalid",
        str(enrollment.enrollment_id), str(account), str(contact),
    )
    run_id = await _start_reply_run(engine, tenant, context)
    await _poll(engine, tenant)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        run_row = await session.get(rows.WorkflowRunRow, run_id)
        classification_row = await session.get(
            rows.ConversationClassificationRow,
            (str(tenant), "msg_inbound_reply_007"),
        )
    assert run_row.status == "completed"
    assert port.calls == 1  # 占位主题进入模型，分类照常
    assert classification_row is not None
    assert classification_row.category == "rejection"


async def test_cross_tenant_apply_actions_fails_closed(
    reply_db: AsyncEngine,
) -> None:
    """硬边界 8：ApplyActionsStep 绑定 tenant A，收到 tenant B 的 run →
    TenantIsolationViolation，不执行任何动作。"""
    factory = async_sessionmaker(reply_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    boss = EmployeeId(new_id("emp"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    contacts = {
        (contact, account): ContactEligibilitySnapshot(
            tenant_a, contact, account, ContactVerificationStatus.VERIFIED, NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST, "basis_reply_x", True,
            "US", "importer", frozenset({"hardware"}), NOW,
        )
    }
    replies = {
        (contact, account): ReplyStatusSnapshot(tenant_a, contact, account, ReplyState.NO_REPLY, None, NOW)
    }
    sender = await _seed_identity(factory, tenant_a, boss)
    await _seed_campaign(factory, tenant_a, campaign_id, sender, approval_id)
    clock = MutableClock(NOW)
    outreach = _outreach_service(factory, tenant_a, campaign_id, approval_id, sender, contacts, replies, clock)
    enrollment = await _enroll(outreach, tenant_a, campaign_id, contact, account)
    step_type = importlib.import_module(
        "workflows.reply_qualification.steps"
    ).ApplyActionsStep
    runner_type = importlib.import_module("workflows.engine.runner")
    step = step_type(outreach, tenant_a, clock.now)
    from shared.errors import TenantIsolationViolation
    from workflows.engine.runner import StepStatus

    foreign_run = runner_type.WorkflowRun(
        run_id=runner_type.RunId("run_foreign"),
        tenant_id=tenant_b,
        workflow_type="reply_qualification",
        workflow_version=1,
        subject_ref="msg_inbound_reply_008",
        current_step="apply_actions",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context={
            "actions": ["stop_sequence", "suppress"],
            "category": "unsubscribe",
            "enrollment_id": str(enrollment.enrollment_id),
            "account_id": str(account),
            "contact_point_id": str(contact),
            "message_id": "msg_inbound_reply_008",
        },
    )
    try:
        await step.execute(foreign_run)
        raise AssertionError("跨租户 execute 应抛 TenantIsolationViolation")
    except TenantIsolationViolation:
        pass
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        enrollment_row = await session.get(
            rows.OutreachEnrollmentRow, (str(tenant_a), str(enrollment.enrollment_id))
        )
        suppressions = (
            await session.execute(
                select(rows.OutreachSuppressionRow).where(
                    rows.OutreachSuppressionRow.tenant_id == str(tenant_a)
                )
            )
        ).scalars().all()
    assert enrollment_row.state == "enrolled"  # 未被停
    assert suppressions == []  # 未被抑制
