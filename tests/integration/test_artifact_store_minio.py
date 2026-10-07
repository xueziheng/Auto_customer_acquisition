from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
import pytest
import pytest_asyncio
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer

from artifact_store.errors import (
    ArtifactCommitUnknownError,
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from artifact_store.store import GeneratedArtifactKind, RawArtifactKind
from connectors.object_store.config import S3ObjectStoreSettings
from infra.db.tables import GeneratedArtifactRow, RawArtifactRow
from infra.pilot.resources import PILOT_MINIO_IMAGE
from shared.schemas.identifiers import IdempotencyKey, RunId, TenantId, new_id

_GENERATED_MIME = "application/vnd.tradeos.email-draft+json"


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def resolve(self, secret_ref: str) -> str:
        return self._values[secret_ref]


@dataclass(frozen=True)
class _MinioRuntime:
    settings: S3ObjectStoreSettings
    secrets: _Secrets
    client: object


@pytest.fixture(scope="session")
def minio_runtime() -> Iterator[_MinioRuntime]:
    access = f"access{secrets.token_hex(12)}"
    secret = f"secret{secrets.token_urlsafe(24)}"
    bucket = f"artifacts-{secrets.token_hex(8)}"
    container = (
        DockerContainer(PILOT_MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", access)
        .with_env("MINIO_ROOT_PASSWORD", secret)
        .with_command(
            "server /bitnami/minio/data --address :9000 --console-address 127.0.0.1:9001"
        )
        .with_exposed_ports(9000)
    )
    client = None
    primary: BaseException | None = None
    try:
        container.start()
        endpoint = f"http://127.0.0.1:{container.get_exposed_port(9000)}"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"},
                proxies={}, connect_timeout=2, read_timeout=2,
                retries={"max_attempts": 0},
            ),
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                client.create_bucket(Bucket=bucket)
                break
            except (BotoCoreError, ClientError):
                if time.monotonic() >= deadline:
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
        yield _MinioRuntime(
            settings,
            _Secrets({"TEST_MINIO_ACCESS": access, "TEST_MINIO_SECRET": secret}),
            client,
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_errors = []
        closers = [] if client is None else [("MinIO 客户端关闭失败", client.close)]
        # Testcontainers.stop 仅移除本实例已取得的容器 ID；未创建时只关闭客户端。
        closers.append(("MinIO 测试容器清理失败", container.stop))
        for reason, close in closers:
            try:
                close()
            except Exception:  # noqa: BLE001 - 保留主失败，清理错误只记录固定类别
                cleanup_errors.append(reason)
        if cleanup_errors:
            summary = "；".join(cleanup_errors)
            if primary is not None:
                primary.add_note(summary)
            else:
                raise RuntimeError(summary) from None


@pytest_asyncio.fixture
async def artifact_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _stores(engine: AsyncEngine, runtime: _MinioRuntime):
    from artifact_store.service_impl import (
        GeneratedArtifactStoreImpl,
        RawArtifactStoreImpl,
    )
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    factory = async_sessionmaker(engine, expire_on_commit=False)
    uow_factory = lambda tenant: SqlAlchemyArtifactUnitOfWork(factory, tenant)
    transport = S3ObjectBlobTransport(runtime.settings, runtime.secrets)
    return (
        RawArtifactStoreImpl(
            uow_factory,
            transport,
            runtime.settings.raw_max_bytes,
            lambda: datetime.now(UTC),
            new_id,
        ),
        GeneratedArtifactStoreImpl(
            uow_factory,
            transport,
            runtime.settings.generated_max_bytes,
            lambda: datetime.now(UTC),
            new_id,
        ),
    )


async def _put_generated(store, tenant: TenantId, content: bytes = b"draft"):
    subject = f"enr_{str(new_id('enr')).split('_', 1)[1]}"
    key = IdempotencyKey(f"{subject}:1:draft")
    return await store.put(
        tenant,
        GeneratedArtifactKind.EMAIL_DRAFT,
        content,
        _GENERATED_MIME,
        workflow_run_id=RunId(new_id("run")),
        subject_ref=subject,
        sequence_number=1,
        idempotency_key=key,
        generated_by="outreach_agent_v1",
    )


def _object_keys(runtime: _MinioRuntime, prefix: str) -> list[str]:
    response = runtime.client.list_objects_v2(  # type: ignore[attr-defined]
        Bucket=runtime.settings.bucket, Prefix=prefix
    )
    return sorted(item["Key"] for item in response.get("Contents", []))


async def test_real_minio_raw_put_get_meta_and_dedup(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    first = await raw.put(
        tenant, RawArtifactKind.PDF, b"real-pdf", "application/pdf"
    )
    second = await raw.put(
        tenant, RawArtifactKind.PDF, b"real-pdf", "application/pdf"
    )
    assert second == first
    assert await raw.get_meta(tenant, first.artifact_id) == first
    assert await raw.get(tenant, first.artifact_id) == (first, b"real-pdf")
    assert _object_keys(minio_runtime, f"raw/{tenant}/") == [
        f"raw/{tenant}/{first.artifact_id}"
    ]


async def test_real_minio_generated_idempotency_and_conflict(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    _, generated = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    subject = f"enr_{str(new_id('enr')).split('_', 1)[1]}"
    key = IdempotencyKey(f"{subject}:1:draft")

    async def put(content: bytes):
        return await generated.put(
            tenant,
            GeneratedArtifactKind.EMAIL_DRAFT,
            content,
            _GENERATED_MIME,
            workflow_run_id=RunId("run_01KZXT00000000000000000001"),
            subject_ref=subject,
            sequence_number=1,
            idempotency_key=key,
            generated_by="outreach_agent_v1",
        )

    first = await put(b"safe-draft")
    assert await put(b"safe-draft") == first
    with pytest.raises(ArtifactConflictError):
        await put(b"changed-draft")
    assert _object_keys(minio_runtime, f"generated/{tenant}/") == [
        f"generated/{tenant}/{first.artifact_id}"
    ]


async def test_twenty_concurrent_raw_puts_have_one_row_and_object(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    results = await asyncio.gather(
        *(
            raw.put(
                tenant,
                RawArtifactKind.PDF,
                b"concurrent-raw",
                "application/pdf",
            )
            for _ in range(20)
        )
    )
    assert len({item.artifact_id for item in results}) == 1
    async with AsyncSession(artifact_engine) as session:
        assert await session.scalar(
            select(func.count()).select_from(RawArtifactRow).where(
                RawArtifactRow.tenant_id == str(tenant)
            )
        ) == 1
    assert len(_object_keys(minio_runtime, f"raw/{tenant}/")) == 1


async def test_twenty_concurrent_generated_puts_have_one_winner(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    _, generated = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    subject = f"enr_{str(new_id('enr')).split('_', 1)[1]}"
    key = IdempotencyKey(f"{subject}:1:draft")

    async def put():
        return await generated.put(
            tenant,
            GeneratedArtifactKind.EMAIL_DRAFT,
            b"concurrent-draft",
            _GENERATED_MIME,
            workflow_run_id=RunId("run_01KZXT00000000000000000002"),
            subject_ref=subject,
            sequence_number=1,
            idempotency_key=key,
            generated_by="outreach_agent_v1",
        )

    results = await asyncio.gather(*(put() for _ in range(20)))
    assert len({item.artifact_id for item in results}) == 1
    async with AsyncSession(artifact_engine) as session:
        assert await session.scalar(
            select(func.count()).select_from(GeneratedArtifactRow).where(
                GeneratedArtifactRow.tenant_id == str(tenant)
            )
        ) == 1
    assert len(_object_keys(minio_runtime, f"generated/{tenant}/")) == 1


async def test_same_bytes_across_tenants_do_not_share_metadata_or_object(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenants = [TenantId(new_id("tn")), TenantId(new_id("tn"))]
    metas = [
        await raw.put(
            tenant, RawArtifactKind.PDF, b"tenant-bytes", "application/pdf"
        )
        for tenant in tenants
    ]
    assert metas[0].artifact_id != metas[1].artifact_id
    assert metas[0].content_hash == metas[1].content_hash
    for tenant, meta in zip(tenants, metas, strict=True):
        assert _object_keys(minio_runtime, f"raw/{tenant}/") == [
            f"raw/{tenant}/{meta.artifact_id}"
        ]


async def test_cross_tenant_get_and_nonexistent_are_identical(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    meta = await raw.put(tenant, RawArtifactKind.PDF, b"pdf", "application/pdf")
    messages = []
    for requested_tenant, artifact_id in (
        (other, meta.artifact_id),
        (tenant, type(meta.artifact_id)(new_id("art"))),
    ):
        with pytest.raises(ArtifactNotFoundError) as exc:
            await raw.get(requested_tenant, artifact_id)
        messages.append(str(exc.value))
    assert messages == ["Artifact 不存在", "Artifact 不存在"]


async def test_mutated_minio_object_is_rejected_by_integrity_check(
    artifact_engine: AsyncEngine,
    minio_runtime: _MinioRuntime,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    meta = await raw.put(
        tenant, RawArtifactKind.PDF, b"original", "application/pdf"
    )
    key = f"raw/{tenant}/{meta.artifact_id}"
    minio_runtime.client.put_object(  # type: ignore[attr-defined]
        Bucket=minio_runtime.settings.bucket, Key=key, Body=b"tampered"
    )
    with caplog.at_level(
        logging.CRITICAL, logger="security.artifact_integrity"
    ), pytest.raises(ArtifactIntegrityError):
        await raw.get(tenant, meta.artifact_id)
    records = [r for r in caplog.records if r.name == "security.artifact_integrity"]
    assert len(records) == 1
    assert records[0].getMessage() == "检测到 Artifact 完整性违规"
    assert "tampered" not in repr(records[0].__dict__)


async def test_real_minio_missing_blob_maps_to_fixed_not_found(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    meta = await raw.put(tenant, RawArtifactKind.PDF, b"pdf", "application/pdf")
    key = f"raw/{tenant}/{meta.artifact_id}"
    minio_runtime.client.delete_object(  # type: ignore[attr-defined]
        Bucket=minio_runtime.settings.bucket, Key=key
    )
    with pytest.raises(ArtifactNotFoundError, match="^Artifact 不存在$"):
        await raw.get(tenant, meta.artifact_id)


async def test_unavailable_minio_does_not_create_metadata(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    settings = S3ObjectStoreSettings(
        True,
        f"http://127.0.0.1:{port}",
        minio_runtime.settings.bucket,
        "TEST_MINIO_ACCESS",
        "TEST_MINIO_SECRET",
        "us-east-1",
        1024,
        1024,
    )
    factory = async_sessionmaker(artifact_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    store = RawArtifactStoreImpl(
        lambda requested: SqlAlchemyArtifactUnitOfWork(factory, requested),
        S3ObjectBlobTransport(settings, minio_runtime.secrets),
        1024,
        lambda: datetime.now(UTC),
        new_id,
    )
    with pytest.raises(ArtifactCommitUnknownError):
        await store.put(tenant, RawArtifactKind.PDF, b"pdf", "application/pdf")
    async with AsyncSession(artifact_engine) as session:
        assert await session.scalar(
            select(func.count()).select_from(RawArtifactRow).where(
                RawArtifactRow.tenant_id == str(tenant)
            )
        ) == 0


async def test_commit_failure_keeps_real_object_and_body_marker_is_not_in_sql(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    from artifact_store.service_impl import RawArtifactStoreImpl
    from connectors.object_store.s3 import S3ObjectBlobTransport
    from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork

    class CommitFailureSession(AsyncSession):
        async def commit(self) -> None:
            raise RuntimeError("commit-marker")

    factory = async_sessionmaker(
        artifact_engine, expire_on_commit=False, class_=CommitFailureSession
    )
    tenant = TenantId(new_id("tn"))
    marker = b"unique-customer-body-marker"
    transport = S3ObjectBlobTransport(minio_runtime.settings, minio_runtime.secrets)
    store = RawArtifactStoreImpl(
        lambda requested: SqlAlchemyArtifactUnitOfWork(factory, requested),
        transport,
        1024,
        lambda: datetime.now(UTC),
        new_id,
    )
    with pytest.raises(ArtifactCommitUnknownError):
        await store.put(tenant, RawArtifactKind.PDF, marker, "application/pdf")
    keys = _object_keys(minio_runtime, f"raw/{tenant}/")
    assert len(keys) == 1
    assert await transport.get(keys[0]) == marker
    async with artifact_engine.connect() as conn:
        textual = await conn.scalar(
            text(
                "SELECT coalesce(string_agg(row_to_json(t)::text, ''), '') "
                "FROM (SELECT * FROM raw_artifacts WHERE tenant_id=:tenant "
                "UNION ALL SELECT tenant_id,artifact_id,kind,content_hash,"
                "size_bytes,mime_type,object_key,NULL::varchar,generated_at "
                "FROM artifacts WHERE tenant_id=:tenant) AS t"
            ),
            {"tenant": str(tenant)},
        )
    assert marker.decode() not in textual


async def test_successful_body_marker_never_enters_sql_metadata(
    artifact_engine: AsyncEngine, minio_runtime: _MinioRuntime
) -> None:
    raw, _ = _stores(artifact_engine, minio_runtime)
    tenant = TenantId(new_id("tn"))
    marker = b"successful-unique-customer-body-marker"
    meta = await raw.put(
        tenant, RawArtifactKind.PDF, marker, "application/pdf"
    )
    assert await raw.get(tenant, meta.artifact_id) == (meta, marker)
    async with artifact_engine.connect() as conn:
        textual = await conn.scalar(
            text(
                "SELECT coalesce(string_agg(row_to_json(t)::text, ''), '') "
                "FROM (SELECT * FROM raw_artifacts WHERE tenant_id=:tenant) AS t"
            ),
            {"tenant": str(tenant)},
        )
    assert marker.decode() not in textual


def test_real_minio_writes_are_tenant_partitioned(
    minio_runtime: _MinioRuntime,
) -> None:
    keys = _object_keys(minio_runtime, "")
    prefixes = Counter(key.split("/", 2)[0] for key in keys)
    assert set(prefixes) <= {"raw", "generated"}
