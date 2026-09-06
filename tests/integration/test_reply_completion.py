"""Task5真实入口到原scheduler完整分类，只有模型/Provider受控。"""

import importlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from apps.composition_support.email_inbound import InboundMailbox, InboundRuntimePorts
from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap
from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
from connectors.object_store.config import S3ObjectStoreSettings
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from infra.controlled.providers import ControlledDnsResolver
from shared.schemas.identifiers import EmployeeId
from shared.schemas.money import CurrencyCode, Money
from tests.integration.test_email_inbound_access import composition
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import (
    NOW,
    prepare_sent,
)
from tests.integration.test_email_inbound_page import (
    page_runtime as _page_runtime,
)
from tests.unit.test_email_inbound import mime

page_runtime = _page_runtime


class Model:
    def __init__(self, category, candidates=()):
        self.category, self.candidates = category, candidates
        self.calls = []

    async def complete_json(self, *, model, system_prompt, payload, max_output_tokens):
        self.calls.append(payload)
        return json.dumps(
            {"category": self.category, "candidate_fields": list(self.candidates)}
        )


def factory_for(runtime, model):
    try:
        builder = importlib.import_module(
            "apps.scheduler_worker.reply_composition"
        ).CurrentEmployeeReplyFactory
    except ModuleNotFoundError:
        pytest.fail("完整原reply组合工厂缺失")
    config = runtime["config"]
    tenant = runtime["route"].tenant_id
    env = config.runtime_environment()
    env["TRADEOS_TENANT_ID"] = tenant
    env["TRADEOS_SCHEDULER_HEALTH_PORT"] = str(config.scheduler_port)
    return SchedulerRuntimeFactory(
        env,
        bootstrap=CanonicalSchedulerBootstrap(
            ScoringPolicy(
                "controlled-v1",
                (Money(Decimal(1000), CurrencyCode("USD")),),
                {i: "high" for i in range(1, 8)},
            ),
            HandoffPolicy(sla_seconds=3600, backlog_threshold=10),
            campaign_enabled=True,
            gmail_transport=runtime["provider"],
            secret_resolver=config,
            reply_factory=builder(
                tenant,
                EmployeeId(runtime["staff"][0].employee_id),
                model,
                "controlled-reply-v1",
            ),
        ),
        inbound_ports=InboundRuntimePorts(
            profile=InboundMailbox(
                tenant_id=tenant,
                mailbox_alias="primary",
                route_id="controlled",
                config_version="v1",
            ),
            provider=runtime["provider"],
            secret_resolver=config,
            secret_ref="CONTROLLED_GMAIL",
            object_settings=S3ObjectStoreSettings.from_environ(env),
            fingerprint_key_ref="CONTROLLED_FINGERPRINT",
            lease_owner="reply-completion-test",
        ),
        resolver_factory=ControlledDnsResolver,
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    "category,body,stopped",
    [
        ("unsubscribe", "Please unsubscribe me.", True),
        ("auto_reply", "I am out of office.", False),
        ("rejection", "We are not interested.", True),
        ("clear_interest", "We may need hinges.", True),
    ],
)
async def test_real_inbound_complete_classification(
    page_runtime, category, body, stopped
):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    inbound = await composition(runtime)
    tenant = runtime["route"].tenant_id
    boss = EmployeeId(runtime["staff"][0].employee_id)
    model = Model(category)
    try:
        await inbound.management.bind(
            tenant, boss, runtime["route"].configured_identity_id
        )
        await runtime["provider"].receive_inbound(
            mime(
                body=body,
                message_id=f"<{category}@example.test>",
                reply=runtime["outbound"],
            ),
            internal_date=NOW,
        )
        async with factory_for(runtime, model)() as worker:
            capability = next(
                c for c in worker.capabilities if c.name == "inbound_body"
            )
            assert capability.status == "enabled"
            assert worker.inbound_driver is not None
            for _ in range(3):
                await worker.inbound_driver.scan_once()
                await worker.outbox.drain()
                await worker.workflow.poll_due(tenant, 20)
                await worker.workflow.poll_due(tenant, 20)
        async with runtime["factory"]() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT status,last_error FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                    ),
                    {"t": tenant},
                )
            ).all()
            assert rows == [("completed", None)]
            states = (
                (
                    await session.execute(
                        text(
                            "SELECT state FROM outreach_enrollments WHERE tenant_id=:t"
                        ),
                        {"t": tenant},
                    )
                )
                .scalars()
                .all()
            )
            assert (states == ["in_sequence"]) == (not stopped)
        assert len(model.calls) == 1
        assert set(model.calls[0]) == {"subject", "body"}
    finally:
        await inbound.aclose()


async def test_next_questions_current_actor_gate_before_conversation_lookup(
    page_runtime,
):
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from shared.schemas.identifiers import new_id

    await prepare_sent(page_runtime)
    tenant = page_runtime["route"].tenant_id
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=2),
        dependencies=page_runtime["deps"],
    )
    sales = next(i.employee_id for i in page_runtime["staff"] if i.role == "sales")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://controlled.test"
    ) as client:
        response = await client.get(
            f"/inbox/conversations/{new_id('con')}/messages/{new_id('msg')}/next-questions",
            headers={"X-Tenant-Id": tenant, "X-Employee-Id": sales},
        )
    assert response.status_code == 403


async def client_for(runtime):
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings

    tenant = runtime["route"].tenant_id
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=2),
        dependencies=runtime["deps"],
    )
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://controlled.test",
        headers={
            "X-Tenant-Id": tenant,
            "X-Employee-Id": runtime["staff"][0].employee_id,
        },
    )


async def advance(worker, tenant, times=4):
    for _ in range(times):
        await worker.outbox.drain()
        await worker.workflow.poll_due(tenant, 30)


async def configure_playbook(runtime, worker):
    tenant = runtime["route"].tenant_id
    async with await client_for(runtime) as client:
        proposed = await client.post(
            "/settings/playbook/proposals",
            headers={"Idempotency-Key": "reply-completion-book"},
            json={
                "company_type": "trading_company",
                "minimum_deal_amount": "1000.00",
                "minimum_deal_currency": "USD",
                "excluded_categories": [],
                "excluded_countries": [],
                "sourcing_regions": ["controlled"],
                "monthly_budget_credits": 3,
                "approval_requirements": [],
                "supply_capabilities_note": "本次合成演练",
            },
        )
        assert proposed.status_code == 202, proposed.json()
        await advance(worker, tenant)
        versions = (await client.get("/settings/playbook/versions")).json()
        approval = next(v["approval_id"] for v in versions if v["approval_id"])
        result = await client.post(
            f"/approvals/{approval}/decide",
            headers={"X-Employee-Id": runtime["staff"][1].employee_id},
            json={"decision": "approve"},
        )
        assert result.status_code == 200, result.json()
        await advance(worker, tenant)
        assert (await client.get("/settings/playbook")).json()["configured"] is True


@pytest.mark.parametrize(
    "complete,outlook", [(False, False), (True, False), (False, True)]
)
async def test_real_need_evidence_questions_and_handoff(
    page_runtime, monkeypatch, complete, outlook
):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    body = "We need hinges for cabinet doors."
    fields = [
        {"field": "product_category", "value": "hinges", "quote": "We need hinges"}
    ]
    if complete:
        body += " We need 5000 units at USD 2 per unit."
        fields.extend(
            [
                {
                    "field": "application",
                    "value": "cabinet doors",
                    "quote": "for cabinet doors",
                },
                {"field": "quantity", "value": "5000", "quote": "5000 units"},
                {
                    "field": "target_price",
                    "value": '{"amount":"2","currency":"USD"}',
                    "quote": "USD 2 per unit",
                },
            ]
        )
    body += "\nContact buyer@example.test at https://example.test."
    if outlook:
        body = (
            "<p>"
            + body
            + '</p><div id="divRplyFwdMsg">From: Supplier</div><p>We offered 100 units.</p>'
        )
    model = Model("provides_specification", fields)
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        async with factory_for(runtime, model)() as worker:
            await configure_playbook(runtime, worker)
            await runtime["provider"].receive_inbound(
                mime(
                    body=body,
                    headers="Content-Type: text/html; charset=utf-8" if outlook else "",
                    message_id="<real-need@example.test>",
                    reply=runtime["outbound"],
                ),
                internal_date=NOW,
            )
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await advance(worker, tenant, 8)
        async with runtime["factory"]() as session:
            runs = (
                await session.execute(
                    text(
                        "SELECT status,last_error FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                    ),
                    {"t": tenant},
                )
            ).all()
            assert runs == [("completed", None)]
            needs = (
                (
                    await session.execute(
                        text("SELECT need_id FROM validated_needs WHERE tenant_id=:t"),
                        {"t": tenant},
                    )
                )
                .scalars()
                .all()
            )
            assert len(needs) == 1
            if outlook:
                stored = (
                    await session.execute(
                        text(
                            "SELECT product_category,quantity FROM validated_needs WHERE tenant_id=:t AND need_id=:n"
                        ),
                        {"t": tenant, "n": needs[0]},
                    )
                ).one()
                assert stored.product_category["value"] == "hinges"
                assert stored.quantity is None
            message = (
                await session.execute(
                    text(
                        "SELECT message_id,conversation_id FROM messages WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).one()
            count = await session.scalar(
                text("SELECT count(*) FROM opportunities WHERE tenant_id=:t"),
                {"t": tenant},
            )
            assert count == int(complete)
            handoffs = await session.scalar(
                text("SELECT count(*) FROM handoffs WHERE tenant_id=:t"), {"t": tenant}
            )
            assert handoffs == int(complete)
            work = (
                await session.execute(
                    text(
                        "SELECT action,status FROM conversation_reply_work WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
            ).all()
            assert work == ([] if complete else [("create_follow_up", "pending")])
            if complete:
                notifications = (
                    await session.execute(
                        text(
                            "SELECT source_event,priority,status FROM notification_jobs WHERE tenant_id=:t AND source_event='HandoffRequested'"
                        ),
                        {"t": tenant},
                    )
                ).all()
                assert len(notifications) == 4, (
                    notifications,
                    (
                        await session.execute(
                            text(
                                "SELECT handler_name,last_error FROM outbox_deliveries WHERE tenant_id=:t AND status='dead'"
                            ),
                            {"t": tenant},
                        )
                    ).all(),
                )
        if complete:
            await deliver_notifications(runtime)
            await verify_current_owner_notification_routing(runtime)
        async with await client_for(runtime) as client:
            response = await client.get(
                f"/inbox/conversations/{message.conversation_id}/messages/{message.message_id}/next-questions"
            )
            assert response.status_code == 200, response.json()
            suggestions = response.json()
            assert suggestions["need_id"] == needs[0]
            assert suggestions["topics"] == (
                [] if complete else ["application", "size_spec"]
            )
            assert len(suggestions["suggestions"]) == (0 if complete else 2)
        # Task7：真实当前负责人和其直属经理走Conversations→Outreach→Demand全链。
        from domains.employees.permissions import Actor as EmployeeActor
        from domains.employees.permissions import EmployeeScope
        from tests.integration.test_inbox_access import inbox_actor

        boss_actor, sales_actor = (
            inbox_actor(runtime, "boss"),
            inbox_actor(runtime, "sales"),
        )
        detail = await runtime["deps"].conversations.get_inbox_detail(
            tenant, message.conversation_id, actor=boss_actor
        )
        async with runtime["deps"].employees(tenant) as employees:
            trusted_boss = EmployeeActor(
                str(boss_actor.employee_id), EmployeeScope.TENANT, "boss"
            )
            lock = await employees.resolve_owner(
                tenant,
                detail.account_id,
                actor=trusted_boss,
                country="US",
                boss_override=sales_actor.employee_id,
            )
            if lock.owner != sales_actor.employee_id:
                await employees.transfer(
                    tenant,
                    detail.account_id,
                    sales_actor.employee_id,
                    actor=trusted_boss,
                    transferred_by=boss_actor.employee_id,
                    reason="测试收件箱当前负责人",
                )
        async with await client_for(runtime) as client:
            for role in ("sales", "manager"):
                response = await client.get(
                    f"/inbox/conversations/{message.conversation_id}/messages/{message.message_id}/next-questions",
                    headers={"X-Employee-Id": inbox_actor(runtime, role).employee_id},
                )
                assert response.status_code == 200, response.json()
                assert response.json() == suggestions
        if not complete and not outlook:
            from tests.integration.test_inbox_access import change_access

            original_read = runtime["deps"].reply_suggestions.demand.get_need

            async def read_and_transfer(*args, **kwargs):
                need = await original_read(*args, **kwargs)
                await change_access(runtime, detail.account_id, "transfer")
                return need

            monkeypatch.setattr(
                runtime["deps"].reply_suggestions.demand, "get_need", read_and_transfer
            )
            async with await client_for(runtime) as client:
                stale = await client.get(
                    f"/inbox/conversations/{message.conversation_id}/messages/{message.message_id}/next-questions",
                    headers={"X-Employee-Id": sales_actor.employee_id},
                )
                assert stale.status_code == 403
        assert len(model.calls) == 1
        assert "buyer@example.test" not in json.dumps(model.calls)
        assert "https://" not in json.dumps(model.calls)
        if outlook:
            assert "We offered 100 units" not in model.calls[0]["body"]
    finally:
        await inbound.aclose()


@pytest.mark.parametrize(
    "body,content_type,quote",
    [
        ("Thanks.\n> We offered 100 units.", "text/plain", "100 units"),
        (
            "<p>Thanks.</p><blockquote>We offered 100 units.</blockquote>",
            "text/html",
            "100 units",
        ),
        (
            '<p>Thanks.</p><div class="gmail_quote">We offered 100 units.</div>',
            "text/html",
            "100 units",
        ),
        (
            '<p>Thanks. We have no current need.</p><div id="divRplyFwdMsg"><b>From:</b> Supplier<br><b>Subject:</b> Previous message</div><p>We offered 100 units.</p>',
            "text/html",
            "100 units",
        ),
        ("We need\n> a previous note\n100 units.", "text/plain", "We need\n100 units."),
        (
            "We need hinges. Contact buyer@example.test.",
            "text/plain",
            "[private reference omitted]",
        ),
        ("We need hinges.", "text/plain", "We need 100 units."),
    ],
)
async def test_unreliable_quotes_fail_before_classification_or_need(
    page_runtime, body, content_type, quote
):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    model = Model(
        "provides_specification",
        [{"field": "quantity", "value": "100", "quote": quote}],
    )
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        raw = mime(
            body=body,
            headers=f"Content-Type: {content_type}; charset=utf-8",
            message_id="<invalid-quote@example.test>",
            reply=runtime["outbound"],
        )
        await runtime["provider"].receive_inbound(raw, internal_date=NOW)
        async with factory_for(runtime, model)() as worker:
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await advance(worker, tenant)
        async with runtime["factory"]() as session:
            for table in (
                "conversation_classifications",
                "validated_needs",
                "opportunities",
                "handoffs",
            ):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"),
                        {"t": tenant},
                    )
                    == 0
                )
            status = await session.scalar(
                text(
                    "SELECT status FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification' ORDER BY created_at, run_id"
                ),
                {"t": tenant},
            )
            assert status == "failed"
        assert len(model.calls) == 1
    finally:
        await inbound.aclose()


@pytest.mark.parametrize("outlook", [False, True])
async def test_ordinary_current_reply_does_not_inherit_historical_unsubscribe(
    page_runtime,
    outlook,
):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    model = Model("no_current_need")
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        await runtime["provider"].receive_inbound(
            mime(
                body=(
                    '<p>Thanks. We have no current need.</p><div id="divRplyFwdMsg"><b>From:</b> Supplier<br><b>Subject:</b> Previous message</div><p>Please unsubscribe our entire company.</p>'
                    if outlook
                    else "Thanks. We have no current need.\n> Please unsubscribe our entire company."
                ),
                headers="Content-Type: text/html; charset=utf-8" if outlook else "",
                message_id="<ordinary@example.test>",
                reply=runtime["outbound"],
            ),
            internal_date=NOW,
        )
        async with factory_for(runtime, model)() as worker:
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await advance(worker, tenant)
        async with runtime["factory"]() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM outreach_suppressions WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT category FROM conversation_classifications WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
                == "no_current_need"
            )
        assert model.calls == [
            {
                "subject": "(current reply)",
                "body": "\nThanks. We have no current need."
                if outlook
                else "Thanks. We have no current need.\n",
            }
        ]
    finally:
        await inbound.aclose()


async def deliver_notifications(runtime):
    import asyncio

    from apps.notification_worker.config import NotificationWorkerConfig
    from apps.notification_worker.runtime import (
        NotificationRuntimeMode,
        notification_worker_runtime,
        run_notification_worker,
    )
    from scripts.run_web_core_controlled import reserve

    tenant = runtime["route"].tenant_id
    listener = reserve(0)
    port = listener.getsockname()[1]
    listener.close()
    settings = NotificationWorkerConfig(
        runtime["config"].database_url, tenant, 1, 30, port, "reply-notification"
    )
    async with notification_worker_runtime(
        settings, mode=NotificationRuntimeMode.CONTROLLED_IN_APP
    ) as delivery:
        stop = asyncio.Event()

        async def wait(seconds, event):
            event.set()

        result = await run_notification_worker(delivery, stop_event=stop, wait=wait)
        assert result.jobs_completed >= 4
    async with runtime["factory"]() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM notification_jobs WHERE tenant_id=:t AND source_event='HandoffRequested' AND status='completed'"
                ),
                {"t": tenant},
            )
            == 4
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM in_app_notifications n JOIN notification_jobs j ON n.tenant_id=j.tenant_id AND n.source_job_id=j.notification_job_id WHERE n.tenant_id=:t AND j.tenant_id=:t AND j.source_event='HandoffRequested' AND n.priority='urgent'"
                ),
                {"t": tenant},
            )
            == 4
        )


async def test_unknown_provider_send_is_reconciled_without_a_second_send(
    page_runtime, tmp_path, monkeypatch, caplog
):
    from connectors.gmail.transport import GmailNetworkError

    runtime = page_runtime
    original_send = runtime["provider"].send

    async def lost_response(**kwargs):
        await original_send(**kwargs)
        raise GmailNetworkError(may_have_written=True)

    monkeypatch.setattr(runtime["provider"], "send", lost_response)
    with pytest.raises(AssertionError):
        await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    async with runtime["factory"]() as session:
        attempt = (
            await session.execute(
                text(
                    "SELECT attempt_id,state,deterministic_message_id FROM outreach_message_attempts WHERE tenant_id=:t"
                ),
                {"t": tenant},
            )
        ).one()
        assert attempt.state != "sent"
        assert (
            await session.scalar(
                text("SELECT count(*) FROM messages WHERE tenant_id=:t"), {"t": tenant}
            )
            == 0
        )
    from apps.api.composition.runtime import build_phase1_dependencies
    from apps.api.runtime_config import Phase1RuntimeSettings
    from infra.controlled.providers import ControlledGmailTransport

    environment = runtime["config"].runtime_environment()
    environment["TRADEOS_TENANT_ID"] = tenant
    recovered_deps = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(environment),
        runtime["factory"],
        now=lambda: NOW + timedelta(minutes=3),
        secret_resolver=runtime["config"],
        gmail_transport=ControlledGmailTransport(
            tmp_path / "mail.sqlite", tenant_id=tenant
        ),
    )
    runtime["deps"] = recovered_deps
    try:
        async with await client_for(runtime) as client:
            for _ in range(2):
                recovered = await client.post(
                    f"/crm/message-attempts/{attempt.attempt_id}/send",
                    json={
                        "subject": "Current supply needs",
                        "body": "Which components are you currently looking for?",
                    },
                )
                assert recovered.status_code == 200, (
                    recovered.json(),
                    [getattr(record, "error_type", None) for record in caplog.records],
                )
    finally:
        if recovered_deps.model_lifecycle:
            await recovered_deps.model_lifecycle.aclose()
    calls = await runtime["provider"].list_calls()
    assert sum(call.operation == "send" for call in calls) == 1
    assert sum(call.operation == "search" for call in calls) >= 2
    async with runtime["factory"]() as session:
        assert (
            await session.scalar(
                text("SELECT state FROM outreach_message_attempts WHERE tenant_id=:t"),
                {"t": tenant},
            )
            == "sent"
        )


async def test_cancel_real_inbound_run_then_restart_keeps_cursor_and_no_classification(
    page_runtime,
):
    from shared.schemas.identifiers import RunId

    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    model = Model("clear_interest")
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        await runtime["provider"].receive_inbound(
            mime(
                body="We may need hinges.",
                message_id="<cancelled@example.test>",
                reply=runtime["outbound"],
            ),
            internal_date=NOW,
        )
        async with factory_for(runtime, model)() as worker:
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await worker.outbox.drain()
            async with runtime["factory"]() as session:
                run = await session.scalar(
                    text(
                        "SELECT run_id FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                    ),
                    {"t": tenant},
                )
            await runtime["deps"].workflow_engine.cancel(
                tenant, RunId(run), "本次演练取消"
            )
        async with factory_for(runtime, model)() as restored:
            await restored.inbound_driver.scan_once()
            await advance(restored, tenant)
        async with runtime["factory"]() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT status FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification' ORDER BY created_at, run_id"
                    ),
                    {"t": tenant},
                )
                == "cancelled"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM conversation_classifications WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM messages WHERE tenant_id=:t"),
                    {"t": tenant},
                )
                == 1
            )
        assert not model.calls
    finally:
        await inbound.aclose()


async def verify_current_owner_notification_routing(runtime):
    from apps.scheduler_worker.bootstrap import CurrentNotificationAudience
    from domains.employees.permissions import Actor, EmployeeScope
    from infra.db.outbox import deserialize
    from shared.events.catalog import HandoffRequested
    from shared.schemas.identifiers import ProspectAccountId

    tenant = runtime["route"].tenant_id
    async with runtime["factory"]() as session:
        payload = await session.scalar(
            text(
                "SELECT event_payload FROM outbox_events WHERE tenant_id=:t AND event_type='HandoffRequested'"
            ),
            {"t": tenant},
        )
    event = deserialize(HandoffRequested, payload)
    audience = CurrentNotificationAudience(
        tenant, runtime["deps"].employees, runtime["deps"].opportunities
    )
    before = {item.employee_id for item in await audience.recipients_for(tenant, event)}
    old_owner = event.assigned_to
    assert old_owner in before
    new_owner = EmployeeId(
        next(item.employee_id for item in runtime["staff"] if item.role == "sourcing")
    )
    boss = EmployeeId(runtime["staff"][0].employee_id)
    async with runtime["deps"].employees(tenant) as employees:
        await employees.transfer(
            tenant,
            ProspectAccountId(runtime["source_account_id"]),
            new_owner,
            actor=Actor(str(boss), EmployeeScope.TENANT, "boss"),
            transferred_by=boss,
            reason="本次演练明确转交",
        )
    after = {item.employee_id for item in await audience.recipients_for(tenant, event)}
    assert old_owner not in after and new_owner in after


@pytest.mark.parametrize("category_changed", [False, True])
async def test_missing_need_is_completed_by_later_reply_across_runtime_restart(
    page_runtime, category_changed
):
    from infra.db.repositories.email_inbound import InboundStore

    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    first_body = "We need hinges."
    later_body = "We need hinges for cabinet doors, 5000 units at USD 2 per unit.\n> We offered 100 units."
    first_model = Model(
        "provides_specification",
        [{"field": "product_category", "value": "hinges", "quote": "We need hinges"}],
    )
    later_model = Model(
        "provides_specification",
        [
            {"field": "product_category", "value": "hinges", "quote": "We need hinges"},
            {
                "field": "application",
                "value": "cabinet doors",
                "quote": "cabinet doors",
            },
            {"field": "quantity", "value": "5000", "quote": "5000 units"},
            {
                "field": "target_price",
                "value": '{"amount":"2","currency":"USD"}',
                "quote": "USD 2 per unit",
            },
        ],
    )
    if category_changed:
        later_body = later_body.replace("hinges", "bolts")
        later_model.candidates[0] = {
            "field": "product_category",
            "value": "bolts",
            "quote": "We need bolts",
        }
    store = InboundStore(runtime["factory"], tenant, "primary", now=lambda: NOW)
    first_raw = mime(
        body=first_body,
        message_id="<need-first@example.test>",
        reply=runtime["outbound"],
    )
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        await runtime["provider"].receive_inbound(first_raw, internal_date=NOW)
        async with factory_for(runtime, first_model)() as worker:
            await configure_playbook(runtime, worker)
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await advance(worker, tenant, 6)
        checkpoint = await store.read_cursor()
        async with runtime["factory"]() as session:
            need_before = await session.scalar(
                text("SELECT need_id FROM validated_needs WHERE tenant_id=:t"),
                {"t": tenant},
            )
            assert need_before is not None
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM opportunities WHERE tenant_id=:t"),
                    {"t": tenant},
                )
                == 0
            )
        await runtime["provider"].receive_inbound(first_raw, internal_date=NOW)
        await runtime["provider"].receive_inbound(
            mime(
                body=later_body,
                message_id="<need-later@example.test>",
                reply=runtime["outbound"],
            ),
            internal_date=NOW,
        )
        async with factory_for(runtime, later_model)() as restored:
            assert await store.read_cursor() == checkpoint
            await restored.inbound_driver.scan_once()
            await advance(restored, tenant, 8)
            await restored.inbound_driver.scan_once()
            await advance(restored, tenant, 2)
        assert len(first_model.calls) == len(later_model.calls) == 1
        assert "100 units" not in later_model.calls[0]["body"]
        async with runtime["factory"]() as session:
            assert (
                await session.execute(
                    text("SELECT need_id FROM validated_needs WHERE tenant_id=:t"),
                    {"t": tenant},
                )
            ).scalars().all() == [need_before]
            for table, count in (
                ("messages", 2),
                ("conversation_classifications", 2),
                ("opportunities", int(not category_changed)),
                ("handoffs", int(not category_changed)),
            ):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE tenant_id=:t"),
                        {"t": tenant},
                    )
                    == count
                ), (
                    table,
                    (
                        await session.execute(
                            text(
                                "SELECT status,last_error FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                            ),
                            {"t": tenant},
                        )
                    ).all(),
                )
            assert (
                await session.execute(
                    text(
                        "SELECT status FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification' ORDER BY created_at, run_id"
                    ),
                    {"t": tenant},
                )
            ).scalars().all() == [
                "completed",
                "failed" if category_changed else "completed",
            ]
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM conversation_reply_work WHERE tenant_id=:t AND action='create_follow_up' AND status='pending'"
                    ),
                    {"t": tenant},
                )
                == 1
            )
            for table, column in (
                ("workflow_runs", "context"),
                ("outbox_events", "event_payload"),
            ):
                dumps = (
                    (
                        await session.execute(
                            text(
                                f"SELECT {column}::text FROM {table} WHERE tenant_id=:t"
                            ),
                            {"t": tenant},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert all(
                    later_body not in dump
                    and "original_body" not in dump
                    and "evidence_segments" not in dump
                    for dump in dumps
                )
    finally:
        await inbound.aclose()


async def test_current_boss_next_questions_rejects_client_fact_query(page_runtime):
    from shared.schemas.identifiers import new_id

    await prepare_sent(page_runtime)
    async with await client_for(page_runtime) as client:
        response = await client.get(
            f"/inbox/conversations/{new_id('con')}/messages/{new_id('msg')}/next-questions",
            params={
                "missing_fields": "quantity",
                "completeness": "5",
                "owner": "pretend",
            },
        )
    assert response.status_code == 400


async def test_raw_secret_is_rejected_before_model_at_real_inbound_entry(page_runtime):
    runtime = page_runtime
    await prepare_sent(runtime, reply_source=True)
    tenant = runtime["route"].tenant_id
    inbound = await composition(runtime)
    model = Model("provides_specification")
    try:
        await inbound.management.bind(
            tenant,
            EmployeeId(runtime["staff"][0].employee_id),
            runtime["route"].configured_identity_id,
        )
        raw = mime(
            body='<p>We need hinges.</p><span data-private="password">details</span>',
            headers="Content-Type: text/html; charset=utf-8",
            message_id="<unsafe@example.test>",
            reply=runtime["outbound"],
        )
        await runtime["provider"].receive_inbound(raw, internal_date=NOW)
        async with factory_for(runtime, model)() as worker:
            await worker.inbound_driver.scan_once()
            await worker.inbound_driver.scan_once()
            await advance(worker, tenant)
        assert model.calls == []
        async with runtime["factory"]() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM conversation_classifications WHERE tenant_id=:t"
                    ),
                    {"t": tenant},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                    ),
                    {"t": tenant},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT reason FROM email_inbound_reviews WHERE tenant_id=:t"),
                    {"t": tenant},
                )
                == "credential_marker"
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM messages WHERE tenant_id=:t"),
                    {"t": tenant},
                )
                == 0
            )
    finally:
        await inbound.aclose()
