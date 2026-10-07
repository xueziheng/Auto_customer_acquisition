"""本owner PG/MinIO及持久Provider场景通过真实Connector/Gateway；无业务替身。"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.gmail.inbound import GmailInboundReader
from connectors.gmail.inbound_cursor import decode_cursor, initial_inbound_cursor
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import S3ObjectBlobTransport
from infra.controlled.providers import ControlledGmailTransport
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tables import RawArtifactRow, ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.email_inbound_artifacts import InboundRawArtifactArchiver
from scripts.controlled_web_supervisor import Supervisor
from scripts.run_web_core_controlled import reserve
from shared.schemas.email_inbound import (
    MIME_BYTES,
    ArchivedInboundPage,
    InboundDisposition,
    InboundError,
    InboundRoute,
)
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import SendingIdentityId, TenantId, UserId, new_id
from tests.runtime_database_fixtures import (
    runtime_database_url as _runtime_database_url,
)
from tests.unit.test_email_inbound import DATE, REPLY, mime
from tool_gateway.checks.email_inbound import InboundTenantCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_inbound import (
    MANIFEST,
    EmailInboundFetchHandler,
    ToolGatewayEmailInboundReader,
)
from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallContext, ToolGateway

ROOT = Path(__file__).resolve().parents[2]
runtime_database_url = _runtime_database_url


@pytest.fixture(scope="module")
def owned_infrastructure(tmp_path_factory):
    parent = tmp_path_factory.mktemp("inbound-owned")
    listeners = [reserve(0) for _ in range(3)]
    supervisor = None
    try:
        supervisor = Supervisor(ROOT, parent, listeners)
        supervisor.start_infrastructure()
        yield supervisor
    finally:
        if supervisor is not None:
            supervisor.close()
            assert supervisor.cleanup_errors == []
            assert not (supervisor.directory / "config.json").exists()
        else:
            for listener in listeners:
                listener.close()


@pytest_asyncio.fixture
async def runtime(owned_infrastructure, tmp_path, runtime_database_url):
    config = owned_infrastructure.config
    tenant = TenantId(new_id("tn"))
    # 每例独立企业运行角色；owned supervisor 仅提供本机对象存储和受控凭证。
    engine = create_engine_from(await runtime_database_url(str(tenant)))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    transport = None
    try:
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
            lambda tenant: SqlAlchemyArtifactUnitOfWork(factory, tenant),
            transport,
            MIME_BYTES,
            lambda: datetime.now(UTC),
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
        route = InboundRoute(
            tenant_id=tenant,
            mailbox_alias="primary",
            configured_identity_id=SendingIdentityId(new_id("sid")),
            route_id="controlled",
            config_version="v1",
        )
        provider = ControlledGmailTransport(
            tmp_path / "provider.sqlite", tenant_id=route.tenant_id
        )
        slot = InboundPageSlot()
        user = UserId(new_id("usr"))
        resolves = []

        class Secrets:
            def resolve(self, reference):
                resolves.append(reference)
                return config.resolve(reference)

        handler = EmailInboundFetchHandler(
            route,
            lambda: GmailInboundReader(route, provider, Secrets(), "CONTROLLED_GMAIL"),
            InboundRawArtifactArchiver(store, store),
            CredentialMarkerGuard(),
            slot,
            HmacFingerprintProvider(
                "v1", bytes.fromhex(config.resolve("CONTROLLED_FINGERPRINT"))
            ),
        )
        registry = ToolRegistry()
        registry.register(MANIFEST, handler)

        async def authorize(ctx, state):
            return (
                ctx.user_id == user
                and ctx.tenant_id == route.tenant_id
                and state.manifest.required_permissions == ("email:inbound_read",)
            )

        gateway = ToolGateway(
            registry,
            {
                "tenant": InboundTenantCheck(route),
                "permission": PermissionCheck(authorize),
            },
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
                factory, tenant, now=lambda: datetime.now(UTC)
            ),
            lease_duration=timedelta(seconds=60),
            lease_owner="inbound-tests",
            now=lambda: datetime.now(UTC),
            id_factory=new_id,
        )
        reader = ToolGatewayEmailInboundReader(gateway, slot, user, route)
        start = initial_inbound_cursor(
            route, DATE, int((DATE - timedelta(days=30)).timestamp())
        )
        yield {
            "route": route,
            "provider": provider,
            "slot": slot,
            "reader": reader,
            "gateway": gateway,
            "handler": handler,
            "store": store,
            "transport": transport,
            "factory": factory,
            "resolves": resolves,
            "start": start,
            "user": user,
        }
    finally:
        if transport is not None:
            await transport.aclose()
        await engine.dispose()
        for suffix in ("", "-journal", "-wal", "-shm"):
            (tmp_path / ("provider.sqlite" + suffix)).unlink(missing_ok=True)


async def fetch(runtime, cursor, limit=20):
    return await runtime["reader"].fetch(
        runtime["route"].tenant_id, "primary", cursor, limit
    )


@pytest.mark.asyncio
async def test_real_archive_matrix_replay_and_sensitive_ledger(runtime, caplog):
    provider, route = runtime["provider"], runtime["route"]
    raws = [
        mime(),
        mime(headers="Auto-Submitted: auto-replied", body="away"),
        mime(body="unsubscribe"),
        mime(headers="Content-Type: multipart/report; report-type=delivery-status"),
        mime(headers="Content-Type: multipart/report; report-type=feedback-report"),
        mime(message_id=None),
        mime(body="text " * 70000 + "cookie"),
        mime(body="text " * 70000),
        mime(
            headers="Content-Type: text/html",
            body="<p>hello</p><script>cookie</script>",
        ),
    ]
    for raw in raws:
        await provider.receive_inbound(raw, internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    assert anchor.items == ()
    assert [c.operation for c in await provider.list_calls()] == ["profile"]
    page = await fetch(runtime, anchor.next_cursor)
    assert [i.disposition for i in page.items] == [
        InboundDisposition.CANDIDATE,
        InboundDisposition.CANDIDATE,
        InboundDisposition.CANDIDATE,
        InboundDisposition.SKIPPED_DELIVERY_REPORT,
        InboundDisposition.SKIPPED_DELIVERY_REPORT,
        InboundDisposition.MISSING_MESSAGE_ID,
        InboundDisposition.CREDENTIAL_MARKER,
        InboundDisposition.TEXT_TOO_LARGE,
        InboundDisposition.CREDENTIAL_MARKER,
    ]
    for item, raw in zip(page.items, raws, strict=True):
        assert item.raw is not None
        meta, actual = await runtime["store"].get_bounded(
            route.tenant_id, item.raw.artifact_id, maximum_bytes=MIME_BYTES
        )
        assert actual == raw and meta.content_hash == hashlib.sha256(raw).hexdigest()
    again = await fetch(runtime, anchor.next_cursor)
    assert again == page
    assert runtime["slot"].is_empty
    async with runtime["factory"]() as session:
        rows = (
            await session.scalars(
                select(RawArtifactRow).where(
                    RawArtifactRow.tenant_id == route.tenant_id
                )
            )
        ).all()
        assert len(rows) == len(raws)
        calls = (
            await session.scalars(
                select(ToolCallRow).where(ToolCallRow.tenant_id == route.tenant_id)
            )
        ).all()
        events = (
            await session.scalars(
                select(ToolCallEventRow).where(
                    ToolCallEventRow.tenant_id == route.tenant_id
                )
            )
        ).all()
        rendered = repr(
            [
                {
                    column.name: getattr(row, column.name)
                    for column in row.__table__.columns
                }
                for row in [*calls, *events]
            ]
        )
    for forbidden in (
        REPLY,
        "buyer@example.invalid",
        "We need",
        "cookie",
        runtime["start"],
        anchor.next_cursor,
    ):
        assert (
            forbidden
            not in rendered + caplog.text + repr(page) + page.model_dump_json()
        )


@pytest.mark.asyncio
async def test_gateway_denial_zero_provider_and_lazy_secret(runtime):
    route = runtime["route"]
    params = {"mailbox_alias": "primary", "cursor": runtime["start"], "page_limit": 20}
    for user, tenant, parameters in [
        (UserId(new_id("usr")), route.tenant_id, params),
        (runtime["user"], route.tenant_id, {**params, "identity": "override"}),
        (runtime["user"], route.tenant_id, {**params, "page_limit": 21}),
    ]:
        result = await runtime["gateway"].invoke(
            ToolCallContext(
                tenant_id=tenant,
                user_id=user,
                tool_id=MANIFEST.tool_id,
                params=parameters,
            )
        )
        assert result.status != ToolCallStatus.SUCCEEDED
    # 错企业在真实 RLS 的 ledger 写入处即拒绝，仍须保证零 Provider/凭证调用。
    with pytest.raises(DBAPIError) as denied:
        await runtime["gateway"].invoke(
            ToolCallContext(
                tenant_id=TenantId(new_id("tn")),
                user_id=runtime["user"],
                tool_id=MANIFEST.tool_id,
                params=params,
            )
        )
    assert getattr(denied.value.orig, "sqlstate", None) == "42501"
    assert runtime["resolves"] == []
    assert await runtime["provider"].list_calls() == ()
    assert runtime["slot"].is_empty


@pytest.mark.asyncio
async def test_page_budget_pending_history_and_provider_reconstruction(runtime):
    provider = runtime["provider"]
    for i in range(23):
        await provider.receive_inbound(
            mime(message_id=f"<m{i}@example.invalid>"), internal_date=DATE
        )
    anchor = await fetch(runtime, runtime["start"])
    first = await fetch(runtime, anchor.next_cursor, 20)
    cursor = decode_cursor(first.next_cursor, runtime["route"])
    assert (
        len(first.items) == 20
        and len(cursor.pending) == 3
        and cursor.phase == "bootstrap"
    )
    second = await fetch(runtime, first.next_cursor)
    assert (
        len(second.items) == 3
        and decode_cursor(second.next_cursor, runtime["route"]).phase == "history"
    )
    await provider.receive_inbound(
        mime(message_id="<new@example.invalid>"), internal_date=DATE
    )
    third = await fetch(runtime, second.next_cursor)
    assert len(third.items) == 1
    other = ControlledGmailTransport(
        provider._path, tenant_id=runtime["route"].tenant_id
    )
    assert len(await other.list_calls()) == len(await provider.list_calls())
    assert len({c.call_id for c in await other.list_calls()}) == len(
        await other.list_calls()
    )
    await other.expire_inbound_history(10000)
    with pytest.raises(ToolGatewayError) as error:
        await fetch(runtime, third.next_cursor)
    assert error.value.category is ToolErrorCategory.PROVIDER_PERMANENT


@pytest.mark.asyncio
async def test_missing_deduplicated_raw_cannot_publish_page(runtime):
    raw = mime()
    await runtime["provider"].receive_inbound(raw, internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    item = page.items[0]
    # 故障只破坏本owner对象store；真实metadata和所有业务规则不替换。
    await runtime["transport"].delete(
        f"raw/{runtime['route'].tenant_id}/{item.raw.artifact_id}"
    )
    with pytest.raises(ToolGatewayError):
        await fetch(runtime, anchor.next_cursor)
    assert runtime["slot"].is_empty


@pytest.mark.asyncio
async def test_child_cannot_take_or_discard_parent_slot(runtime):
    page = ArchivedInboundPage(
        route=runtime["route"],
        starting_cursor=runtime["start"],
        next_cursor=runtime["start"],
        items=(),
    )
    slot = runtime["slot"]
    handle = slot.put(page)

    async def child():
        with pytest.raises(InboundError):
            slot.take(handle)
        slot.discard_all()

    await asyncio.create_task(child())
    assert slot.take(handle) == page
    with pytest.raises(InboundError):
        slot.take(handle)
    slot.put(page)
    with pytest.raises(InboundError):
        slot.take("ipg_invalid")
    assert slot.is_empty


@pytest.mark.asyncio
@pytest.mark.parametrize("target_status", ["executing", "succeeded"])
async def test_real_ledger_storage_failure_blocks_delivery(
    runtime, target_status, integration_engine,
):
    from sqlalchemy import text

    tenant = runtime["route"].tenant_id
    await runtime["provider"].receive_inbound(mime(), internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    before = len(await runtime["provider"].list_calls())
    # 管理连接只安装故障触发器；Gateway、Store、Repository 始终使用真实受限角色。
    async with integration_engine.begin() as session:
        await session.execute(
            text(
                "CREATE OR REPLACE FUNCTION inbound_test_ledger_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.tenant_id = TG_ARGV[0] AND NEW.status = TG_ARGV[1] THEN RAISE EXCEPTION 'controlled_ledger_failure'; END IF; RETURN NEW; END $$"
            )
        )
        await session.execute(
            text(
                f"CREATE TRIGGER inbound_test_failure BEFORE UPDATE ON tool_calls FOR EACH ROW EXECUTE FUNCTION inbound_test_ledger_failure('{tenant}', '{target_status}')"
            )
        )
    try:
        with pytest.raises(ToolGatewayError):
            await fetch(runtime, anchor.next_cursor)
        assert runtime["slot"].is_empty
        calls = await runtime["provider"].list_calls()
        if target_status == "executing":
            assert len(calls) == before
            assert len(runtime["resolves"]) == 1
        else:
            assert len(calls) > before
            async with runtime["factory"]() as session:
                originals = (
                    await session.scalars(
                        select(RawArtifactRow).where(RawArtifactRow.tenant_id == tenant)
                    )
                ).all()
                assert len(originals) == 1
                _meta, raw = await runtime["store"].get_bounded(
                    tenant, originals[0].artifact_id, maximum_bytes=MIME_BYTES
                )
                assert raw == mime()
    finally:
        async with integration_engine.begin() as session:
            await session.execute(
                text("DROP TRIGGER inbound_test_failure ON tool_calls")
            )
            await session.execute(text("DROP FUNCTION inbound_test_ledger_failure()"))


@pytest.mark.asyncio
async def test_cancellation_during_archive_clears_slot_and_never_returns_page(
    runtime, monkeypatch
):
    from sqlalchemy import text

    tenant = runtime["route"].tenant_id
    await runtime["provider"].receive_inbound(mime(), internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    # 用真实PG表锁阻塞归档；取消当前task而不是制造假的完成结果。
    async with runtime["factory"]() as blocker:
        await blocker.execute(text("LOCK TABLE raw_artifacts IN ACCESS EXCLUSIVE MODE"))
        started = asyncio.Event()
        original = runtime["handler"]._archiver.archive

        async def observe(*args, **kwargs):
            started.set()
            return await original(*args, **kwargs)

        monkeypatch.setattr(runtime["handler"]._archiver, "archive", observe)

        async def invoke_and_check():
            try:
                await fetch(runtime, anchor.next_cursor)
            finally:
                assert runtime["slot"].is_empty

        task = asyncio.create_task(invoke_and_check())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        finally:
            await blocker.rollback()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with runtime["factory"]() as session:
        assert (
            await session.scalars(
                select(RawArtifactRow).where(RawArtifactRow.tenant_id == tenant)
            )
        ).all() == []


@pytest.mark.asyncio
async def test_raw_commit_unknown_preserves_bytes_and_dedup_retry(runtime, monkeypatch):
    tenant = runtime["route"].tenant_id
    await runtime["provider"].receive_inbound(mime(), internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    original_factory = runtime["store"]._uow_factory
    fired = []

    class CommitUnknown(SqlAlchemyArtifactUnitOfWork):
        async def __aexit__(self, exc_type, exc, tb):
            await super().__aexit__(exc_type, exc, tb)
            if exc_type is None and not fired:
                fired.append(True)
                raise RuntimeError("controlled_commit_unknown")

    monkeypatch.setattr(
        runtime["store"],
        "_uow_factory",
        lambda requested: CommitUnknown(runtime["factory"], requested),
    )
    with pytest.raises(ToolGatewayError):
        await fetch(runtime, anchor.next_cursor)
    monkeypatch.setattr(runtime["store"], "_uow_factory", original_factory)
    async with runtime["factory"]() as session:
        rows = (
            await session.scalars(
                select(RawArtifactRow).where(RawArtifactRow.tenant_id == tenant)
            )
        ).all()
        assert len(rows) == 1
    # commit 未知仍须保留已持久化原件；重试复用同一份 metadata 与 bytes。
    meta, raw = await runtime["store"].get_bounded(
        tenant, rows[0].artifact_id, maximum_bytes=MIME_BYTES
    )
    assert raw == mime() and meta.content_hash == hashlib.sha256(raw).hexdigest()
    page = await fetch(runtime, anchor.next_cursor)
    assert len(page.items) == 1
    assert page.items[0].raw.artifact_id == rows[0].artifact_id
    async with runtime["factory"]() as session:
        stored_ids = list((await session.scalars(
            select(RawArtifactRow.artifact_id).where(RawArtifactRow.tenant_id == tenant)
        )).all())
        assert stored_ids == [rows[0].artifact_id]
    calls = await runtime["provider"].list_calls()
    assert await fetch(runtime, anchor.next_cursor) == page
    assert await runtime["provider"].list_calls() == calls
    assert runtime["slot"].is_empty


@pytest.mark.asyncio
async def test_page_bytes_do_not_cross_unconsumed_ref(runtime):
    raw = mime(body="x" * (3 * 1024 * 1024))
    for i in range(3):
        await runtime["provider"].receive_inbound(
            raw.replace(b"<buyer@", f"<buyer{i}@".encode()), internal_date=DATE
        )
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    assert len(page.items) == 2
    assert len(decode_cursor(page.next_cursor, runtime["route"]).pending) == 1
    final = await fetch(runtime, page.next_cursor)
    assert len(final.items) == 1
    assert len({i.provider_ref_digest for i in (*page.items, *final.items)}) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [b"wrong-bytes", b"x" * 1000, b""])
async def test_actual_raw_hash_and_size_corruption_blocks_page(runtime, corrupt):
    await runtime["provider"].receive_inbound(mime(), internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    item = page.items[0]
    # 本owner对象存储故障；绕Store只为模拟外部bytes损坏。
    await runtime["transport"].put(
        f"raw/{runtime['route'].tenant_id}/{item.raw.artifact_id}", corrupt or b"x"
    )
    with pytest.raises(ToolGatewayError):
        await fetch(runtime, anchor.next_cursor)
    assert runtime["slot"].is_empty


@pytest.mark.asyncio
async def test_oversized_provider_keeps_no_raw_and_does_not_retry_forever(runtime):
    await runtime["provider"].receive_inbound(
        b"x" * (MIME_BYTES + 1), internal_date=DATE
    )
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    assert len(page.items) == 1
    assert (
        page.items[0].disposition is InboundDisposition.TOO_LARGE
        and page.items[0].raw is None
    )
    assert decode_cursor(page.next_cursor, runtime["route"]).phase == "history"
    async with runtime["factory"]() as session:
        assert (
            await session.scalars(
                select(RawArtifactRow).where(
                    RawArtifactRow.tenant_id == runtime["route"].tenant_id
                )
            )
        ).all() == []


@pytest.mark.asyncio
async def test_more_than_one_provider_page_no_watermark_jump(runtime):
    provider = runtime["provider"]
    for i in range(101):
        await provider.receive_inbound(
            mime(message_id=f"<bulk{i}@example.invalid>"), internal_date=DATE
        )
    anchor = await fetch(runtime, runtime["start"])
    current = anchor.next_cursor
    all_items = []
    for _ in range(5):
        page = await fetch(runtime, current)
        all_items.extend(page.items)
        current = page.next_cursor
        assert decode_cursor(current, runtime["route"]).phase == "bootstrap"
    final = await fetch(runtime, current)
    all_items.extend(final.items)
    assert len(all_items) == len({i.provider_ref_digest for i in all_items}) == 101
    assert decode_cursor(final.next_cursor, runtime["route"]).phase == "history"
    empty = await fetch(runtime, final.next_cursor)
    assert empty.items == ()
    assert (
        len([c for c in await provider.list_calls() if c.operation == "inbound_list"])
        == 2
    )


@pytest.mark.asyncio
async def test_tenant_isolation_of_actual_raw_store(runtime):
    await runtime["provider"].receive_inbound(mime(), internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    from artifact_store.errors import ArtifactNotFoundError

    with pytest.raises(ArtifactNotFoundError):
        await runtime["store"].get_bounded(
            TenantId(new_id("tn")),
            page.items[0].raw.artifact_id,
            maximum_bytes=MIME_BYTES,
        )
    other = ControlledGmailTransport(
        runtime["provider"]._path, tenant_id=TenantId(new_id("tn"))
    )
    assert await other.list_calls() == ()
    assert (
        await other.get_inbound_message(
            token="controlled", message_ref="unknown", maximum_bytes=MIME_BYTES
        )
    ).status == "message_gone"


@pytest.mark.asyncio
async def test_html_tags_cannot_split_credential_marker(runtime):
    await runtime["provider"].receive_inbound(
        mime(headers="Content-Type: text/html", body="<p>co<b></b>okie</p>"),
        internal_date=DATE,
    )
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    assert page.items[0].disposition is InboundDisposition.CREDENTIAL_MARKER
    assert page.items[0].raw is not None


@pytest.mark.asyncio
async def test_empty_duplicate_refs_and_repeated_provider_token(runtime, monkeypatch):
    provider = runtime["provider"]
    ref = await provider.receive_inbound(mime(), internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    original = provider.list_feedback_messages

    async def empty_page(*, token, after_epoch, page_token):
        await original(token=token, after_epoch=after_epoch, page_token=None)
        return (), "next-page"

    # 只控制外部Provider分页响应；使用同持久邮箱和真实调用记录。
    monkeypatch.setattr(provider, "list_feedback_messages", empty_page)
    empty = await fetch(runtime, anchor.next_cursor)
    assert empty.items == ()
    assert decode_cursor(empty.next_cursor, runtime["route"]).phase == "bootstrap"
    with pytest.raises(ToolGatewayError):
        await fetch(runtime, empty.next_cursor)

    async def duplicate_refs(*, token, after_epoch, page_token):
        await original(token=token, after_epoch=after_epoch, page_token=None)
        return (ref, ref), None

    monkeypatch.setattr(provider, "list_feedback_messages", duplicate_refs)
    page = await fetch(runtime, empty.next_cursor)
    assert len(page.items) == 1
    assert decode_cursor(page.next_cursor, runtime["route"]).phase == "history"


@pytest.mark.asyncio
async def test_history_pending_keeps_original_start_until_all_items(runtime):
    provider = runtime["provider"]
    anchor = await fetch(runtime, runtime["start"])
    empty = await fetch(runtime, anchor.next_cursor)
    original_start = decode_cursor(empty.next_cursor, runtime["route"]).history_start
    for i in range(3):
        await provider.receive_inbound(
            mime(message_id=f"<history{i}@example.invalid>"), internal_date=DATE
        )
    first = await fetch(runtime, empty.next_cursor, 1)
    state = decode_cursor(first.next_cursor, runtime["route"])
    assert state.history_start == original_start and len(state.pending) == 2
    second = await fetch(runtime, first.next_cursor)
    assert len(second.items) == 2
    assert (
        decode_cursor(second.next_cursor, runtime["route"]).history_start
        != original_start
    )


@pytest.mark.asyncio
async def test_gateway_wrapper_preserves_bounded_provider_retry_after(
    runtime, monkeypatch
):
    from connectors.gmail.transport import GmailHttpStatusError

    original = runtime["provider"].get_profile_history_id

    async def rate_limited(*, token):
        await original(token=token)
        raise GmailHttpStatusError(429, feedback_retry_after_seconds=3600)

    monkeypatch.setattr(runtime["provider"], "get_profile_history_id", rate_limited)
    with pytest.raises(ToolGatewayError) as error:
        await fetch(runtime, runtime["start"])
    assert error.value.category is ToolErrorCategory.RATE_LIMITED
    assert error.value.retry_after_seconds == 3600
    assert runtime["slot"].is_empty


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["zlib_codec", "bz2_codec", "date_extra", "date_conflicting"]
)
async def test_codec_and_date_rejections_archive_then_isolate(runtime, case):
    import bz2
    import zlib

    if case in {"zlib_codec", "bz2_codec"}:
        compress = zlib.compress if case == "zlib_codec" else bz2.compress
        raw = mime(
            headers=f"Content-Type: text/plain; charset={case}", body=""
        ) + compress(b"x" * 1024)
        expected = InboundDisposition.MALFORMED
    else:
        suffix = "+0000 garbage GMT" if case == "date_extra" else "+0000 -1200"
        raw = mime(date=f"Sat, 05 Sep 2026 09:00:00 {suffix}")
        expected = InboundDisposition.INVALID_SENT_AT
    await runtime["provider"].receive_inbound(raw, internal_date=DATE)
    anchor = await fetch(runtime, runtime["start"])
    page = await fetch(runtime, anchor.next_cursor)
    assert len(page.items) == 1
    item = page.items[0]
    assert item.disposition is expected and item.sent_at is None
    assert item.raw is not None
    _, actual = await runtime["store"].get_bounded(
        runtime["route"].tenant_id, item.raw.artifact_id, maximum_bytes=MIME_BYTES
    )
    assert actual == raw
    assert decode_cursor(page.next_cursor, runtime["route"]).phase == "history"
    assert runtime["slot"].is_empty
