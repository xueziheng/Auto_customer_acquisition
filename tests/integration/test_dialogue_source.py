"""精确来源使用真实业务父记录；新来信即使日期回拨也作废旧计划。"""
from datetime import timedelta

import pytest_asyncio
from sqlalchemy import text

from shared.schemas.identifiers import new_id
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import (  # noqa: F401
    NOW,
    page_runtime,
    prepare_sent,
)


@pytest_asyncio.fixture
async def dialogue_source_runtime(page_runtime):  # noqa: F811
    from domains.conversations.dialogue_source import DialogueSource
    from infra.db.dialogue_source import SqlDialogueSources
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant, factory = runtime["route"].tenant_id, runtime["factory"]
    conversations = runtime["deps"].conversations
    message = await conversations.ingest_inbound(tenant, None, runtime["source_account_id"],
        new_id("art"), "<" + new_id("tst") + "@example.test>", NOW,
        outbound_message_id=runtime["outbound"])
    async with factory() as db:
        enrollment = (await db.execute(text("SELECT * FROM outreach_enrollments WHERE tenant_id=:t AND enrollment_id=:e"),
            {"t": tenant, "e": runtime["source_enrollment_id"]})).mappings().one()
        campaign = (await db.execute(text("SELECT approval_id FROM outreach_campaigns WHERE tenant_id=:t AND campaign_id=:c"),
            {"t": tenant, "c": enrollment["campaign_id"]})).one()
    owner = next(i.employee_id for i in runtime["staff"] if i.role == "boss")
    source = DialogueSource(tenant_id=tenant, binding_id=new_id("dlg"),
        account_id=runtime["source_account_id"], contact_point_id=enrollment["contact_point_id"],
        hypothesis_id=runtime["source_hypothesis_id"], sending_identity_id=runtime["route"].configured_identity_id,
        campaign_id=enrollment["campaign_id"], campaign_version=enrollment["campaign_version"],
        approval_id=campaign.approval_id, owner_employee_id=owner, ownership_locked_at=NOW,
        mailbox_operation_id="controlled-v1", source_kind="outreach", enrollment_id=runtime["source_enrollment_id"],
        business_message_id=None, admitted_message_id=message, admitted_source_sha256="a" * 64)
    reader = SqlDialogueSources(factory, tenant)
    async with factory.begin() as db:
        await reader.admit(db, source)
    return runtime, source, reader


async def test_source_binds_real_inbound_and_backdated_arrival_invalidates_previous(dialogue_source_runtime):
    from infra.db.dialogue_execution_tables import DialogueBindingRow
    runtime, source, reader = dialogue_source_runtime
    tenant, factory = runtime["route"].tenant_id, runtime["factory"]
    conversations, message = runtime["deps"].conversations, source.admitted_message_id
    assert await reader.for_message(tenant, message) == source
    assert await reader.for_sent_header(tenant, "<unknown@example.test>") is None
    newer = await conversations.ingest_inbound(tenant, None, source.account_id, new_id("art"),
        "<" + new_id("tst") + "@example.test>", NOW - timedelta(days=1), outbound_message_id=runtime["outbound"])
    async with factory.begin() as db:
        await reader.bind_message(db, source.binding_id, newer)
        await reader.bind_message(db, source.binding_id, message)
    async with factory() as db:
        binding = await db.get(DialogueBindingRow, (tenant, source.binding_id))
        assert binding.latest_message_id == newer


async def test_canonical_event_starts_dialogue_context_from_persistent_source(dialogue_source_runtime):
    from apps.scheduler_worker.reply_events import ReplyQualificationEventHandlers
    from shared.events.catalog import InboundMessageStored
    runtime, source, _ = dialogue_source_runtime

    class Engine:
        def __init__(self):
            self.started = []

        async def start(self, tenant, kind, subject, context, key):
            self.started.append((tenant, kind, subject, context, key))

    engine = Engine()
    handler = ReplyQualificationEventHandlers(engine=engine, factory=runtime["factory"], tenant_id=source.tenant_id)
    await handler.on_inbound_message_stored(InboundMessageStored(
        tenant_id=source.tenant_id, occurred_at=NOW,
        message_id=source.admitted_message_id, outbound_message_id=runtime["outbound"]))
    assert len(engine.started) == 1
    context = engine.started[0][3]
    assert context["dialogue_binding_id"] == source.binding_id
    assert context["account_id"] == source.account_id


async def test_real_inbound_processor_correlates_sent_dialogue_header(dialogue_source_runtime):
    from domains.conversations.dialogue_execution import DialogueExecutionState as State
    from infra.db.dialogue_execution import SqlDialogueExecutionStore
    from tests.integration.test_email_inbound_page import archived_page, processor_for
    from tests.unit.test_email_inbound import mime
    runtime, source, reader = dialogue_source_runtime
    tenant = source.tenant_id
    store = SqlDialogueExecutionStore(runtime["factory"], tenant, now=lambda: NOW)
    await store.enqueue(source.admitted_message_id, source.binding_id)
    claim = await store.claim(lease_duration=timedelta(seconds=30))
    started = await store.advance(claim, State.MODEL_STARTED)
    planned = await store.advance(started, State.PLANNED, plan_artifact_id=new_id("art"), plan_sha256="b" * 64)
    import hashlib
    header = "<" + hashlib.sha256(new_id("tst").encode()).hexdigest() + "@messages.tradeos.invalid>"
    sending = await store.advance(planned, State.SENDING, attempt_id=new_id("mat"),
        deterministic_message_id=header, idempotency_key="dialogue:" + source.binding_id)
    assert await reader.for_sent_header(tenant, header) is None
    await store.advance(sending, State.SENT, provider_ref="synthetic-dialogue-sent")
    assert await reader.for_sent_header(tenant, header) == source
    raw = mime(message_id="<" + new_id("tst") + "@example.test>", reply=header,
        body="We need 5000 hinges for cabinets.")
    async for first, anchor in archived_page(runtime, [raw]):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        current = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(tenant, "primary", current.cursor, 20)
        assert page.items, "入站页面为空"
        assert page.items[0].disposition.value == "candidate", page.items[0].disposition
        await processor.process(current, page)
        async with runtime["factory"]() as db:
            message = await db.scalar(text("SELECT message_id FROM messages WHERE tenant_id=:t AND outbound_message_id=:h"),
                {"t": tenant, "h": header})
        assert message is not None
        continued = await reader.for_message(tenant, message)
        assert continued.hypothesis_id == source.hypothesis_id
        assert continued.account_id == source.account_id
        assert continued.enrollment_id == source.enrollment_id


async def test_review_work_atomically_pauses_binding_and_is_idempotent(dialogue_source_runtime):
    from domains.conversations.schemas import ReplyWorkAction, ReplyWorkActionRequest
    from infra.db.dialogue_execution_tables import DialogueBindingRow
    runtime, source, _ = dialogue_source_runtime
    request = ReplyWorkActionRequest(message_id=source.admitted_message_id,
        outbound_message_id=runtime["outbound"], enrollment_id=source.enrollment_id,
        dialogue_binding_id=source.binding_id, account_id=source.account_id,
        contact_point_id=source.contact_point_id, action=ReplyWorkAction.REVIEW_REPLY,
        idempotency_key=f"reply:review_reply:{source.admitted_message_id}")
    service = runtime["deps"].conversations
    first = await service.enqueue_reply_work_action(source.tenant_id, request)
    second = await service.enqueue_reply_work_action(source.tenant_id, request)
    assert first.action_id == second.action_id
    async with runtime["factory"]() as db:
        row = await db.get(DialogueBindingRow, (source.tenant_id, source.binding_id))
        assert row.state == "review"


async def test_bound_inbound_transaction_reads_same_dialogue_source(dialogue_source_runtime):
    from infra.db.email_inbound_uow import _BoundConversations
    runtime, source, _ = dialogue_source_runtime
    async with runtime["factory"]() as db:
        bound = _BoundConversations(db, source.tenant_id, lambda: NOW)
        assert await bound.dialogue_source(source.tenant_id, source.admitted_message_id) == source
