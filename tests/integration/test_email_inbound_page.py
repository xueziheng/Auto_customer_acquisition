"""入站整页真实PG/MinIO；仅外部Provider响应合成，生命周期复用Task4。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.runtime_config import Phase1RuntimeSettings
from connectors.gmail.inbound_cursor import initial_inbound_cursor
from domains.sending_identity.schemas import DomainRole
from domains.sending_identity.service import (
    Actor,
    IdentityRegisterRequest,
    ScopeLevel,
    SendingIdentityScope,
)
from infra.controlled.providers import ControlledGmailTransport
from infra.db.session import create_engine_from
from shared.schemas.email_inbound import InboundRoute
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)

NOW = datetime(2026, 9, 5, tzinfo=UTC)


@pytest_asyncio.fixture
async def page_runtime(owned_infrastructure, tmp_path):  # noqa: F811 - 复用owned fixture
    config = owned_infrastructure.config
    tenant = TenantId(new_id("tn"))
    engine = create_engine_from(config.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    provider = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id=tenant)
    env = config.runtime_environment()
    env["TRADEOS_TENANT_ID"] = tenant
    deps = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(env),
        factory,
        now=lambda: NOW,
        secret_resolver=config,
        gmail_transport=provider,
    )
    try:
        boss = Actor(
            new_id("emp"), SendingIdentityScope(level=ScopeLevel.TENANT), "boss"
        )
        sid = await deps.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                "sender@tradeos-controlled.test",
                "tradeos-controlled.test",
                DomainRole.COLD_OUTREACH,
            ),
            actor=boss,
        )
        route = InboundRoute(
            tenant_id=tenant,
            mailbox_alias="primary",
            configured_identity_id=sid,
            route_id="controlled",
            config_version="v1",
        )
        yield {
            "factory": factory,
            "route": route,
            "deps": deps,
            "provider": provider,
            "config": config,
            "boss": boss,
        }
    finally:
        if deps.model_lifecycle:
            await deps.model_lifecycle.aclose()
        await engine.dispose()
        for suffix in ("", "-journal", "-wal", "-shm"):
            (tmp_path / ("mail.sqlite" + suffix)).unlink(missing_ok=True)


async def test_initial_cursor_is_durable_and_binding_cannot_change_identity(
    page_runtime,
):
    try:
        module = importlib.import_module("infra.db.repositories.email_inbound")
    except ModuleNotFoundError:
        pytest.fail("耐久入站cursor仓储缺失")
    runtime = page_runtime
    route = runtime["route"]
    repository = module.InboundStore(
        runtime["factory"], route.tenant_id, "primary", now=lambda: NOW
    )
    assert await repository.read_cursor() is None
    start = initial_inbound_cursor(
        route, NOW, int((NOW - timedelta(days=30)).timestamp())
    )
    first = await repository.bind(
        route,
        start,
        NOW,
        int((NOW - timedelta(days=30)).timestamp()),
        runtime["boss"].actor_id,
    )
    assert (await repository.read_cursor()) == first
    assert first.cursor == start and first.version == 1
    assert (
        await repository.bind(
            route,
            "ignored-later-initial",
            NOW,
            int(NOW.timestamp()),
            runtime["boss"].actor_id,
        )
        == first
    )
    other = await runtime["deps"].sending_identities.register(
        route.tenant_id,
        IdentityRegisterRequest(
            "other@tradeos-controlled.test",
            "tradeos-controlled.test",
            DomainRole.COLD_OUTREACH,
        ),
        actor=runtime["boss"],
    )
    from workflows.reply_qualification.inbound_contracts import InboundPageError

    with pytest.raises(InboundPageError):
        await repository.bind(
            route.model_copy(update={"configured_identity_id": other}),
            start,
            NOW,
            int(NOW.timestamp()),
            runtime["boss"].actor_id,
        )
    assert (await repository.read_cursor()) == first
    async with runtime["factory"]() as session:
        names = (
            (
                await session.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'email_inbound_%'"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(names) == [
        "email_inbound_cursors",
        "email_inbound_receipts",
        "email_inbound_reviews",
    ]


async def archived_page(runtime, raw_messages):
    """5a完整Gateway归档入口，仅Provider receive合成外部响应。"""
    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.gmail.inbound import GmailInboundReader
    from connectors.object_store.bounded import S3BoundedObjectBlobTransport
    from connectors.object_store.config import S3ObjectStoreSettings
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from infra.db.repositories.email_inbound import InboundStore
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
    from infra.email_inbound_artifacts import InboundRawArtifactArchiver
    from shared.schemas.email_inbound import MIME_BYTES
    from shared.schemas.evidence_read import ObjectReadLimits
    from shared.schemas.identifiers import UserId
    from tool_gateway.checks.email_inbound import InboundTenantCheck
    from tool_gateway.checks.permission import PermissionCheck
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.handlers.email_inbound import (
        MANIFEST,
        EmailInboundFetchHandler,
        ToolGatewayEmailInboundReader,
    )
    from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
    from tool_gateway.manifest import ToolRegistry
    from tool_gateway.pipeline import ToolGateway

    config, route, factory, provider = (
        runtime[k] for k in ("config", "route", "factory", "provider")
    )
    settings = S3ObjectStoreSettings(
        True,
        f"http://127.0.0.1:{config.object_port}",
        config.bucket,
        "CONTROLLED_OBJECT_ACCESS",
        "CONTROLLED_OBJECT_SECRET",
        "us-east-1",
        MIME_BYTES,
        MIME_BYTES,
    )
    transport = S3ObjectBlobTransport(settings, config)
    store = RawArtifactStoreImpl(
        lambda t: SqlAlchemyArtifactUnitOfWork(factory, t),
        transport,
        MIME_BYTES,
        lambda: NOW,
        new_id,
        bounded_transport=S3BoundedObjectBlobTransport(
            settings,
            config,
            limits=ObjectReadLimits(
                connect_timeout_ms=2000,
                read_timeout_ms=2000,
                total_timeout_ms=10000,
                maximum_attempts=1,
                chunk_bytes=65536,
            ),
        ),
    )
    slot = InboundPageSlot()
    user = UserId(new_id("usr"))
    registry = ToolRegistry()
    registry.register(
        MANIFEST,
        EmailInboundFetchHandler(
            route,
            lambda: GmailInboundReader(route, provider, config, "CONTROLLED_GMAIL"),
            InboundRawArtifactArchiver(store, store),
            CredentialMarkerGuard(),
            slot,
            HmacFingerprintProvider(
                "v1", bytes.fromhex(config.resolve("CONTROLLED_FINGERPRINT"))
            ),
        ),
    )

    async def authorize(ctx, state):
        return (
            ctx.user_id == user
            and ctx.tenant_id == route.tenant_id
            and state.manifest.required_permissions == ("email:inbound_read",)
        )

    gateway = ToolGateway(
        registry,
        {"tenant": InboundTenantCheck(route), "permission": PermissionCheck(authorize)},
        lambda t: SqlAlchemyToolGatewayUnitOfWork(factory, t, now=lambda: NOW),
        lease_duration=timedelta(seconds=60),
        lease_owner="inbound-page-tests",
        now=lambda: NOW,
        id_factory=new_id,
    )
    reader = ToolGatewayEmailInboundReader(gateway, slot, user, route)
    repository = InboundStore(
        factory, route.tenant_id, route.mailbox_alias, now=lambda: NOW
    )
    first = await repository.bind(
        route,
        initial_inbound_cursor(route, NOW, int((NOW - timedelta(days=30)).timestamp())),
        NOW,
        int((NOW - timedelta(days=30)).timestamp()),
        runtime["boss"].actor_id,
    )
    try:
        for raw in raw_messages:
            await provider.receive_inbound(raw, internal_date=NOW)
        anchor = await reader.fetch(
            route.tenant_id, route.mailbox_alias, first.cursor, 20
        )
        runtime.update(store=store, reader=reader, repository=repository)
        yield first, anchor
    finally:
        await transport.aclose()


async def test_unknown_page_is_atomic_replay_safe_and_has_real_raw(page_runtime):
    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification import inbound

    assert hasattr(inbound, "InboundPageProcessor"), "整页原子处理能力缺失"
    runtime = page_runtime
    async for first, anchor in archived_page(
        runtime, [mime(message_id="<unknown-one@example.test>")]
    ):
        from domains.outreach.permissions import (
            Actor,
            OutreachScope,
            Phase1OutreachAuthorizer,
            ScopeLevel,
            StandardAuditLogger,
        )
        from domains.outreach.service_impl import OutreachServiceImpl
        from infra.db.email_inbound_uow import SqlAlchemyInboundPageUnitOfWork

        def builder(factory, audit):
            return OutreachServiceImpl(
                factory,
                None,
                None,
                None,
                None,
                Phase1OutreachAuthorizer(runtime["route"].tenant_id),
                audit,
                now=lambda: NOW,
            )

        def actor(sid):
            return Actor(
                "system:email-inbound",
                OutreachScope(
                    level=ScopeLevel.SYSTEM,
                    allowed_sending_identity_ids=frozenset({sid}),
                ),
                "system",
            )

        processor = inbound.InboundPageProcessor(
            runtime["repository"],
            lambda expected: SqlAlchemyInboundPageUnitOfWork(
                runtime["repository"],
                expected,
                outreach_builder=builder,
                audit_sink=StandardAuditLogger(),
            ),
            actor,
        )
        assert await processor.process(first, anchor) == "committed"
        current = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            current.route.tenant_id, "primary", current.cursor, 20
        )
        assert await processor.process(current, page) == "committed"
        assert await processor.process(current, page) == "replayed"
        async with runtime["factory"]() as session:
            facts = (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM email_inbound_receipts WHERE tenant_id=:t), (SELECT count(*) FROM email_inbound_reviews WHERE tenant_id=:t), (SELECT count(*) FROM messages WHERE tenant_id=:t)"
                    ),
                    {"t": first.route.tenant_id},
                )
            ).one()
        assert tuple(facts) == (1, 1, 0)
        assert (await runtime["repository"].read_cursor()).cursor == page.next_cursor
        raw = page.items[0].raw
        assert (
            await runtime["store"].get_bounded(
                first.route.tenant_id, raw.artifact_id, maximum_bytes=4194304
            )
        )[1] == mime(message_id="<unknown-one@example.test>")


async def prepare_sent(runtime, *, reply_source=False):
    """真实公开登记/认证/验证/审批/发送；不插Attempt结果态。"""
    from httpx import ASGITransport, AsyncClient

    from apps.api.controlled import initialize_identities
    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from domains.approvals.service import ApprovalType, BlastRadius
    from domains.demand.schemas import SignalCaptureRequest
    from domains.demand.service_impl import DemandServiceImpl
    from domains.outreach.permissions import Actor as OA
    from domains.outreach.permissions import OutreachScope
    from domains.outreach.permissions import ScopeLevel as OS
    from domains.outreach.schemas import (
        CampaignCreateRequest,
        EnrollmentCreateRequest,
        SequenceStepRequest,
        StepIntent,
    )
    from domains.prospecting.schemas import (
        AccountResolveRequest,
        ContactCreateRequest,
        ContactPointCreateRequest,
        ContactPointKind,
        ContactType,
        LegalBasisInput,
        LegalBasisType,
        SubjectType,
        VerificationRecordRequest,
        VerificationStatus,
    )
    from domains.sending_identity.service import AuthenticationResult
    from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
    from shared.schemas.identifiers import EmployeeId, IdempotencyKey
    from shared.schemas.provenance import Provenance, SourceType

    deps, route = runtime["deps"], runtime["route"]
    sequence = runtime.get("prepared_count", 0)
    runtime["prepared_count"] = sequence + 1
    tenant, sid = route.tenant_id, route.configured_identity_id
    config = runtime["config"].model_copy(
        update={
            "tenant_id": tenant,
            "identities": tuple(
                i.model_copy(
                    update={"employee_id": new_id("emp"), "user_id": new_id("usr")}
                )
                for i in runtime["config"].identities
            ),
        }
    )
    if "staff" in runtime:
        config = config.model_copy(update={"identities": runtime["staff"]})
    await initialize_identities(config)
    runtime["staff"] = config.identities
    boss_id = next(i.employee_id for i in config.identities if i.role == "boss")
    sender_boss = Actor(boss_id, SendingIdentityScope(level=ScopeLevel.TENANT), "boss")
    if not runtime.get("sender_prepared"):
        await deps.sending_identities.begin_authentication(
            tenant, sid, actor=sender_boss
        )
        await deps.sending_identities.record_authentication_result(
            tenant,
            sid,
            AuthenticationResult(NOW, True, True, True, (), "controlled-auth"),
            actor=Actor(
                "system:inbound-test",
                SendingIdentityScope(
                    level=ScopeLevel.SYSTEM, allowed_identity_ids=frozenset({sid})
                ),
                "system",
            ),
        )
        await deps.sending_identities.start_warmup(tenant, sid, 5, actor=sender_boss)
        runtime["sender_prepared"] = True
    boss = OA(boss_id, OutreachScope(level=OS.TENANT), "boss")
    account = await deps.prospecting.resolve_account(
        tenant,
        AccountResolveRequest(
            f"Controlled buyer {sequence}",
            "DE",
            entity_type="manufacturer",
            field_provenance=(
                {
                    name: Provenance(
                        source_type=SourceType.EMPLOYEE_INPUT,
                        source_id=boss_id,
                        extracted_by=boss_id,
                        extracted_at=NOW,
                        confirmed_by=boss_id,
                        confirmed_at=NOW,
                    )
                    for name in ("name", "country")
                }
                if reply_source
                else {}
            ),
        ),
    )
    contact = await deps.prospecting.create_contact(
        tenant, ContactCreateRequest(account)
    )
    point = await deps.prospecting.add_contact_point(
        tenant,
        ContactPointCreateRequest(
            contact,
            ContactPointKind.EMAIL,
            f"buyer{sequence}@example.test",
            LegalBasisInput(
                LegalBasisType.LEGITIMATE_INTEREST,
                SubjectType.LEGAL_ENTITY,
                ContactType.PERSONAL_BUSINESS,
                "website",
                NOW,
                assessment_ref="controlled-lia",
            ),
        ),
    )
    await deps.prospecting.record_verification(
        tenant,
        VerificationRecordRequest(
            point, VerificationStatus.VERIFIED, "controlled", NOW, "no external cost"
        ),
    )
    demand = DemandServiceImpl(
        lambda t: SqlAlchemyDemandUnitOfWork(runtime["factory"], t, now=lambda: NOW),
        now=lambda: NOW,
    )
    signal = await demand.capture_signal(
        tenant,
        SignalCaptureRequest(
            "product_line_expansion",
            "Controlled buyer",
            "new facility",
            NOW,
            "employee_input",
            "controlled-inbound",
            "human",
        ),
    )
    hypothesis_id = await demand.create_hypothesis(
        tenant,
        account,
        "hinges",
        [str(signal)],
        "possible facility requirement",
        "controlled-model",
    )
    campaign = await deps.outreach.create_campaign(
        tenant,
        CampaignCreateRequest(
            "Controlled discovery",
            ("DE",),
            ("manufacturer",),
            ("hinges",),
            (sid,),
            (
                (
                    SequenceStepRequest(1, StepIntent.DISCOVERY, 0),
                    SequenceStepRequest(2, StepIntent.FOLLOW_UP, 7),
                )
                if reply_source
                else (SequenceStepRequest(1, StepIntent.DISCOVERY, 0),)
            ),
            5,
            5,
            (),
        ),
        actor=boss,
    )
    await deps.outreach.submit_campaign(tenant, campaign.campaign_id, actor=boss)
    approval = await deps.approvals.submit(
        tenant,
        ApprovalType.CAMPAIGN_BOUNDARY_CHANGE,
        "受控范围",
        {"version": 1},
        "核对范围",
        BlastRadius(["Campaign"], "启用", "停止", True),
        proposed_by_employee=EmployeeId(
            next(i.employee_id for i in config.identities if i.role == "manager")
        ),
        change_set_ref=f"campaign:{campaign.campaign_id}:v1",
    )
    await deps.approvals.decide(tenant, approval, True, EmployeeId(boss_id))
    await deps.outreach.activate_campaign(tenant, campaign.campaign_id, actor=boss)
    enrollment = await deps.outreach.enroll(
        tenant,
        campaign.campaign_id,
        EnrollmentCreateRequest(
            account,
            point,
            IdempotencyKey(f"controlled-inbound-send-{sequence}"),
            source_hypothesis_id=hypothesis_id if reply_source else None,
        ),
        actor=boss,
    )
    system = OA(
        "system:inbound-test",
        OutreachScope(
            level=OS.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    attempt = await deps.outreach.prepare_message_attempt(
        tenant, enrollment.enrollment_id, actor=system
    )
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=2),
        dependencies=deps,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://controlled.test"
    ) as client:
        response = await client.post(
            f"/crm/message-attempts/{attempt.attempt_id}/send",
            headers={"X-Tenant-Id": tenant, "X-Employee-Id": boss_id},
            json={
                "subject": "Current supply needs",
                "body": "Which components are you currently looking for?",
            },
        )
    assert response.status_code == 200, (response.status_code, response.json())
    async with runtime["factory"]() as session:
        result = (
            await session.execute(
                text(
                    "SELECT state,deterministic_message_id FROM outreach_message_attempts WHERE tenant_id=:t AND attempt_id=:a"
                ),
                {"t": tenant, "a": attempt.attempt_id},
            )
        ).one()
    assert result.state == "sent"
    runtime["outbound"] = result.deterministic_message_id
    if reply_source:
        runtime["source_hypothesis_id"] = hypothesis_id
        runtime["source_enrollment_id"] = enrollment.enrollment_id
        runtime["source_account_id"] = account
    return result


async def test_real_sent_reply_ingests_message(page_runtime):
    from domains.outreach.permissions import (
        Actor,
        OutreachScope,
        Phase1OutreachAuthorizer,
        ScopeLevel,
        StandardAuditLogger,
    )
    from domains.outreach.service_impl import OutreachServiceImpl
    from infra.db.email_inbound_uow import SqlAlchemyInboundPageUnitOfWork
    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification.inbound import InboundPageProcessor

    runtime = page_runtime
    await prepare_sent(runtime)
    raw = mime(message_id="<real-one@example.test>", reply=runtime["outbound"])
    async for first, anchor in archived_page(runtime, [raw]):

        def builder(factory, audit):
            return OutreachServiceImpl(
                factory,
                None,
                None,
                None,
                None,
                Phase1OutreachAuthorizer(runtime["route"].tenant_id),
                audit,
                now=lambda: NOW,
            )

        def actor(sid):
            return Actor(
                "system:email-inbound",
                OutreachScope(
                    level=ScopeLevel.SYSTEM,
                    allowed_sending_identity_ids=frozenset({sid}),
                ),
                "system",
            )

        processor = InboundPageProcessor(
            runtime["repository"],
            lambda e: SqlAlchemyInboundPageUnitOfWork(
                runtime["repository"],
                e,
                outreach_builder=builder,
                audit_sink=StandardAuditLogger(),
            ),
            actor,
        )
        await processor.process(first, anchor)
        current = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            current.route.tenant_id, "primary", current.cursor, 20
        )
        await processor.process(current, page)
        async with runtime["factory"]() as session:
            messages = (
                await session.execute(
                    text(
                        "SELECT message_id, outbound_message_id FROM messages WHERE tenant_id=:t"
                    ),
                    {"t": first.route.tenant_id},
                )
            ).all()
            events = await session.scalar(
                text(
                    "SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND event_type='InboundMessageStored'"
                ),
                {"t": first.route.tenant_id},
            )
        assert len(messages) == 1 and events == 1
        assert messages[0][1] == runtime["outbound"]


def processor_for(runtime, *, page_store=None, audit=None):
    from domains.outreach.permissions import (
        Actor,
        OutreachScope,
        Phase1OutreachAuthorizer,
        ScopeLevel,
        StandardAuditLogger,
    )
    from domains.outreach.service_impl import OutreachServiceImpl
    from infra.db.email_inbound_uow import SqlAlchemyInboundPageUnitOfWork
    from workflows.reply_qualification.inbound import InboundPageProcessor

    def builder(factory, sink):
        return OutreachServiceImpl(
            factory,
            None,
            None,
            None,
            None,
            Phase1OutreachAuthorizer(runtime["route"].tenant_id),
            sink,
            now=lambda: NOW,
        )

    def actor(sid):
        return Actor(
            "system:email-inbound",
            OutreachScope(
                level=ScopeLevel.SYSTEM, allowed_sending_identity_ids=frozenset({sid})
            ),
            "system",
        )

    return InboundPageProcessor(
        runtime["repository"],
        lambda e: SqlAlchemyInboundPageUnitOfWork(
            page_store or runtime["repository"],
            e,
            outreach_builder=builder,
            audit_sink=audit or StandardAuditLogger(),
        ),
        actor,
    )


async def counts(runtime):
    async with runtime["factory"]() as session:
        return tuple(
            (
                await session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM messages WHERE tenant_id=:t), (SELECT count(*) FROM outbox_events WHERE tenant_id=:t AND event_type='InboundMessageStored'), (SELECT count(*) FROM email_inbound_receipts WHERE tenant_id=:t), (SELECT count(*) FROM email_inbound_reviews WHERE tenant_id=:t)"
                    ),
                    {"t": runtime["route"].tenant_id},
                )
            ).one()
        )


async def test_second_receipt_failure_rolls_back_messages_events_cursor(page_runtime):
    from sqlalchemy.exc import DBAPIError

    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    await prepare_sent(runtime)
    raws = [
        mime(message_id=f"<atomic-{i}@example.test>", reply=runtime["outbound"])
        for i in range(2)
    ]
    async for first, anchor in archived_page(runtime, raws):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        before = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", before.cursor, 20
        )
        poison = sorted(i.provider_ref_digest for i in page.items)[1]
        async with runtime["factory"].begin() as session:
            await session.execute(
                text(
                    "CREATE FUNCTION inbound_test_reject() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.provider_ref_digest = '"
                    + poison
                    + "' THEN RAISE EXCEPTION 'controlled failure'; END IF; RETURN NEW; END $$"
                )
            )
            await session.execute(
                text(
                    "CREATE TRIGGER inbound_test_reject BEFORE INSERT ON email_inbound_receipts FOR EACH ROW EXECUTE FUNCTION inbound_test_reject()"
                )
            )
        try:
            with pytest.raises(DBAPIError):
                await processor.process(before, page)
            assert await counts(runtime) == (0, 0, 0, 0)
            assert await runtime["repository"].read_cursor() == before
            for item, raw in zip(page.items, raws, strict=True):
                assert (
                    await runtime["store"].get_bounded(
                        first.route.tenant_id,
                        item.raw.artifact_id,
                        maximum_bytes=4194304,
                    )
                )[1] == raw
        finally:
            async with runtime["factory"].begin() as session:
                await session.execute(
                    text("DROP TRIGGER inbound_test_reject ON email_inbound_receipts")
                )
                await session.execute(text("DROP FUNCTION inbound_test_reject()"))
        await processor.process(before, page)
        assert await counts(runtime) == (2, 2, 2, 0)


async def test_two_sessions_replay_and_stale_cas(page_runtime):
    import asyncio

    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification.inbound_contracts import InboundPageError

    runtime = page_runtime
    await prepare_sent(runtime)
    async for first, anchor in archived_page(
        runtime, [mime(reply=runtime["outbound"])]
    ):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        before = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", before.cursor, 20
        )
        assert sorted(
            await asyncio.gather(
                processor.process(before, page),
                processor_for(runtime).process(before, page),
            )
        ) == ["committed", "replayed"]
        assert await counts(runtime) == (1, 1, 1, 0)
        with pytest.raises(InboundPageError, match="cursor_conflict"):
            await processor.process(first, anchor)
        assert await counts(runtime) == (1, 1, 1, 0)


@pytest.mark.parametrize(
    "failure",
    [
        "after_commit",
        "before_commit",
        "close",
        "cancel_commit",
        "cancel_before_commit_close",
        "cancel_after_commit_close",
    ],
)
async def test_uncertain_commit_verifies_durable_database(page_runtime, failure):
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession

    from infra.db.repositories.email_inbound import InboundStore
    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification.inbound_contracts import InboundCommitUnknown

    runtime = page_runtime
    await prepare_sent(runtime)
    async for first, anchor in archived_page(
        runtime, [mime(reply=runtime["outbound"])]
    ):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        before = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", before.cursor, 20
        )

        class FaultSession(AsyncSession):
            async def commit(self):
                if failure == "before_commit":
                    raise RuntimeError("controlled commit failure")
                if failure in {"cancel_commit", "cancel_before_commit_close"}:
                    raise asyncio.CancelledError()
                await super().commit()
                if failure == "cancel_after_commit_close":
                    raise asyncio.CancelledError()
                if failure == "after_commit":
                    raise RuntimeError("controlled unknown commit")

            async def close(self):
                await super().close()
                if failure in {
                    "close",
                    "cancel_before_commit_close",
                    "cancel_after_commit_close",
                }:
                    raise RuntimeError("controlled close failure")

        faulty = InboundStore(
            async_sessionmaker(
                runtime["factory"].kw["bind"],
                class_=FaultSession,
                expire_on_commit=False,
            ),
            first.route.tenant_id,
            "primary",
            now=lambda: NOW,
        )
        if failure in {"cancel_before_commit_close", "cancel_after_commit_close"}:
            with pytest.raises(asyncio.CancelledError):
                await processor_for(runtime, page_store=faulty).process(before, page)
            committed = failure == "cancel_after_commit_close"
            assert await counts(runtime) == (
                (1, 1, 1, 0) if committed else (0, 0, 0, 0)
            )
            durable = await runtime["repository"].read_cursor()
            if committed:
                assert (
                    durable.cursor == page.next_cursor
                    and durable.version == before.version + 1
                )
                assert await processor.process(before, page) == "replayed"
            else:
                assert durable == before
                assert await processor.process(before, page) == "committed"
            assert await counts(runtime) == (1, 1, 1, 0)
            assert await processor.process(before, page) == "replayed"
            assert await counts(runtime) == (1, 1, 1, 0)
        elif failure in {"after_commit", "close"}:
            assert (
                await processor_for(runtime, page_store=faulty).process(before, page)
                == "replayed"
            )
            assert await counts(runtime) == (1, 1, 1, 0)
            assert (
                await runtime["repository"].read_cursor()
            ).cursor == page.next_cursor
        else:
            with pytest.raises(
                asyncio.CancelledError
                if failure == "cancel_commit"
                else InboundCommitUnknown
            ):
                await processor_for(runtime, page_store=faulty).process(before, page)
            assert await counts(runtime) == (0, 0, 0, 0)
            assert await runtime["repository"].read_cursor() == before


async def test_postcommit_log_failure_does_not_repeat_business(page_runtime, caplog):
    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    await prepare_sent(runtime)
    async for first, anchor in archived_page(
        runtime, [mime(reply=runtime["outbound"])]
    ):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        before = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", before.cursor, 20
        )

        class FailedLog:
            def log(self, **values):
                raise RuntimeError("controlled log failure")

        assert (
            await processor_for(runtime, audit=FailedLog()).process(before, page)
            == "committed"
        )
        assert await processor.process(before, page) == "replayed"
        assert await counts(runtime) == (1, 1, 1, 0)
        assert "入站提交后审计刷新失败" in caplog.text


@pytest.mark.parametrize(
    "conflict", ["provider_content", "rfc_content", "rfc_time", "rfc_outbound"]
)
async def test_poison_conflict_preserves_winner_and_entire_page(page_runtime, conflict):
    import hashlib
    import sqlite3
    from contextlib import closing

    from shared.errors import ValidationError
    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification.inbound_contracts import InboundPageError

    runtime = page_runtime
    await prepare_sent(runtime)
    original = mime(message_id="<conflict@example.test>", reply=runtime["outbound"])
    async for first, anchor in archived_page(runtime, [original]):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        current = await runtime["repository"].read_cursor()
        first_page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", current.cursor, 20
        )
        await processor.process(current, first_page)
        before = await runtime["repository"].read_cursor()
        changed = (
            original.replace(b"We need", b"We require")
            if conflict != "rfc_time"
            else original.replace(b"10:00:00", b"11:00:00")
        )
        if changed == original:
            changed = original.replace(b"\r\n\r\n", b"\r\nX-Fixture: changed\r\n\r\n")
        if conflict == "rfc_outbound":
            old_outbound = runtime["outbound"]
            await prepare_sent(runtime)
            changed = original.replace(
                old_outbound.encode(), runtime["outbound"].encode()
            )
            assert changed != original
        if conflict == "provider_content":
            # 只改本owner的外部Provider响应：同ID再现不同内容及新history事件。
            ref = "controlled-inbound-" + hashlib.sha256(original).hexdigest()[:24]
            with closing(sqlite3.connect(runtime["provider"]._path)) as db, db:
                db.execute(
                    "UPDATE provider_inbound SET body=?, ordinal=(SELECT max(ordinal)+1 FROM provider_inbound WHERE tenant_id=?) WHERE tenant_id=? AND ref=?",
                    (changed, first.route.tenant_id, first.route.tenant_id, ref),
                )
        else:
            await runtime["provider"].receive_inbound(changed, internal_date=NOW)
        await runtime["provider"].receive_inbound(
            mime(message_id="<later@example.test>", reply=runtime["outbound"]),
            internal_date=NOW,
        )
        page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", before.cursor, 20
        )
        with pytest.raises((InboundPageError, ValidationError)):
            await processor.process(before, page)
        assert await counts(runtime) == (1, 1, 1, 0)
        assert await runtime["repository"].read_cursor() == before
        assert (
            await runtime["store"].get_bounded(
                first.route.tenant_id,
                first_page.items[0].raw.artifact_id,
                maximum_bytes=4194304,
            )
        )[1] == original


async def test_cancel_during_real_pg_wait_rolls_back_page(page_runtime):
    import asyncio

    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    await prepare_sent(runtime)
    async for first, anchor in archived_page(
        runtime, [mime(reply=runtime["outbound"])]
    ):
        processor = processor_for(runtime)
        await processor.process(first, anchor)
        before = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            first.route.tenant_id, "primary", before.cursor, 20
        )
        async with runtime["factory"]() as blocker:
            await blocker.execute(
                text("LOCK TABLE email_inbound_receipts IN ACCESS EXCLUSIVE MODE")
            )
            task = asyncio.create_task(processor.process(before, page))
            try:
                waiting = False
                for _ in range(100):
                    async with runtime["factory"]() as observer:
                        waiting = bool(
                            await observer.scalar(
                                text(
                                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%email_inbound_receipts%')"
                                )
                            )
                        )
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
                assert waiting
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await blocker.rollback()
        assert await counts(runtime) == (0, 0, 0, 0)
        assert await runtime["repository"].read_cursor() == before


async def test_same_rfc_across_two_mailboxes_is_one_domain_message(page_runtime):
    import asyncio

    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    await prepare_sent(runtime)
    raw = mime(reply=runtime["outbound"])
    peer = dict(
        runtime,
        route=runtime["route"].model_copy(update={"mailbox_alias": "secondary"}),
    )
    async for first, anchor in archived_page(runtime, [raw]):
        async for second, second_anchor in archived_page(peer, [raw]):
            p1, p2 = processor_for(runtime), processor_for(peer)
            await p1.process(first, anchor)
            await p2.process(second, second_anchor)
            c1, c2 = (
                await runtime["repository"].read_cursor(),
                await peer["repository"].read_cursor(),
            )
            a = await runtime["reader"].fetch(
                first.route.tenant_id, "primary", c1.cursor, 20
            )
            b = await peer["reader"].fetch(
                first.route.tenant_id, "secondary", c2.cursor, 20
            )
            assert await asyncio.gather(p1.process(c1, a), p2.process(c2, b)) == [
                "committed",
                "committed",
            ]
            assert await counts(runtime) == (1, 1, 2, 0)


async def test_retry_cas_cannot_bypass_provider_wait_or_new_failure(page_runtime):
    from infra.db.repositories.email_inbound import InboundStore
    from workflows.reply_qualification.inbound_contracts import InboundPageError

    assert hasattr(InboundStore, "mark_failure"), "耐久阻断和原位重试尚未实现"
    runtime = page_runtime
    route = runtime["route"]
    current_time = [NOW]
    store = InboundStore(
        runtime["factory"], route.tenant_id, "primary", now=lambda: current_time[0]
    )
    start = initial_inbound_cursor(
        route, NOW, int((NOW - timedelta(days=30)).timestamp())
    )
    first = await store.bind(
        route,
        start,
        NOW,
        int((NOW - timedelta(days=30)).timestamp()),
        runtime["boss"].actor_id,
    )
    await store.mark_failure(first, "provider_transient", NOW + timedelta(seconds=120))
    waiting = await store.read_cursor()
    assert waiting.version == 2
    assert await store.retry(waiting.version) == waiting
    with pytest.raises(InboundPageError, match="cursor_conflict"):
        await store.retry(first.version)
    current_time[0] += timedelta(seconds=120)
    resumed = await store.retry(waiting.version)
    assert (
        resumed.cursor == first.cursor
        and resumed.next_retry_at is None
        and resumed.version == 3
    )
    assert await store.mark_failure(first, "page_integrity", None) is False
    assert await store.read_cursor() == resumed


async def test_buffered_domain_audit_failure_rolls_back_entire_page(
    page_runtime, monkeypatch
):
    from infra.db.email_feedback_uow import _TransactionAwareAudit
    from tests.unit.test_email_inbound import mime

    runtime = page_runtime
    await prepare_sent(runtime)
    raw = [
        mime(message_id=f"<audit-{i}@example.test>", reply=runtime["outbound"])
        for i in range(2)
    ]
    async for initial, anchor in archived_page(runtime, raw):
        processor = processor_for(runtime)
        await processor.process(initial, anchor)
        current = await runtime["repository"].read_cursor()
        page = await runtime["reader"].fetch(
            current.route.tenant_id, "primary", current.cursor, 20
        )
        original = _TransactionAwareAudit.log
        calls = 0

        def audit(self, _original=original, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("controlled_audit_buffer_failure")
            return _original(self, **kwargs)

        monkeypatch.setattr(_TransactionAwareAudit, "log", audit)
        with pytest.raises(RuntimeError, match="controlled_audit_buffer_failure"):
            await processor.process(current, page)
        assert calls == 2 and await counts(runtime) == (0, 0, 0, 0)
        assert await runtime["repository"].read_cursor() == current
        monkeypatch.setattr(_TransactionAwareAudit, "log", original)
        await processor.process(current, page)
        assert await counts(runtime) == (2, 2, 2, 0)


async def test_actual_other_tenant_sent_is_review_and_wrong_tenant_page_rejected(
    page_runtime, tmp_path
):
    from tests.unit.test_email_inbound import mime
    from workflows.reply_qualification.inbound_contracts import InboundPageError

    runtime = page_runtime
    await prepare_sent(runtime)
    foreign_tenant = TenantId(new_id("tn"))
    env = runtime["config"].runtime_environment()
    env["TRADEOS_TENANT_ID"] = foreign_tenant
    provider = ControlledGmailTransport(
        tmp_path / "foreign.sqlite", tenant_id=foreign_tenant
    )
    deps = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(env),
        runtime["factory"],
        now=lambda: NOW,
        secret_resolver=runtime["config"],
        gmail_transport=provider,
    )
    try:
        sid = await deps.sending_identities.register(
            foreign_tenant,
            IdentityRegisterRequest(
                "foreign@tradeos-controlled.test",
                "tradeos-controlled.test",
                DomainRole.COLD_OUTREACH,
            ),
            actor=runtime["boss"],
        )
        other = {
            **runtime,
            "route": runtime["route"].model_copy(
                update={"tenant_id": foreign_tenant, "configured_identity_id": sid}
            ),
            "provider": provider,
            "deps": deps,
        }
        async for initial, anchor in archived_page(
            other,
            [mime(message_id="<cross-tenant@example.test>", reply=runtime["outbound"])],
        ):
            processor = processor_for(other)
            with pytest.raises(InboundPageError, match="page_integrity"):
                await processor.process(
                    initial, anchor.model_copy(update={"route": runtime["route"]})
                )
            assert await other["repository"].read_cursor() == initial
            await processor.process(initial, anchor)
            current = await other["repository"].read_cursor()
            page = await other["reader"].fetch(
                foreign_tenant, "primary", current.cursor, 20
            )
            await processor.process(current, page)
            assert await counts(other) == (0, 0, 1, 1)
    finally:
        if deps.model_lifecycle:
            await deps.model_lifecycle.aclose()
