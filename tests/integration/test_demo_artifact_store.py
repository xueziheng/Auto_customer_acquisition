"""Artifact Store 真实 PostgreSQL + MinIO 演示的进程级验收。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.errors import ArtifactNotFoundError
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.object_store.s3 import S3ObjectBlobTransport
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tables import GeneratedArtifactRow, RawArtifactRow
from scripts import demo_artifact_store
from shared.schemas.identifiers import ArtifactId, TenantId, new_id
from tests.integration.test_artifact_store_minio import (
    minio_runtime as _base_minio_runtime,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_PLANE = _REPO_ROOT / "docs/architecture/05-data-plane.md"
_DATABASE = _REPO_ROOT / "docs/architecture/10-database.md"
_SUMMARY_KEYS = {
    "tenant_id",
    "raw_artifact_id",
    "generated_artifact_id",
    "raw_hash",
    "generated_hash",
    "raw_put_count",
    "raw_row_count",
    "generated_put_count",
    "generated_row_count",
}
_BODY_MARKERS = (
    "artifact-demo-raw-content-marker",
    "artifact-demo-generated-content-marker",
)


class _DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.fixture(scope="session")
def artifact_minio_runtime() -> Iterator[Any]:
    """复用真实 MinIO fixture，同时保持本文件的 lint 边界明确。"""
    yield from _base_minio_runtime.__wrapped__()


def _environment(database_url: str, runtime: Any) -> dict[str, str]:
    settings = runtime.settings
    return {
        "DATABASE_URL": database_url,
        "TRADEOS_DEV_MODE": "true",
        "S3_ENDPOINT": settings.endpoint,
        "S3_BUCKET_ARTIFACTS": settings.bucket,
        "S3_ACCESS_KEY_REF": settings.access_key_ref,
        "S3_SECRET_KEY_REF": settings.secret_key_ref,
        "S3_REGION": settings.region,
        "RAW_ARTIFACT_MAX_BYTES": str(settings.raw_max_bytes),
        "GENERATED_ARTIFACT_MAX_BYTES": str(settings.generated_max_bytes),
        settings.access_key_ref: runtime.secrets.resolve(settings.access_key_ref),
        settings.secret_key_ref: runtime.secrets.resolve(settings.secret_key_ref),
    }


async def test_demo_disposes_engine_when_composition_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _DisposableEngine()
    environ = {
        "DATABASE_URL": "postgresql+asyncpg://unused.invalid/db",
        "TRADEOS_DEV_MODE": "false",
        "S3_ENDPOINT": "https://objects.example.invalid",
        "S3_BUCKET_ARTIFACTS": "tradeos-artifacts",
        "S3_ACCESS_KEY_REF": "ARTIFACT_S3_ACCESS_KEY",
        "S3_SECRET_KEY_REF": "ARTIFACT_S3_SECRET_KEY",
        "S3_REGION": "us-east-1",
        "RAW_ARTIFACT_MAX_BYTES": "1048576",
        "GENERATED_ARTIFACT_MAX_BYTES": "1048576",
        "ARTIFACT_S3_ACCESS_KEY": "access-value",
        "ARTIFACT_S3_SECRET_KEY": "secret-value",
    }
    monkeypatch.setattr(
        demo_artifact_store, "create_engine_from", lambda _: engine
    )

    def fail_transport(*_args: object) -> object:
        raise RuntimeError("composition failed")

    monkeypatch.setattr(
        demo_artifact_store, "S3ObjectBlobTransport", fail_transport
    )
    with pytest.raises(RuntimeError, match="^composition failed$"):
        await demo_artifact_store.run_demo(environ)
    assert engine.disposed is True


def _run(environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/demo_artifact_store.py"],
        cwd=_REPO_ROOT,
        env={
            **environ,
            "PYTHONPATH": str(_REPO_ROOT),
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _summary(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, "Artifact Store 演示子进程失败（输出已脱敏）"
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    assert set(value) == _SUMMARY_KEYS
    assert isinstance(value["tenant_id"], str)
    assert isinstance(value["raw_artifact_id"], str)
    assert isinstance(value["generated_artifact_id"], str)
    assert value["raw_artifact_id"] != value["generated_artifact_id"]
    for name in ("raw_hash", "generated_hash"):
        assert isinstance(value[name], str)
        assert len(value[name]) == 64
        assert set(value[name]) <= set("0123456789abcdef")
    assert value["raw_put_count"] == 2
    assert value["raw_row_count"] == 1
    assert value["generated_put_count"] == 2
    assert value["generated_row_count"] == 1
    return value


async def _readback(
    database_url: str, runtime: Any, summary: dict[str, object]
) -> None:
    tenant = TenantId(summary["tenant_id"])
    engine = create_engine_from(database_url)
    try:
        async with AsyncSession(engine) as session:
            raw_rows = (
                await session.execute(
                    select(RawArtifactRow).where(
                        RawArtifactRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
            generated_rows = (
                await session.execute(
                    select(GeneratedArtifactRow).where(
                        GeneratedArtifactRow.tenant_id == str(tenant)
                    )
                )
            ).scalars().all()
        assert len(raw_rows) == len(generated_rows) == 1
        raw_row = raw_rows[0]
        generated_row = generated_rows[0]
        assert raw_row.artifact_id == summary["raw_artifact_id"]
        assert generated_row.artifact_id == summary["generated_artifact_id"]
        assert raw_row.content_hash == summary["raw_hash"]
        assert generated_row.content_hash == summary["generated_hash"]
        for row in (raw_row, generated_row):
            response = runtime.client.get_object(
                Bucket=runtime.settings.bucket, Key=row.object_key
            )
            try:
                content = response["Body"].read()
            finally:
                response["Body"].close()
            assert len(content) == row.size_bytes
            assert hashlib.sha256(content).hexdigest() == row.content_hash

        factory = async_sessionmaker(engine, expire_on_commit=False)
        raw_store = RawArtifactStoreImpl(
            lambda requested: SqlAlchemyArtifactUnitOfWork(factory, requested),
            S3ObjectBlobTransport(runtime.settings, runtime.secrets),
            runtime.settings.raw_max_bytes,
            lambda: raw_row.uploaded_at,
            new_id,
        )
        other = TenantId(new_id("tn"))
        messages = []
        for requested_tenant, artifact_id in (
            (other, ArtifactId(raw_row.artifact_id)),
            (tenant, ArtifactId(new_id("art"))),
        ):
            with pytest.raises(ArtifactNotFoundError) as exc:
                await raw_store.get(requested_tenant, artifact_id)
            messages.append(str(exc.value))
        assert messages == ["Artifact 不存在", "Artifact 不存在"]
    finally:
        await engine.dispose()


async def test_demo_runs_twice_with_isolated_real_postgres_and_minio(
    db_url: str, artifact_minio_runtime: Any
) -> None:
    environ = _environment(db_url, artifact_minio_runtime)
    first_result, second_result = await asyncio.gather(
        asyncio.to_thread(_run, environ),
        asyncio.to_thread(_run, environ),
    )
    first = _summary(first_result)
    second = _summary(second_result)
    assert first["tenant_id"] != second["tenant_id"]
    assert first["raw_artifact_id"] != second["raw_artifact_id"]
    assert first["generated_artifact_id"] != second["generated_artifact_id"]
    await _readback(db_url, artifact_minio_runtime, first)
    await _readback(db_url, artifact_minio_runtime, second)

    captured = first_result.stdout + first_result.stderr + second_result.stdout
    for forbidden in (
        artifact_minio_runtime.settings.endpoint,
        artifact_minio_runtime.settings.bucket,
        artifact_minio_runtime.secrets.resolve(
            artifact_minio_runtime.settings.access_key_ref
        ),
        artifact_minio_runtime.secrets.resolve(
            artifact_minio_runtime.settings.secret_key_ref
        ),
        *_BODY_MARKERS,
    ):
        assert forbidden not in captured


@pytest.mark.parametrize("failure", ["dsn", "endpoint", "secret", "unavailable"])
async def test_demo_failures_are_fixed_and_do_not_reflect_configuration(
    failure: str, db_url: str, artifact_minio_runtime: Any
) -> None:
    environ = _environment(db_url, artifact_minio_runtime)
    marker = "injected-sensitive-marker"
    if failure == "dsn":
        environ["DATABASE_URL"] = (
            f"postgresql+asyncpg://{marker}:password@127.0.0.1:1/missing"
        )
    elif failure == "endpoint":
        environ["S3_ENDPOINT"] = f"https://{marker}.invalid/path"
    elif failure == "secret":
        environ.pop(environ["S3_SECRET_KEY_REF"])
    else:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        environ["S3_ENDPOINT"] = f"http://127.0.0.1:{port}"
        marker = environ["S3_ENDPOINT"]
    result = await asyncio.to_thread(_run, environ)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "Artifact Store 演示运行失败\n"
    assert marker not in result.stdout + result.stderr
    for value in (
        artifact_minio_runtime.secrets.resolve(
            artifact_minio_runtime.settings.access_key_ref
        ),
        artifact_minio_runtime.secrets.resolve(
            artifact_minio_runtime.settings.secret_key_ref
        ),
        *_BODY_MARKERS,
    ):
        assert value not in result.stdout + result.stderr


async def test_demo_two_runs_create_only_two_rows_each(
    db_url: str, artifact_minio_runtime: Any
) -> None:
    results = [
        _summary(
            await asyncio.to_thread(
                _run, _environment(db_url, artifact_minio_runtime)
            )
        )
        for _ in range(2)
    ]
    engine = create_engine_from(db_url)
    try:
        async with AsyncSession(engine) as session:
            for summary in results:
                tenant = str(summary["tenant_id"])
                assert await session.scalar(
                    select(func.count()).select_from(RawArtifactRow).where(
                        RawArtifactRow.tenant_id == tenant
                    )
                ) == 1
                assert await session.scalar(
                    select(func.count()).select_from(GeneratedArtifactRow).where(
                        GeneratedArtifactRow.tenant_id == tenant
                    )
                ) == 1
    finally:
        await engine.dispose()


def test_artifact_store_docs_have_stable_layer_and_table_contracts() -> None:
    data_plane = _DATA_PLANE.read_text(encoding="utf-8")
    database = _DATABASE.read_text(encoding="utf-8")
    assert "Raw Artifact（原始证据）" in data_plane
    assert "Generated Artifact（派生产物）" in data_plane
    assert "派生产物不能作为原始证据" in data_plane
    assert "## Artifact Store metadata" in database
    assert "raw_artifacts" in database
    assert "artifacts" in database
    assert "禁止内容列" in database
