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
- MIME：首个非 attachment text/plain，charset/transfer encoding 确定性；
  HTML-only fail-closed；Subject 缺失 → None。
- 截断：注入的 max_raw_bytes/max_subject_chars/max_body_chars，
  解析前限 raw bytes，解析后分别确定性截断 subject/body；
  guard/model 只见截断后内容。
- 无 marker：outbox payload 精确键集；caplog 零日志；异常消息不含正文。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer

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
BODY_MARKER = "please stop contacting us SECRET-MARKER-77"

_MINIO_IMAGE = "minio/minio:RELEASE.2025-04-22T22-12-26Z"


@dataclass(frozen=True)
class _MinioRuntime:
    settings: Any
    secrets: Any


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, secret_ref: str) -> str:
        return self._values[secret_ref]


@dataclass(frozen=True)
class _MinioRuntime:
    settings: Any
    secrets: Any


@pytest.fixture(scope="module")
def minio_runtime() -> Iterator[_MinioRuntime]:
    import secrets as stdlib_secrets
    import time

    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError

    from connectors.object_store.s3 import S3ObjectStoreSettings

    access = f"access{stdlib_secrets.token_hex(12)}"
    secret = f"secret{stdlib_secrets.token_urlsafe(24)}"
    bucket = f"artifacts-{stdlib_secrets.token_hex(8)}"
    container = (
        DockerContainer(_MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", access)
        .with_env("MINIO_ROOT_PASSWORD", secret)
        .with_command("server /data --address :9000")
        .with_exposed_ports(9000)
    )
    container.start()
    endpoint = f"http://127.0.0.1:{container.get_exposed_port(9000)}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            client.create_bucket(Bucket=bucket)
            break
        except BotoCoreError:
            if time.monotonic() >= deadline:
                container.stop()
                pytest.fail("MinIO 未在限定时间内就绪")
            time.sleep(0.2)
    settings = S3ObjectStoreSettings(
        True,
        endpoint,
        bucket,
        "TEST_MINIO_ACCESS",
        "TEST_MINIO_SECRET",
        "us-east-1",
        1024 * 1024,
        1024 * 1024,
    )
    try:
        yield _MinioRuntime(
            settings,
            _Secrets({"TEST_MINIO_ACCESS": access, "TEST_MINIO_SECRET": secret}),
        )
    finally:
        container.stop()


@pytest.fixture
def tenant() -> TenantId:
    return TenantId(new_id("tn"))


@pytest.fixture
def account() -> ProspectAccountId:
    return ProspectAccountId(new_id("acc"))


@pytest_asyncio.fixture
async def artifact_db(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _stores(engine: AsyncEngine, runtime: _MinioRuntime):
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = lambda tenant: SqlAlchemyArtifactUnitOfWork(factory, tenant)
    transport = S3ObjectBlobTransport(runtime.settings, runtime.secrets)
    return RawArtifactStoreImpl(
        uow_factory,
        transport,
        runtime.settings.raw_max_bytes,
        lambda: NOW,
        new_id,
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
        tenant, None, account, artifact_ref, external_id, NOW,
    )


async def _store_email(
    store: Any, tenant: TenantId, raw: bytes
) -> str:
    from artifact_store.store import RawArtifactKind

    meta = await store.put(
        tenant, RawArtifactKind.EMAIL_RAW, raw, "message/rfc822"
    )
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
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(
        store, tenant,
        _rfc822("Re: hinges", f"Please send specs. {BODY_MARKER}"),
    )
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<reply-1@example.test>")
    reader = _reader(factory, tenant, store)
    content = await reader.load(tenant, message_id)
    assert content is not None
    assert content.subject == "Re: hinges"
    assert content.body == f"Please send specs. {BODY_MARKER}"


async def test_reader_multipart_text_plain_and_attachment_excluded(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
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
    assert content.subject == "multipart test"
    assert content.body == "the plain body"
    assert "QUJD" not in content.body  # attachment 排除


async def test_reader_missing_message_returns_none(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    store = _stores(artifact_db, minio_runtime)
    reader = _reader(factory, tenant, store)
    assert await reader.load(tenant, MessageId(new_id("msg"))) is None


async def test_reader_missing_artifact_fails_closed(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(
        service, tenant, account, "art_0123456789abcdefghjkmnpqrsvwxyz",
        "<missing-artifact@example.test>",
    )
    reader = _reader(factory, tenant, store)
    with pytest.raises(ValidationError, match="原文不可读"):
        await reader.load(tenant, message_id)


async def test_reader_rejects_non_email_artifact(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    from artifact_store.store import RawArtifactKind

    meta = await store.put(
        tenant, RawArtifactKind.PDF, b"%PDF-1.4 fake", "application/pdf"
    )
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(
        service, tenant, account, str(meta.artifact_id),
        "<non-email@example.test>",
    )
    reader = _reader(factory, tenant, store)
    with pytest.raises(ValidationError, match="原文非邮件"):
        await reader.load(tenant, message_id)


async def test_reader_rejects_outbound_direction(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
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


async def test_reader_html_only_fails_closed(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
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
    with pytest.raises(ValidationError, match="正文不可解析"):
        await reader.load(tenant, message_id)


async def test_reader_truncates_subject_and_body_by_injected_limits(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    long_subject = "S" * 500
    long_body = "B" * 100_000
    ref = await _store_email(store, tenant, _rfc822(long_subject, long_body))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<trunc@example.test>")
    reader = _reader(
        factory, tenant, store,
        max_raw_bytes=4 * 1024 * 1024,
        max_subject_chars=100,
        max_body_chars=1000,
    )
    content = await reader.load(tenant, message_id)
    assert content is not None
    assert len(content.subject) == 100
    assert len(content.body) == 1000


async def test_reader_raw_bytes_over_limit_fails_closed(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(store, tenant, _rfc822("big", "B" * 5000))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<big@example.test>")
    reader = _reader(
        factory, tenant, store, max_raw_bytes=1024,
    )
    with pytest.raises(ValidationError, match="原文超限"):
        await reader.load(tenant, message_id)


async def test_reader_quoted_printable_and_utf8_subject(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
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
    assert content.subject == "Café reply"
    assert content.body.strip() == "café please"  # QP 解码后尾部 CRLF 属预期


async def test_reader_cross_tenant_isolation(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant_a = TenantId(new_id("tn"))
    tenant_b = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(store, tenant_a, _rfc822("x", "y"))
    service_a = _conversations_service(factory, tenant_a)
    message_id = await _ingest(
        service_a, tenant_a, account, ref, "<iso@example.test>",
    )
    reader_b = _reader(factory, tenant_b, store)
    assert await reader_b.load(tenant_b, message_id) is None


async def test_reader_content_never_persisted_or_logged(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime, caplog: Any,
) -> None:
    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account = ProspectAccountId(new_id("acc"))
    store = _stores(artifact_db, minio_runtime)
    ref = await _store_email(
        store, tenant, _rfc822("s", f"body {BODY_MARKER}"),
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
            await session.execute(
                select(tables.OutboxEventRow).where(
                    tables.OutboxEventRow.tenant_id == str(tenant)
                )
            )
        ).scalars().all()
    for row in outbox:
        import json

        dump = json.dumps(row.event_payload)
        assert BODY_MARKER not in dump


async def test_reader_through_classify_step_guard_and_model_see_truncated_content(
    artifact_db: AsyncEngine, minio_runtime: _MinioRuntime, tenant: TenantId,
    account: ProspectAccountId,
) -> None:
    """条件 7：真实 reader → ClassifyStep → QualificationAgent(fake port)。

    guard 在 model 前；model port 只收到截断后的 subject/body；
    run 推进到 apply_actions 且分类落库。"""
    import json as _json

    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from agent_runtime.qualification_agent.agent import QualificationAgent
    from workflows.engine.runner import RunId, StepStatus, WorkflowRun
    from workflows.reply_qualification.steps import ClassifyStep

    factory = async_sessionmaker(artifact_db, expire_on_commit=False)
    store = _stores(artifact_db, minio_runtime)
    long_subject = "S" * 300
    long_body = "B" * 90_000
    ref = await _store_email(store, tenant, _rfc822(long_subject, long_body))
    service = _conversations_service(factory, tenant)
    message_id = await _ingest(service, tenant, account, ref, "<flow@example.test>")
    reader = _reader(
        factory, tenant, store,
        max_subject_chars=100, max_body_chars=1000,
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
        classifier, reader, CredentialMarkerGuard(), service,
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
    action, next_step, patch = await step.execute(run)
    assert action == "advance"
    assert next_step == "apply_actions"
    assert port.calls == 1
    # guard 在 model 前：model port 只见截断内容
    assert len(port.last_message["subject"]) == 100
    assert len(port.last_message["body"]) == 1000
    assert patch["category"] == "clear_interest"
    # 分类落库（真实 conversations service）
    tables = importlib.import_module("infra.db.tables")
    async with factory() as session:
        classification = await session.get(
            tables.ConversationClassificationRow,
            (str(tenant), str(message_id)),
        )
    assert classification is not None
    assert classification.category == "clear_interest"
