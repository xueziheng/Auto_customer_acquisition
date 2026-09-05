"""入站管理与原件的真实权限和Gateway验收。"""

import importlib

import pytest

# ruff: noqa: F811 - pytest按名称注入跨模块复用的owned fixture
from domains.sending_identity.errors import SendingIdentityNotFoundError
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import (  # noqa: F401
    page_runtime,
    prepare_sent,
)
from tool_gateway.errors import ToolGatewayError


async def test_management_binding_requires_human_and_exact_identity(page_runtime):
    try:
        module = importlib.import_module("apps.composition_support.email_inbound")
    except ModuleNotFoundError:
        pytest.fail("入站管理/原件/单页读取机械装配缺失")
    assert hasattr(module, "build_inbound_composition")


async def composition(runtime, clock=None):
    from apps.api.controlled import initialize_identities
    from apps.composition_support.email_inbound import (
        InboundMailbox,
        build_inbound_composition,
    )
    from connectors.object_store.config import S3ObjectStoreSettings
    from domains.outreach.permissions import Phase1OutreachAuthorizer
    from domains.outreach.service_impl import OutreachServiceImpl
    from shared.schemas.identifiers import new_id
    from tests.integration.test_email_inbound_page import NOW

    clock = clock or (lambda: NOW)
    config = runtime["config"].model_copy(
        update={
            "tenant_id": runtime["route"].tenant_id,
            "identities": tuple(
                i.model_copy(
                    update={"employee_id": new_id("emp"), "user_id": new_id("usr")}
                )
                for i in runtime["config"].identities
            ),
        }
    )
    if "staff" not in runtime:
        await initialize_identities(config)
        runtime["staff"] = config.identities

    def builder(factory, audit):
        return OutreachServiceImpl(
            factory,
            None,
            None,
            None,
            None,
            Phase1OutreachAuthorizer(runtime["route"].tenant_id),
            audit,
            now=clock,
        )

    return build_inbound_composition(
        InboundMailbox(
            tenant_id=runtime["route"].tenant_id,
            mailbox_alias="primary",
            route_id="controlled",
            config_version="v1",
        ),
        runtime["factory"],
        sending_identities=runtime["deps"].sending_identities,
        employees=runtime["deps"].employees,
        employee_actor=runtime["deps"].employee_lookup_actor,
        outreach_builder=builder,
        provider=runtime["provider"],
        secret_resolver=config,
        secret_ref="CONTROLLED_GMAIL",
        object_settings=S3ObjectStoreSettings.from_environ(
            config.runtime_environment()
        ),
        fingerprint_key_ref="CONTROLLED_FINGERPRINT",
        lease_owner="inbound-access-tests",
        now=clock,
    )


async def test_active_boss_binding_status_review_and_raw(page_runtime):
    from shared.errors import PermissionDenied
    from shared.schemas.identifiers import (
        EmployeeId,
        SendingIdentityId,
        TenantId,
        new_id,
    )
    from tests.integration.test_email_inbound_page import NOW
    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    c = await composition(runtime)
    tenant = runtime["route"].tenant_id
    boss = EmployeeId(next(i.employee_id for i in runtime["staff"] if i.role == "boss"))
    sales = EmployeeId(
        next(i.employee_id for i in runtime["staff"] if i.role == "sales")
    )
    try:
        assert (await c.management.status(tenant, boss)).state == "disabled"
        with pytest.raises(PermissionDenied):
            await c.management.bind(
                tenant, sales, runtime["route"].configured_identity_id
            )
        with pytest.raises(SendingIdentityNotFoundError):
            await c.management.bind(tenant, boss, SendingIdentityId(new_id("sid")))
        first = await c.management.bind(
            tenant, boss, runtime["route"].configured_identity_id
        )
        assert first.state == "active"
        assert await c.management.bind(tenant, boss, first.identity_id) == first
        cursor = await c.store.read_cursor()
        assert cursor.confirmed_by == boss and cursor.confirmed_at == NOW
        raw = mime(message_id="<review-source@example.test>")
        await runtime["provider"].receive_inbound(raw, internal_date=NOW)
        reader = c.reader_for(cursor.route)
        anchor = await reader.fetch(tenant, "primary", cursor.cursor, 20)
        await c.processor.process(cursor, anchor)
        cursor = await c.store.read_cursor()
        page = await reader.fetch(tenant, "primary", cursor.cursor, 20)
        await c.processor.process(cursor, page)
        reviews = await c.management.reviews(tenant, boss, limit=10, after=None)
        assert (
            len(reviews) == 1
            and reviews[0].archived
            and reviews[0].reason == "unknown_outbound"
        )
        assert await c.raw.read(tenant, boss, reviews[0].review_id) == raw
        for bad_tenant, bad_employee in (
            (tenant, sales),
            (TenantId(new_id("tn")), boss),
        ):
            with pytest.raises(ToolGatewayError):
                await c.raw.read(bad_tenant, bad_employee, reviews[0].review_id)
        from sqlalchemy import text

        async with runtime["factory"].begin() as session:
            await session.execute(
                text(
                    "UPDATE employees SET is_active=false WHERE tenant_id=:t AND employee_id=:e"
                ),
                {"t": tenant, "e": boss},
            )
        with pytest.raises(PermissionDenied):
            await c.management.reviews(tenant, boss, limit=10, after=None)
        with pytest.raises(ToolGatewayError):
            await c.raw.read(tenant, boss, reviews[0].review_id)
    finally:
        await c.aclose()


async def test_http_status_exposes_disabled_composition_safely(page_runtime):
    from httpx import ASGITransport, AsyncClient

    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings

    await prepare_sent(page_runtime)
    # 不造替身API或结果态；取本测试公开初始化的当前boss。
    from sqlalchemy import text

    async with page_runtime["factory"]() as session:
        boss = await session.scalar(
            text(
                "SELECT employee_id FROM employees WHERE tenant_id=:t AND role='boss' AND is_active=true ORDER BY employee_id LIMIT 1"
            ),
            {"t": page_runtime["route"].tenant_id},
        )
    app = create_app(
        settings=ApiSettings(
            tenant_id=page_runtime["route"].tenant_id,
            dev_mode=True,
            retry_after_seconds=2,
        ),
        dependencies=page_runtime["deps"],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://controlled.test"
    ) as client:
        response = await client.get(
            "/email-inbound/status",
            headers={
                "X-Tenant-Id": page_runtime["route"].tenant_id,
                "X-Employee-Id": boss,
            },
        )
    assert response.status_code == 503


async def test_driver_restart_routes_real_sent_event_to_original_reply_run(
    page_runtime,
):
    from sqlalchemy import text

    from apps.scheduler_worker.inbound_driver import InboundDriver
    from apps.scheduler_worker.reply_events import ReplyQualificationEventHandlers
    from infra.db.outbox_delivery import OutboxDeliverer
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.events.catalog import InboundMessageStored
    from shared.schemas.identifiers import EmployeeId
    from tests.integration.test_email_inbound_page import NOW
    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification.flow import (
        build_reply_qualification_handlers,
        register_reply_qualification,
    )

    runtime = page_runtime
    await prepare_sent(runtime)
    c = await composition(runtime)
    c2 = None
    tenant = runtime["route"].tenant_id
    boss = EmployeeId(next(i.employee_id for i in runtime["staff"] if i.role == "boss"))
    try:
        await c.management.bind(tenant, boss, runtime["route"].configured_identity_id)
        await runtime["provider"].receive_inbound(
            mime(
                message_id="<driver-real-sent@example.test>", reply=runtime["outbound"]
            ),
            internal_date=NOW,
        )
        driver = InboundDriver(c, now=lambda: NOW)
        assert await driver.scan_once() == 1  # 耐久profile空锚定
        c2 = await composition(runtime)
        assert (
            c2.store is not c.store
            and c2.raw is not c.raw
            and c2.objects is not c.objects
        )
        assert await InboundDriver(c2, now=lambda: NOW).scan_once() == 1
        # 本批只验证原事件→Run，不执行Task6分类。原步骤类型完整注册，未调用端口不造模型结果。
        handlers = build_reply_qualification_handlers(
            classifier=None,
            content_reader=None,
            input_guard=None,
            conversations=runtime["deps"].conversations,
            outreach=runtime["deps"].outreach,
            tenant_id=tenant,
            now=lambda: NOW,
        )
        engine = PostgresWorkflowEngine(runtime["factory"], handlers, now=lambda: NOW)
        register_reply_qualification(engine)
        outbox = OutboxDeliverer(runtime["factory"], tenant, now=lambda: NOW)
        outbox.register_handler(
            InboundMessageStored,
            "reply_qualification.inbound_stored",
            ReplyQualificationEventHandlers(
                engine=engine, factory=runtime["factory"], tenant_id=tenant
            ),
        )
        await outbox.drain()
        await outbox.drain()
        async with runtime["factory"]() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT subject_ref,context FROM workflow_runs WHERE tenant_id=:t AND workflow_type='reply_qualification'"
                    ),
                    {"t": tenant},
                )
            ).all()
            assert len(rows) == 1 and set(rows[0].context) == {
                "message_id",
                "outbound_message_id",
                "enrollment_id",
                "account_id",
                "contact_point_id",
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND event_type='InboundMessageStored' AND status='delivered'"
                    ),
                    {"t": tenant},
                )
                == 1
            )
    finally:
        await c.aclose()
        if c2:
            await c2.aclose()


@pytest.mark.parametrize(
    "status,reason,delay",
    [
        (429, "rate_limited", 3600),
        (503, "provider_transient", 30),
        (401, "provider_auth_required", None),
    ],
)
async def test_driver_provider_block_and_versioned_retry(
    page_runtime, monkeypatch, status, reason, delay
):
    from datetime import timedelta

    from apps.scheduler_worker.inbound_driver import InboundDriver
    from connectors.gmail.transport import GmailHttpStatusError
    from shared.schemas.identifiers import EmployeeId
    from tests.integration.test_email_inbound_page import NOW
    from workflows.reply_qualification.inbound_contracts import InboundPageError

    clock = [NOW]
    c = await composition(page_runtime, lambda: clock[0])
    tenant = page_runtime["route"].tenant_id
    boss = EmployeeId(
        next(i.employee_id for i in page_runtime["staff"] if i.role == "boss")
    )
    provider = page_runtime["provider"]
    original = provider.get_profile_history_id

    async def failure(*, token):
        await original(token=token)
        raise GmailHttpStatusError(
            status, feedback_retry_after_seconds=delay if status == 429 else None
        )

    monkeypatch.setattr(provider, "get_profile_history_id", failure)
    try:
        await c.management.bind(
            tenant, boss, page_runtime["route"].configured_identity_id
        )
        initial = await c.store.read_cursor()
        driver = InboundDriver(c, now=lambda: clock[0])
        assert await driver.scan_once() == 0
        blocked = await c.store.read_cursor()
        assert blocked.blocked_reason == reason and blocked.cursor == initial.cursor
        before = await provider.list_calls()
        assert await driver.scan_once() == 0 and await provider.list_calls() == before
        with pytest.raises(InboundPageError):
            await c.management.retry(tenant, boss, initial.version)
        if delay:
            assert (
                await c.management.retry(tenant, boss, blocked.version)
            ).state == "waiting"
            assert await c.store.read_cursor() == blocked
            clock[0] += timedelta(seconds=delay)
        monkeypatch.setattr(provider, "get_profile_history_id", original)
        await c.management.retry(tenant, boss, blocked.version)
        assert await driver.scan_once() == 1
    finally:
        await c.aclose()


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_real_scheduler_lost_backend_stops_inbound_and_later_phases(
    page_runtime, phase
):
    from sqlalchemy import text

    from apps.scheduler_worker.inbound_driver import InboundDriver
    from apps.scheduler_worker.main import (
        SchedulerConfig,
        SchedulerRuntime,
        WorkerStartStatus,
        run_scheduler_worker,
    )
    from infra.db.outbox_delivery import OutboxDeliverer
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.schemas.identifiers import EmployeeId
    from tests.integration.test_email_inbound_page import NOW

    c = await composition(page_runtime)
    tenant = page_runtime["route"].tenant_id
    boss = EmployeeId(
        next(i.employee_id for i in page_runtime["staff"] if i.role == "boss")
    )
    factory = page_runtime["factory"]
    lock_key = 7440159

    class LoseBackend:
        async def activate(self):
            async with factory() as session:
                # 仅终止本测试独占DB、具名lock_key的实际scheduler backend。
                pid = await session.scalar(
                    text(
                        "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=:k AND granted"
                    ),
                    {"k": lock_key},
                )
                assert pid
                assert await session.scalar(
                    text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                )

    class LoseAfter(InboundDriver):
        async def scan_once(self):
            result = await super().scan_once()
            await LoseBackend().activate()
            return result

    try:
        await c.management.bind(
            tenant, boss, page_runtime["route"].configured_identity_id
        )
        initial = await c.store.read_cursor()
        runtime = SchedulerRuntime(
            factory.kw["bind"],
            OutboxDeliverer(factory, tenant, now=lambda: NOW),
            PostgresWorkflowEngine(factory, {}, now=lambda: NOW),
            tenant,
            SchedulerConfig(1, 20, lock_key),
            activation=LoseBackend() if phase == "before" else None,
            inbound_driver=(LoseAfter if phase == "after" else InboundDriver)(
                c, now=lambda: NOW
            ),
        )
        result = await run_scheduler_worker(runtime, install_signal_handlers=False)
        assert (
            result.status is WorkerStartStatus.LOCK_LOST
            and result.cycles_completed == 0
        )
        current = await c.store.read_cursor()
        assert current.version == initial.version + (1 if phase == "after" else 0)
        calls = await page_runtime["provider"].list_calls()
        assert len([call for call in calls if call.operation == "profile"]) == (
            1 if phase == "after" else 0
        )
    finally:
        await c.aclose()


async def test_http_review_download_and_unarchived_or_missing_raw(page_runtime):
    from dataclasses import replace

    from httpx import ASGITransport, AsyncClient

    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from apps.scheduler_worker.inbound_driver import InboundDriver
    from shared.schemas.email_inbound import MIME_BYTES
    from shared.schemas.identifiers import EmployeeId, new_id
    from tests.integration.test_email_inbound_page import NOW
    from tests.unit.test_email_inbound import mime

    c = await composition(page_runtime)
    tenant = page_runtime["route"].tenant_id
    boss = EmployeeId(
        next(i.employee_id for i in page_runtime["staff"] if i.role == "boss")
    )
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=2),
        dependencies=replace(page_runtime["deps"], email_inbound=c),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://controlled.test",
            headers={"X-Tenant-Id": tenant, "X-Employee-Id": boss},
        ) as client:
            assert (
                await client.post(
                    "/email-inbound/binding",
                    json={"identity_id": page_runtime["route"].configured_identity_id},
                )
            ).status_code == 200
            raw = mime(message_id="<http-review@example.test>")
            await page_runtime["provider"].receive_inbound(raw, internal_date=NOW)
            await page_runtime["provider"].receive_inbound(
                b"X" * (MIME_BYTES + 1), internal_date=NOW
            )
            driver = InboundDriver(c, now=lambda: NOW)
            assert await driver.scan_once() == 1 and await driver.scan_once() == 1
            response = await client.get("/email-inbound/reviews")
            assert response.status_code == 200 and len(response.json()) == 2
            ready = next(r for r in response.json() if r["archived"])
            absent = next(r for r in response.json() if not r["archived"])
            for row in response.json():
                assert set(row) == {"review_id", "reason", "created_at", "archived"}
            path = f"/email-inbound/reviews/{ready['review_id']}/raw"
            download = await client.get(path)
            assert download.status_code == 200 and download.content == raw
            assert download.headers["content-type"] == "application/octet-stream"
            assert download.headers["x-content-type-options"] == "nosniff"
            assert (
                download.headers["content-disposition"]
                == 'attachment; filename="inbound-review.eml"'
            )
            assert (
                await client.get(f"/email-inbound/reviews/{absent['review_id']}/raw")
            ).status_code == 409
            assert (
                await client.get(f"/email-inbound/reviews/{new_id('irv')}/raw")
            ).status_code == 404
            reference = await c.management.authorize_raw(
                tenant, boss, ready["review_id"]
            )
            await c.objects.delete(f"raw/{tenant}/{reference.artifact_id}")
            assert (await client.get(path)).status_code == 503
            # 已归档事实仍保留，但对象当前不可读，不能伪造下载成功或补造Message。
            assert (
                await client.get("/email-inbound/reviews")
            ).json() == response.json()
    finally:
        await c.aclose()


async def test_expired_history_retries_only_in_place(page_runtime):
    from apps.scheduler_worker.inbound_driver import InboundDriver
    from shared.schemas.identifiers import EmployeeId
    from tests.integration.test_email_inbound_page import NOW

    c = await composition(page_runtime)
    tenant = page_runtime["route"].tenant_id
    boss = EmployeeId(
        next(i.employee_id for i in page_runtime["staff"] if i.role == "boss")
    )
    try:
        await c.management.bind(
            tenant, boss, page_runtime["route"].configured_identity_id
        )
        driver = InboundDriver(c, now=lambda: NOW)
        assert await driver.scan_once() == 1 and await driver.scan_once() == 1
        initial = await c.store.read_cursor()
        await page_runtime["provider"].expire_inbound_history(10000)
        assert await driver.scan_once() == 0
        blocked = await c.store.read_cursor()
        assert (
            blocked.blocked_reason == "provider_permanent"
            and blocked.cursor == initial.cursor
        )
        await c.management.retry(tenant, boss, blocked.version)
        assert await driver.scan_once() == 0
        final = await c.store.read_cursor()
        assert (
            final.blocked_reason == "provider_permanent"
            and final.cursor == initial.cursor
        )
    finally:
        await c.aclose()


async def test_auto_unsubscribe_candidates_and_technical_skips_keep_responsibilities(
    page_runtime,
):
    from apps.scheduler_worker.inbound_driver import InboundDriver
    from shared.schemas.identifiers import EmployeeId
    from tests.integration.test_email_inbound_page import NOW, counts
    from tests.unit.test_email_inbound import mime

    await prepare_sent(page_runtime)
    c = await composition(page_runtime)
    tenant = page_runtime["route"].tenant_id
    boss = EmployeeId(
        next(i.employee_id for i in page_runtime["staff"] if i.role == "boss")
    )
    try:
        await c.management.bind(
            tenant, boss, page_runtime["route"].configured_identity_id
        )
        for index, (headers, body, labels) in enumerate(
            [
                ("Auto-Submitted: auto-replied", "I am away", ("INBOX",)),
                ("", "Please unsubscribe me", ("INBOX",)),
                (
                    "Content-Type: multipart/report; report-type=delivery-status",
                    "report",
                    ("INBOX",),
                ),
                (
                    "Content-Type: multipart/report; report-type=feedback-report",
                    "report",
                    ("INBOX",),
                ),
                ("", "sent copy", ("SENT",)),
                ("", "draft copy", ("DRAFT",)),
            ]
        ):
            await page_runtime["provider"].receive_inbound(
                mime(
                    message_id=f"<disposition-{index}@example.test>",
                    reply=page_runtime["outbound"],
                    headers=headers,
                    body=body,
                ),
                internal_date=NOW,
                labels=labels,
            )
        driver = InboundDriver(c, now=lambda: NOW)
        assert await driver.scan_once() == 1 and await driver.scan_once() == 1
        assert await counts(page_runtime) == (2, 2, 6, 0)
        from sqlalchemy import text

        async with page_runtime["factory"]() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND event_type='ReplyReceived'"
                    ),
                    {"t": tenant},
                )
                == 0
            )
    finally:
        await c.aclose()
