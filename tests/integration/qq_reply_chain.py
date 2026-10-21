"""原发送API→QQ→Gateway/原件/Outbox→模型Gateway→原工作项→通知worker的受控链。
只替换邮箱、模型、对象传输外部端口；数据库和流程均为项目实现。
"""
import json
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

from pydantic import SecretStr
from sqlalchemy import func, select, update

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from apps.composition_support.employee_readers import employee_service_scope
from apps.notification_worker.config import NotificationWorkerConfig
from apps.notification_worker.health import NotificationHealthState
from apps.notification_worker.runtime import (
    ControlledInAppRoutingPolicy,
    NotificationRuntimeMode,
    NotificationWorkerRuntime,
    run_notification_worker,
)
from apps.scheduler_worker.bootstrap import CurrentNotificationAudience
from apps.scheduler_worker.inbound_driver import InboundDriver
from apps.scheduler_worker.notification_projection import NotificationProjectionHandler
from apps.scheduler_worker.reply_binding import ReplyRuntimeResources
from apps.scheduler_worker.reply_composition import CurrentEmployeeReplyFactory
from apps.scheduler_worker.reply_events import ReplyQualificationEventHandlers
from apps.scheduler_worker.reply_model_binding import (
    BoundReplyClassifier,
    ReplyModelAuthority,
)
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.qq_mail.inbound import QQInboundReader, decode_cursor, initial_cursor
from domains.assistant.schemas import AssistantActor
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import Actor as EA
from domains.employees.permissions import (
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from domains.outreach.permissions import Actor as OA
from domains.outreach.permissions import (
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from domains.outreach.service_impl import OutreachServiceImpl
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.email_inbound_uow import SqlAlchemyInboundPageUnitOfWork
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.qq_inbound import PostgresProjectReplyScope
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.repositories.email_inbound import InboundStore
from infra.db.repositories.in_app_notifications import PostgresInAppNotificationStore
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.repositories.notifications import PostgresNotificationDedupStore
from infra.db.tables import (
    AgentTurnRow,
    ConversationReplyWorkRow,
    EmployeeRow,
    HandoffRow,
    MessageRow,
    ModelInvocationRow,
    OutboxEventRow,
    OutreachMessageAttemptRow,
    RawArtifactRow,
    ToolCallRow,
    WorkflowRunRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from infra.email_inbound_artifacts import InboundRawArtifactArchiver
from infra.qq_inbound_binding import CurrentQQBinding
from notification_gateway.channels.in_app import InAppChannel
from notification_gateway.router import NotificationRouter
from notification_gateway.templates import FixedNotificationTemplateRenderer
from shared.events.catalog import InboundMessageStored, ReplyReceived
from shared.schemas.email_inbound import MIME_BYTES, InboundError, InboundRoute
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import ModelResponse, ModelUsage
from tests.integration.test_model_usage import limits
from tests.unit.test_qq_inbound import Session, mail
from tool_gateway.checks.email_inbound import InboundTenantCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_inbound import (
    MANIFEST,
    EmailInboundFetchHandler,
    ToolGatewayEmailInboundReader,
)
from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
from tool_gateway.handlers.model_generate import GatewayModelGenerator
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway
from workflows.reply_qualification.flow import (
    build_reply_qualification_definition,
    build_reply_qualification_handlers,
)
from workflows.reply_qualification.inbound import InboundPageProcessor


class Blobs:
    def __init__(self):
        self.values = {}

    async def put(self, object_key, content):
        self.values[object_key] = content

    async def get(self, object_key):
        return self.values[object_key]

    async def delete(self, object_key):
        self.values.pop(object_key, None)

    async def get_bounded(self, object_key, *, maximum_bytes):
        value = self.values[object_key]
        assert len(value) <= maximum_bytes
        return value


class Model:
    def __init__(self, category):
        self.category, self.calls = category, []

    async def generate(self, request):
        self.calls.append(request)
        return ModelResponse(text=json.dumps({"category": self.category, "candidate_fields": []}),
            model=request.model, usage=ModelUsage(input_tokens=20, cached_input_tokens=0, output_tokens=10))

    async def aclose(self):
        pass


async def exercise_reply(*, sessions, tenant, owner, boss, user, sender, attempt, cfg,
                         deps, client, headers, category, now):
    from domains.conversations.aliyun_connection import AliyunActor

    async with sessions() as db:
        sent = await db.scalar(select(OutreachMessageAttemptRow).where(
            OutreachMessageAttemptRow.tenant_id == tenant, OutreachMessageAttemptRow.attempt_id == attempt))
        assert sent.state == "sent"
        outbound = sent.deterministic_message_id
        boss_user = await db.scalar(select(EmployeeRow.user_id).where(
            EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == boss))
    route = InboundRoute(tenant_id=tenant, configured_identity_id=sender,
        mailbox_alias="qq-project-reply", route_id=outbound[1:].split(".")[0], config_version="fixture-v1")
    binding = CurrentQQBinding(cfg.mailbox_directory,
        AliyunActor(tenant_id=tenant, employee_id=owner, user_id=user), cfg.address)
    scope = PostgresProjectReplyScope(sessions, route, owner, binding, attempt_id=attempt)
    bodies = {"clear_interest": "I am interested in purchasing your products.",
              "willing_to_continue": "Please add my WhatsApp so we can talk.",
              "no_current_need": "We have no current need. Thank you."}
    class QuerySession(Session):
        async def search_replies(self, *args):
            result = await super().search_replies(*args)
            if category == "search_unsupported":
                raise InboundError()
            if category == "search_network":
                raise OSError("fixture unavailable")
            if category == "search_excessive":
                return tuple(range(1, 102))
            if category == "search_validity":
                self.validity += 1
            return result
    qq = QuerySession({1: mail(reply="<unrelated@qq.com>", body="PRIVATE-UNRELATED-BODY"),
                       2: mail(reply=outbound, body=bodies.get(category, "Fixture direct reply"))})
    blobs = Blobs()
    raw_store = RawArtifactStoreImpl(lambda t: SqlAlchemyArtifactUnitOfWork(sessions, t),
        blobs, MIME_BYTES, now, new_id, bounded_transport=blobs)
    slot, fingerprints = InboundPageSlot(), HmacFingerprintProvider("v1", b"x" * 32)
    registry = ToolRegistry()
    registry.register(MANIFEST, EmailInboundFetchHandler(route,
        lambda: QQInboundReader(route, owner, qq, scope, address=cfg.address),
        InboundRawArtifactArchiver(raw_store, raw_store), CredentialMarkerGuard(), slot,
        fingerprints, cursor_validator=decode_cursor))
    async def allowed(ctx, state):
        return ctx.tenant_id == tenant and ctx.user_id == user and state.manifest.tool_id == MANIFEST.tool_id
    gateway = ToolGateway(registry, {"tenant": InboundTenantCheck(route), "permission": PermissionCheck(allowed)},
        lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t, now=now),
        lease_duration=timedelta(seconds=60), lease_owner="fixture-qq-chain", now=now, id_factory=new_id)
    reader = ToolGatewayEmailInboundReader(gateway, slot, user, route, cursor_validator=decode_cursor)
    store = InboundStore(sessions, tenant, route.mailbox_alias, now=now)
    first = await store.bind(route, initial_cursor(route, attempt_id=attempt), now(), int(now().timestamp()), boss)
    if category.startswith("search_"):
        class NeverProcess:
            async def process(self, *args):
                raise AssertionError("失败查询不得提交正文或启动回复分类")
        composition = SimpleNamespace(store=store, reader_for=lambda _route: reader,
                                      processor=NeverProcess())
        driver = InboundDriver(composition, now=now)
        assert await driver.scan_once() == 0
        blocked = await store.read_cursor()
        assert blocked.blocked_reason == "provider_permanent"
        assert blocked.next_retry_at is None and blocked.cursor == first.cursor
        assert await driver.scan_once() == 0
        # 新Store和Driver重读同一数据库，时钟前进也不能绕过持久阻断。
        restarted_store = InboundStore(sessions, tenant, route.mailbox_alias, now=now)
        restarted = InboundDriver(SimpleNamespace(store=restarted_store,
            reader_for=lambda _route: reader, processor=NeverProcess()),
            now=lambda: now() + timedelta(days=1))
        assert await restarted.scan_once() == 0
        assert qq.opens == 1 and len(qq.searches) == 1
        assert not qq.read_headers and not qq.downloads and not blobs.values
        async with sessions() as db:
            calls = (await db.scalars(select(ToolCallRow).where(
                ToolCallRow.tenant_id == tenant, ToolCallRow.tool_id == MANIFEST.tool_id))).all()
            assert len(calls) == 1 and calls[0].status == "failed_permanent"
            for row in (RawArtifactRow, ModelInvocationRow, ConversationReplyWorkRow):
                assert await db.scalar(select(func.count()).select_from(row).where(row.tenant_id == tenant)) == 0
            assert await db.scalar(select(func.count()).select_from(MessageRow).where(
                MessageRow.tenant_id == tenant, MessageRow.direction == "inbound")) == 0
            assert await db.scalar(select(func.count()).select_from(OutboxEventRow).where(
                OutboxEventRow.tenant_id == tenant,
                OutboxEventRow.event_type.in_(("InboundMessageStored", "ReplyReceived")))) == 0
        return
    page = await reader.fetch(tenant, route.mailbox_alias, first.cursor, 20)
    assert qq.downloads == [2] and len(page.items) == 1
    assert qq.read_headers == [2]
    assert qq.searches == [(attempt, outbound, 0, 2)]
    assert list(blobs.values.values()) == [qq.messages[2]]
    def outreach_builder(factory, audit):
        return OutreachServiceImpl(factory, None, None, None, None,
            Phase1OutreachAuthorizer(tenant), audit, now=now)
    def inbound_actor(identity):
        return OA("system:email-inbound", OutreachScope(level=ScopeLevel.SYSTEM,
            allowed_sending_identity_ids=frozenset({identity})), "system")
    processor = InboundPageProcessor(store, lambda expected: SqlAlchemyInboundPageUnitOfWork(
        store, expected, outreach_builder=outreach_builder, audit_sink=StandardAuditLogger()), inbound_actor)
    assert await processor.process(first, page) == "committed"
    assert await processor.process(first, page) == "replayed"
    checkpoint = await store.read_cursor()
    after = await reader.fetch(tenant, route.mailbox_alias, checkpoint.cursor, 20)
    assert not after.items and qq.downloads == [2]

    employee_scope = partial(employee_service_scope, sessions, now=now,
        authorizer=Phase1EmployeeAuthorizer(tenant), audit=StandardAuditLogger())
    lookup = EA("system:reply-model", EmployeeScope.SYSTEM, "system")
    config_repo = SqlModelConfigurationRepository(sessions, fingerprints, now=now)
    configuration = ModelConfigurationServiceImpl(config_repo,
        CurrentEmployeeIdentity(employee_scope, lookup), now=now)
    policy = limits(tenant_calls=1, employee_calls=1, max_input_bytes=65536, max_output_tokens=512)
    await config_repo.initialize(tenant, "reply-v1", "fixture-model", policy, True)
    for process in ("api", "scheduler"):
        await config_repo.register_process(tenant, process, "reply-v1", "fixture-" + process)
    actor = AssistantActor(tenant_id=tenant, employee_id=boss, user_id=boss_user)
    probe = await configuration.request_probe(actor, "fixture-probe")
    async with sessions.begin() as db:
        await db.execute(update(AgentTurnRow).where(AgentTurnRow.tenant_id == tenant,
            AgentTurnRow.turn_id == probe.turn_id).values(state="running"))
    await configuration.complete_probe(actor, probe.turn_id, "reply-v1")
    authority = ReplyModelAuthority(SqlReplyRunBindingReader(sessions), employee_scope,
        lookup, configuration, tenant_id=tenant, employee_id=boss, model="fixture-model")
    model = Model(category)
    generator = GatewayModelGenerator(authority=authority, usage=SqlModelUsageRepository(sessions),
        provider_factory=lambda: model, limits=policy, model="fixture-model", configuration_version="reply-v1",
        ledger_factory=lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        fingerprints=fingerprints, lease_owner="fixture-reply", lease_duration=timedelta(seconds=30))
    classifier = BoundReplyClassifier(authority, generator, model="fixture-model",
        configuration_version="reply-v1", max_output_tokens=512)
    # 原工厂在fixture中借用同一真实服务，不替换业务动作或当前授权。
    core = SimpleNamespace(employee_scope=employee_scope, conversations=deps.conversations)
    composition = CurrentEmployeeReplyFactory(tenant, boss, classifier=classifier)(
        core, deps.outreach, ReplyRuntimeResources(sessions, raw_store, None, now))
    workflow = PostgresWorkflowEngine(sessions, build_reply_qualification_handlers(
        classifier=composition.classifier, content_reader=composition.content_reader,
        input_guard=composition.input_guard, conversations=composition.conversations,
        outreach=composition.outreach, tenant_id=tenant, now=now,
        action_ports=composition.action_ports, classification_access=composition.classification_access), now=now)
    workflow.register(build_reply_qualification_definition())
    jobs = PostgresNotificationJobStore(sessions, now=now)
    projection = NotificationProjectionHandler(tenant_id=tenant,
        audience=CurrentNotificationAudience(tenant, employee_scope, None),
        jobs=jobs, conversations=deps.conversations, now=now)
    def deliverer():
        outbox = OutboxDeliverer(sessions, tenant, now=now)
        outbox.register_handler(InboundMessageStored, "reply_qualification.inbound", ReplyQualificationEventHandlers(
            engine=workflow, factory=sessions, tenant_id=tenant))
        outbox.register_handler(ReplyReceived, "notification.reply", projection)
        return outbox
    await deliverer().drain()
    for _ in range(6):
        if not await workflow.poll_due(tenant, 10):
            break
    await deliverer().drain()
    async with sessions() as db:
        runs = (await db.scalars(select(WorkflowRunRow).where(
            WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.workflow_type == "reply_qualification"))).all()
        assert len(runs) == 1 and runs[0].status == "completed", [(v.status, v.context) for v in runs]
        messages = (await db.scalars(select(MessageRow).where(
            MessageRow.tenant_id == tenant, MessageRow.direction == "inbound"))).all()
        assert len(messages) == 1
        message, conversation = messages[0].message_id, messages[0].conversation_id
        invocations = (await db.scalars(select(ModelInvocationRow).where(ModelInvocationRow.tenant_id == tenant))).all()
        assert len(invocations) == 1 and invocations[0].employee_id == boss
        assert invocations[0].state == "succeeded"
        assert await db.scalar(select(EmployeeRow.role).where(EmployeeRow.tenant_id == tenant,
            EmployeeRow.employee_id == owner)) == "sales"
        tools = (await db.scalars(select(ToolCallRow.tool_id).where(ToolCallRow.tenant_id == tenant))).all()
        assert {"email.send", "email.inbound.fetch", "inbox.message.evidence.read", "model.generate"} <= set(tools)
        assert await db.scalar(select(func.count()).select_from(RawArtifactRow).where(RawArtifactRow.tenant_id == tenant)) == 1
        events = (await db.scalars(select(OutboxEventRow).where(
            OutboxEventRow.tenant_id == tenant, OutboxEventRow.event_type.in_(("InboundMessageStored", "ReplyReceived"))))).all()
        assert len(events) == 2 and all(e.status == "delivered" for e in events)
    assert len(model.calls) == 1
    assert model.calls[0].payload["body"].strip() == bodies[category]
    assert "PRIVATE-UNRELATED" not in str(model.calls[0].payload)
    current = await deps.conversations.get_effective_reply_category(tenant, message)
    assert current.value == category

    positive = category != "no_current_need"
    queue = await client.get("/inbox/reply-work", params={"status":"pending"}, headers=headers)
    assert queue.status_code == 200
    assert len(queue.json()) == int(positive), queue.text
    if positive:
        work = queue.json()[0]
        assert work["message_id"] == message and work["can_handle"]
        denied = await client.post("/inbox/reply-work/" + work["action_id"],
            headers={**headers, "X-Employee-Id": boss}, json={"expected_status":"pending","status":"in_progress"})
        assert denied.status_code == 403
        for previous, target in (("pending", "in_progress"), ("in_progress", "completed")):
            command = {"expected_status":previous,"status":target}
            if target == "completed":
                command.update(disposition="handled", note="合成私有回复验收处理，非采购确认。", request_id="fixture-reply-handled")
            result = await client.post("/inbox/reply-work/" + work["action_id"], headers=headers, json=command)
            assert result.status_code == 200, result.text
            repeat = await client.post("/inbox/reply-work/" + work["action_id"], headers=headers, json=command)
            assert repeat.status_code == 200 and repeat.json() == result.json()

    notifications = PostgresInAppNotificationStore(sessions)
    router = NotificationRouter(PostgresNotificationDedupStore(sessions, now=now), ControlledInAppRoutingPolicy())
    router.register_channel(InAppChannel(notifications, now=now))
    config = NotificationWorkerConfig(database_url=SecretStr("fixture-only"), tenant_id=tenant,
        poll_interval_seconds=1, batch_limit=10, health_port=19090, lease_owner="fixture-notify")
    runtime = NotificationWorkerRuntime(jobs, router, FixedNotificationTemplateRenderer(), config,
        NotificationHealthState(delivery_mode="controlled_in_app"), NotificationRuntimeMode.CONTROLLED_IN_APP)
    async def stop_after_one(_seconds, stop):
        stop.set()
    result = await run_notification_worker(runtime, wait=stop_after_one)
    assert result.jobs_completed == int(positive)
    # 进程重建后的原游标、原Outbox、原workflow和通知账本保持幂等。
    await deliverer().drain()
    assert await workflow.poll_due(tenant, 10) == 0
    assert (await run_notification_worker(runtime, wait=stop_after_one)).jobs_completed == 0
    delivered = await notifications.list_for_recipient(tenant, boss, limit=10, before=None)
    assert len(delivered) == int(positive)
    if delivered:
        assert delivered[0].relative_link == f"/inbox/conversations/{conversation}"
        assert delivered[0].context.reason_code == category
    assert not await notifications.list_for_recipient(tenant, owner, limit=10, before=None)
    assert len(model.calls) == 1
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(HandoffRow).where(HandoffRow.tenant_id == tenant)) == 0
        assert await db.scalar(select(func.count()).select_from(ConversationReplyWorkRow).where(
            ConversationReplyWorkRow.tenant_id == tenant)) == int(positive)
