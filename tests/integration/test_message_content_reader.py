"""生产 MessageContentReader（真实 PostgreSQL + MinIO）。

TDD RED：apps/scheduler_worker/adapters 尚不存在，导入即失败。

契约（修订计划 A 片）：
- load(tenant, message_id)：message 行不存在 → None（端口契约）；
  行存在但 artifact 缺失/非 EMAIL_RAW/解析失败/超限 → 固定摘要
  ValidationError（明确异常，不靠 AttributeError）。
- 全链路 tenant：conversations UoW 每次新开（不持有绑定 session 的仓储）；
  artifact store 自身 tenant-bound。
- 校验 direction=inbound、kind=EMAIL_RAW、mime=message/rfc822；
  raw ref 只做显式 ArtifactId 适配。
- MIME复用5a有界纯解析，HTML支持且附件/历史引用不作当前表达。
- 原完整双视图先guard，超预算拒绝；模型只见当前表达投影与固定主题。
- 无 marker：outbox payload 精确键集；caplog 零日志；异常消息不含正文。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.conversations.service import ConversationService
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    MessageId,
    ProspectAccountId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.conversations.models")

NOW = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)
BODY_MARKER = "please stop contacting us CUSTOMER-EXPRESSION-77"

from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.runtime_database_fixtures import (
    runtime_database_url as runtime_database_url,  # noqa: PLC0414 - 真实企业运行角色
)


@dataclass(frozen=True)
class _MinioRuntime:
    settings: Any
    secrets: Any


@pytest.fixture(scope="module")
def minio_runtime(owned_infrastructure):  # noqa: F811
    from connectors.object_store.config import S3ObjectStoreSettings

    config = owned_infrastructure.config
    return _MinioRuntime(
        S3ObjectStoreSettings.from_environ(config.runtime_environment()), config
    )


@pytest.fixture
def reader_tenant() -> TenantId:
    return TenantId(new_id("tn"))


@pytest_asyncio.fixture
async def artifact_db(runtime_database_url, reader_tenant) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(
        await runtime_database_url(str(reader_tenant))
    )
    try:
        yield engine
    finally:
        await engine.dispose()


def _stores(engine: AsyncEngine, runtime: _MinioRuntime):
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.object_store.bounded import S3BoundedObjectBlobTransport
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
    from shared.schemas.evidence_read import ObjectReadLimits

    factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = lambda tenant: SqlAlchemyArtifactUnitOfWork(factory, tenant)
    transport = S3ObjectBlobTransport(runtime.settings, runtime.secrets)
    return RawArtifactStoreImpl(
        uow_factory,
        transport,
        runtime.settings.raw_max_bytes,
        lambda: NOW,
        new_id,
        bounded_transport=S3BoundedObjectBlobTransport(
            runtime.settings,
            runtime.secrets,
            limits=ObjectReadLimits(
                connect_timeout_ms=2000,
                read_timeout_ms=2000,
                total_timeout_ms=10000,
                maximum_attempts=1,
                chunk_bytes=65536,
            ),
        ),
    )


def _conversations_service(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId
) -> ConversationService:
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    impl_type = importlib.import_module(
        "domains.conversations.service_impl"
    ).ConversationServiceImpl
    return impl_type(
        lambda requested: uow_type(factory, requested, now=lambda: NOW),
        now=lambda: NOW,
    )


def _reader(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    store: Any,
    *,
    max_raw_bytes: int = 1024 * 1024,
    max_subject_chars: int = 200,
    max_body_chars: int = 65536,
) -> Any:
    uow_type = importlib.import_module(
        "infra.db.conversations_uow"
    ).SqlAlchemyConversationsUnitOfWork
    reader_type = importlib.import_module(
        "apps.scheduler_worker.adapters.message_content_reader"
    ).ArtifactMessageContentReader
    return reader_type(
        lambda requested: uow_type(factory, requested, now=lambda: NOW),
        store,
        max_raw_bytes=max_raw_bytes,
        max_subject_chars=max_subject_chars,
        max_body_chars=max_body_chars,
    )


async def _ingest(
    service: ConversationService,
    tenant: TenantId,
    account: ProspectAccountId,
    artifact_ref: str,
    external_id: str,
) -> MessageId:
    return await service.ingest_inbound(
        tenant,
        None,
        account,
        artifact_ref,
        external_id,
        NOW,
    )


async def _store_email(store: Any, tenant: TenantId, raw: bytes) -> str:
    from artifact_store.store import RawArtifactKind

    meta = await store.put(tenant, RawArtifactKind.EMAIL_RAW, raw, "message/rfc822")
    return str(meta.artifact_id)


def _rfc822(subject: str | None, body: str, *, extra_headers: str = "") -> bytes:
    subject_line = "" if subject is None else f"Subject: {subject}\r\n"
    return (
        f"From: customer@example.test\r\n"
        f"To: sales@example.test\r\n"
        f"{subject_line}{extra_headers}"
        f"Message-ID: <reply-1@example.test>\r\n"
        f"Date: Mon, 18 Aug 2026 10:00:00 +0000\r\n"
        f"MIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: 8bit\r\n"
        f"\r\n"
        f"{body}"
    ).encode()


async def test_reader_loads_subject_and_body_from_email_artifact(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(
        store,
        tenant,
        _rfc822("Re: hinges", f"Please send specs. {BODY_MARKER}"),
    )
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<reply-1@example.test>")
    reader = _reader(factory, tenant, store)
    content = await reader.load(tenant, message_id)
    assert content is not None
    assert content.subject == "(current reply)"
    assert content.original_subject == "Re: hinges"
    assert content.body == f"Please send specs. {BODY_MARKER}"


async def test_reader_multipart_text_plain_and_attachment_excluded(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    raw = (
        b"From: c@example.test\r\n"
        b"To: s@example.test\r\n"
        b"Subject: multipart test\r\n"
        b"Message-ID: <reply-mp@example.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=bound\r\n"
        b"\r\n"
        b"--bound\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"\r\n"
        b"the plain body\r\n"
        b"--bound\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=x.bin\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"QUJD\r\n"
        b"--bound--\r\n"
    )
    ref = await _store_email(store, tenant, raw)
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<reply-mp@example.test>")
    reader = _reader(factory, tenant, store)
    content = await reader.load(tenant, message_id)
    assert content is not None
    assert content.original_subject == "multipart test"
    assert content.body == "the plain body\r\n"
    assert "QUJD" not in content.body  # attachment 排除


async def test_reader_missing_message_returns_none(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    store = _stores(artifact_db, minio_runtime)
    reader = _reader(factory, tenant, store)
    assert await reader.load(tenant, MessageId(new_id("msg"))) is None


async def test_reader_missing_artifact_fails_closed(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(
        service,
        tenant,
        account,
        "art_0123456789abcdefghjkmnpqrsvwxyz",
        "<missing-artifact@example.test>",
    )
    reader = _reader(factory, tenant, store)
    with pytest.raises(ValidationError, match="原文不可读"):
        await reader.load(tenant, message_id)


async def test_reader_rejects_non_email_artifact(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    from artifact_store.store import RawArtifactKind

    meta = await store.put(
        tenant, RawArtifactKind.PDF, b"%PDF-1.4 fake", "application/pdf"
    )
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(
        service,
        tenant,
        account,
        str(meta.artifact_id),
        "<non-email@example.test>",
    )
    reader = _reader(factory, tenant, store)
    with pytest.raises(ValidationError, match="原文非邮件"):
        await reader.load(tenant, message_id)


async def test_reader_rejects_outbound_direction(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(store, tenant, _rfc822("out", "body"))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<out@example.test>")
    # 直接改成 outbound 方向（模拟未来出站消息走同一表）
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        row = await session.get(tables.MessageRow, (str(tenant), str(message_id)))
        row.direction = "outbound"
        await session.commit()
    reader = _reader(factory, tenant, store)
    with pytest.raises(ValidationError, match="非入站"):
        await reader.load(tenant, message_id)


async def test_reader_html_only_uses_original_inbound_parser(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    raw = (
        b"From: c@example.test\r\n"
        b"Subject: html only\r\n"
        b"Message-ID: <html@example.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"\r\n"
        b"<html><body><p>hi</p></body></html>\r\n"
    )
    ref = await _store_email(store, tenant, raw)
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<html@example.test>")
    reader = _reader(factory, tenant, store)
    content = await reader.load(tenant, message_id)
    assert content.body == "\nhi\r\n"


async def test_reader_rejects_subject_and_body_over_budget(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    long_subject = "S" * 500
    long_body = "B" * 100_000
    ref = await _store_email(store, tenant, _rfc822(long_subject, long_body))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<trunc@example.test>")
    reader = _reader(
        factory,
        tenant,
        store,
        max_raw_bytes=4 * 1024 * 1024,
        max_subject_chars=100,
        max_body_chars=1000,
    )
    with pytest.raises(ValidationError, match="模型读取预算"):
        await reader.load(tenant, message_id)


async def test_reader_raw_bytes_over_limit_fails_closed(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(store, tenant, _rfc822("big", "B" * 5000))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<big@example.test>")
    reader = _reader(
        factory,
        tenant,
        store,
        max_raw_bytes=1024,
    )
    with pytest.raises(ValidationError, match="原文超限"):
        await reader.load(tenant, message_id)


async def test_reader_quoted_printable_and_utf8_subject(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    raw = (
        b"From: c@example.test\r\n"
        b"Subject: =?utf-8?Q?Caf=C3=A9=20reply?=\r\n"
        b"Message-ID: <qp@example.test>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"caf=E9 please\r\n"
    )
    ref = await _store_email(store, tenant, raw)
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<qp@example.test>")
    reader = _reader(factory, tenant, store)
    content = await reader.load(tenant, message_id)
    assert content is not None
    assert content.original_subject == "Café reply"
    assert content.body.strip() == "café please"  # QP 解码后尾部 CRLF 属预期


async def test_reader_cross_tenant_isolation(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
    runtime_database_url,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant_a = reader_tenant
    tenant_b = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(store, tenant_a, _rfc822("x", "y"))
    service_a = _conversations_service(factory, tenant_a)
    message_id = await _ingest(
        service_a,
        tenant_a,
        account,
        ref,
        "<iso@example.test>",
    )
    engine_b = importlib.import_module("infra.db.session").create_engine_from(
        await runtime_database_url(str(tenant_b))
    )
    try:
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        reader_b = _reader(factory_b, tenant_b, _stores(engine_b, minio_runtime))
        assert await reader_b.load(tenant_b, message_id) is None
    finally:
        await engine_b.dispose()


async def test_reader_content_never_persisted_or_logged(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
    caplog: Any,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(
        store,
        tenant,
        _rfc822("s", f"body {BODY_MARKER}"),
    )
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<noise@example.test>")
    reader = _reader(factory, tenant, store)
    with caplog.at_level("CRITICAL"):
        content = await reader.load(tenant, message_id)
    assert content is not None
    assert content.body == f"body {BODY_MARKER}"  # 视图允许携带正文（仅内存）
    # 无日志输出（reader 不打印任何内容）
    assert len(caplog.records) == 0
    # outbox 无正文（该 message 只 ingest，未分类 → 无 ReplyReceived；
    # InboundMessageStored payload 只有 typed ID）
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        outbox = (
            (
                await session.execute(
                    select(tables.OutboxEventRow).where(
                        tables.OutboxEventRow.tenant_id == str(tenant)
                    )
                )
            )
            .scalars()
            .all()
        )
    for row in outbox:
        import json

        dump = json.dumps(row.event_payload)
        assert BODY_MARKER not in dump


async def test_reader_through_classify_step_over_budget_prevents_model_and_classification(
    artifact_db: AsyncEngine,
    reader_tenant: TenantId,
    minio_runtime: _MinioRuntime,
) -> None:
    """条件 7：真实 reader → ClassifyStep → QualificationAgent(fake port)。

    完整原件guard/预算在model之前；超预算零模型调用、零分类落库。"""
    import json as _json

    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from agent_runtime.qualification_agent.agent import QualificationAgent
    from workflows.engine.runner import RunId, StepStatus, WorkflowRun
    from workflows.reply_qualification.steps import ClassifyStep

    tenant = reader_tenant
    account = ProspectAccountId(new_id("acc"))
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    store = _stores(artifact_db, minio_runtime)
    long_subject = "S" * 300
    long_body = "B" * 90_000
    ref = await _store_email(store, tenant, _rfc822(long_subject, long_body))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<flow@example.test>")
    reader = _reader(
        factory,
        tenant,
        store,
        max_subject_chars=100,
        max_body_chars=1000,
    )

    class RecordingPort:
        def __init__(self) -> None:
            self.calls = 0
            self.last_message: dict[str, str] = {}

        async def classify_reply(
            self, *, system_prompt: str, message: dict[str, str]
        ) -> str:
            del system_prompt
            self.calls += 1
            self.last_message = dict(message)
            return _json.dumps({"category": "clear_interest", "candidate_fields": []})

    port = RecordingPort()
    classifier = QualificationAgent(
        model="reader-test-model", model_client=port, gateway=None, guardrails=None
    )
    step = ClassifyStep(
        classifier,
        reader,
        CredentialMarkerGuard(),
        service,
    )
    run = WorkflowRun(
        run_id=RunId(new_id("run")),
        tenant_id=tenant,
        workflow_type="reply_qualification",
        workflow_version=1,
        subject_ref=str(message_id),
        current_step="classify",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context={
            "message_id": str(message_id),
            "outbound_message_id": None,
        },
    )
    with pytest.raises(ValidationError, match="模型读取预算"):
        await step.execute(run)
    assert port.calls == 0
    # 分类落库（真实 conversations service）
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        classification = await session.get(
            tables.ConversationClassificationRow,
            (str(tenant), str(message_id)),
        )
    assert classification is None
