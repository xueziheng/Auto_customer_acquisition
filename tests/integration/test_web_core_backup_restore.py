"""静止owned数据恢复演练；不接受运行库、外部dump或真实配置路径。"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import secrets
import tarfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import RawArtifactKind
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import S3ObjectBlobTransport
from infra.controlled.config import ControlledConfig, ControlledError
from infra.controlled.resources import OwnedContainers
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.session import create_engine_from
from scripts.controlled_web_supervisor import Supervisor
from scripts.run_web_core_controlled import reserve
from shared.schemas.identifiers import TenantId, new_id

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/acceptance/web-core-delivery/backup-restore.json"


def _close_safely(
    operation: Callable[[], object], reason: str, errors: list[str]
) -> None:
    try:
        operation()
    except Exception:  # noqa: BLE001 关闭失败只保留固定类别，其余回调继续
        errors.append(reason)


async def _aclose_safely(
    operation: Callable[[], Awaitable[object]], reason: str, errors: list[str]
) -> None:
    try:
        await operation()
    except Exception:  # noqa: BLE001 关闭失败只保留固定类别，其余回调继续
        errors.append(reason)


@contextmanager
def _resources(errors: list[str]) -> Iterator[ExitStack]:
    """逐项注册关闭；主失败不被关闭失败覆盖，单独清理失败不得报通过。"""
    before = len(errors)
    with ExitStack() as closing:
        yield closing
    if len(errors) != before:
        raise ControlledError("backup_resource_cleanup_failed")


@asynccontextmanager
async def _async_resources(errors: list[str]) -> AsyncIterator[AsyncExitStack]:
    before = len(errors)
    async with AsyncExitStack() as closing:
        yield closing
    if len(errors) != before:
        raise ControlledError("backup_resource_cleanup_failed")


def _verify(supervisor: Supervisor) -> ControlledConfig:
    """每次动作前核本owner配置、精确容器ID/标签，且没有应用写入者。"""
    config = ControlledConfig.read(supervisor.directory / "config.json")
    if config.owner != supervisor.owner or supervisor.processes:
        raise ControlledError("backup_owner_rejected")
    if supervisor.containers is None or len(supervisor.containers.ids) != 2:
        raise ControlledError("backup_owner_rejected")
    for container_id in supervisor.containers.ids:
        supervisor.containers.verify(container_id)
    return config


def _pg(supervisor: Supervisor, command: list[str]) -> bytes:
    _verify(supervisor)
    result = supervisor.containers.verify(supervisor.containers.ids[0]).exec_run(
        command
    )
    if result.exit_code != 0:
        raise ControlledError("backup_database_command_failed")
    return result.output


def _require_empty_target(source: Supervisor, target: Supervisor) -> None:
    """恢复拒绝同owner或已有public关系的目标，不删表或关闭不可变约束。"""
    if source.owner == target.owner:
        raise ControlledError("backup_target_not_distinct")
    count = _pg(
        target,
        [
            "psql",
            "-U",
            "controlled",
            "-d",
            "controlled",
            "-At",
            "-c",
            (
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='public'"
            ),
        ],
    )
    if count.strip() != b"0":
        raise ControlledError("backup_target_not_empty")


def _client(config: ControlledConfig):
    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{config.object_port}",
        aws_access_key_id=config.resolve("CONTROLLED_OBJECT_ACCESS"),
        aws_secret_access_key=config.resolve("CONTROLLED_OBJECT_SECRET"),
        region_name="us-east-1",
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            proxies={},
            connect_timeout=2,
            read_timeout=2,
            retries={"max_attempts": 0},
        ),
    )


def _empty_target(
    target: Supervisor, source_config: ControlledConfig, cleanup_errors: list[str]
) -> None:
    """仅建另一新owned空PG/MinIO，不迁移/初始化员工，不启动应用。"""
    target.containers = OwnedContainers(target.owner)
    passphrase, access, secret = (
        secrets.token_hex(24),
        secrets.token_hex(16),
        secrets.token_hex(32),
    )
    database_id, database_port = target.containers.create(
        "pgvector/pgvector:pg16",
        port=5432,
        environment={
            "POSTGRES_USER": "controlled",
            "POSTGRES_PASSWORD": passphrase,
            "POSTGRES_DB": "controlled",
        },
    )
    deadline = time.monotonic() + 30
    while (
        target.containers.verify(database_id)
        .exec_run(["pg_isready", "-U", "controlled", "-d", "controlled"])
        .exit_code
        != 0
    ):
        if time.monotonic() >= deadline:
            raise ControlledError("backup_target_not_ready")
        time.sleep(0.1)
    _, object_port = target.containers.create(
        "minio/minio:RELEASE.2025-04-22T22-12-26Z",
        port=9000,
        environment={"MINIO_ROOT_USER": access, "MINIO_ROOT_PASSWORD": secret},
        command=[
            "server",
            "/data",
            "--address",
            ":9000",
            "--console-address",
            "127.0.0.1:9001",
        ],
    )
    config = source_config.model_copy(
        update={
            "owner": target.owner,
            "database_port": database_port,
            "object_port": object_port,
            "bucket": "controlled-" + target.owner,
            "database_url": SecretStr(
                URL.create(
                    "postgresql+asyncpg",
                    "controlled",
                    passphrase,
                    "127.0.0.1",
                    database_port,
                    "controlled",
                ).render_as_string(False)
            ),
            "secrets": {
                "CONTROLLED_OBJECT_ACCESS": SecretStr(access),
                "CONTROLLED_OBJECT_SECRET": SecretStr(secret),
            },
            "api_port": target.listeners[0].getsockname()[1],
            "web_port": target.listeners[1].getsockname()[1],
            "scheduler_port": target.listeners[2].getsockname()[1],
            "notification_port": target.listeners[3].getsockname()[1],
        }
    )
    config.write(target.directory / "config.json")
    target.config = ControlledConfig.read(target.directory / "config.json")
    with _resources(cleanup_errors) as closing:
        client = _client(_verify(target))
        closing.callback(
            _close_safely, client.close, "target_client_close_failed", cleanup_errors
        )
        deadline = time.monotonic() + 30
        while True:
            _verify(target)
            try:
                client.create_bucket(Bucket=config.bucket)
                break
            except Exception:  # noqa: BLE001 固定就绪失败，不展示SDK异常
                if time.monotonic() >= deadline:
                    raise ControlledError("backup_object_not_ready") from None
                time.sleep(0.1)


async def _artifact(
    supervisor: Supervisor, artifact_id=None, cleanup_errors: list[str] | None = None
):
    config = _verify(supervisor)
    errors = [] if cleanup_errors is None else cleanup_errors
    async with _async_resources(errors) as closing:
        engine = create_engine_from(config.database_url.get_secret_value())
        closing.push_async_callback(
            _aclose_safely, engine.dispose, "engine_dispose_failed", errors
        )
        settings = S3ObjectStoreSettings.from_environ(config.runtime_environment())
        transport = S3ObjectBlobTransport(settings, config)
        closing.push_async_callback(
            _aclose_safely, transport.aclose, "transport_close_failed", errors
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        store = RawArtifactStoreImpl(
            lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),
            transport,
            settings.raw_max_bytes,
            lambda: datetime.now(UTC),
            new_id,
        )
        tenant = TenantId(config.tenant_id)
        if artifact_id is None:
            meta = await store.put(
                tenant,
                RawArtifactKind.EMAIL_RAW,
                b"Message-ID: <backup-fixture@controlled.test>\r\n\r\nSynthetic backup fixture.\r\n",
                "message/rfc822",
            )
            artifact_id = meta.artifact_id
        meta, content = await store.get(tenant, artifact_id)
        async with sessions() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM raw_artifacts WHERE tenant_id = :tenant"),
                {"tenant": str(tenant)},
            )
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        safe = asdict(meta)
        safe["uploaded_at"] = meta.uploaded_at.isoformat()
        return {
            "metadata": safe,
            "metadata_sha256": hashlib.sha256(
                json.dumps(safe, sort_keys=True).encode()
            ).hexdigest(),
            "bytes_sha256": hashlib.sha256(content).hexdigest(),
            "row_count": count,
            "schema_head": revision,
        }


def _restore(
    source: Supervisor, target: Supervisor, cleanup_errors: list[str] | None = None
) -> None:
    _require_empty_target(source, target)
    dump = _pg(
        source,
        [
            "pg_dump",
            "-U",
            "controlled",
            "-d",
            "controlled",
            "-Fc",
            "--no-owner",
            "--no-acl",
        ],
    )
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w") as archive:
        info = tarfile.TarInfo("task13-owned.dump")
        info.mode, info.size = 0o600, len(dump)
        archive.addfile(info, io.BytesIO(dump))
    _verify(target)
    container = target.containers.verify(target.containers.ids[0])
    if not container.put_archive("/tmp", packed.getvalue()):
        raise ControlledError("backup_transfer_failed")
    try:
        _pg(
            target,
            [
                "pg_restore",
                "-U",
                "controlled",
                "-d",
                "controlled",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "/tmp/task13-owned.dump",
            ],
        )
    finally:
        _pg(target, ["rm", "-f", "/tmp/task13-owned.dump"])
    source_config, target_config = _verify(source), _verify(target)
    errors = [] if cleanup_errors is None else cleanup_errors
    with _resources(errors) as closing:
        first = _client(source_config)
        closing.callback(
            _close_safely, first.close, "source_client_close_failed", errors
        )
        second = _client(target_config)
        closing.callback(
            _close_safely, second.close, "target_client_close_failed", errors
        )
        _verify(target)
        if second.list_objects_v2(Bucket=target_config.bucket).get("KeyCount") != 0:
            raise ControlledError("backup_object_target_not_empty")
        _verify(source)
        objects = first.list_objects_v2(Bucket=source_config.bucket)
        if objects.get("IsTruncated") or objects.get("KeyCount") != 1:
            raise ControlledError("backup_fixture_object_count_invalid")
        key = objects["Contents"][0]["Key"]
        _verify(source)
        body = first.get_object(Bucket=source_config.bucket, Key=key)["Body"]
        try:
            content = body.read(10485761)
        finally:
            body.close()
        if len(content) > 10485760:
            raise ControlledError("backup_fixture_too_large")
        _verify(target)
        second.put_object(Bucket=target_config.bucket, Key=key, Body=content)


async def test_owned_static_pg_and_original_restore_to_distinct_empty_target(
    tmp_path: Path,
) -> None:
    """实际dump/restore与Store读取一致；同owner、非空目标拒绝，最后精确清理。"""
    prior_logging = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        supervisors = []
        resource_errors: list[str] = []
        primary_failed = False
        result = {
            "date": "2026-09-06",
            "status": "failed",
            "scope": "static_owned_pg_and_raw_object_only",
            "resource_cleanup_errors": resource_errors,
        }
        stage = "create_source"
        try:
            source = Supervisor(ROOT, tmp_path, [reserve(0) for _ in range(3)])
            supervisors.append(source)
            source.start_infrastructure()
            source_config = _verify(source)
            stage = "source_artifact"
            original = await _artifact(source, cleanup_errors=resource_errors)
            try:
                _require_empty_target(source, source)
            except ControlledError as exc:
                if exc.reason != "backup_target_not_distinct":
                    raise
            else:
                raise ControlledError("backup_same_owner_not_rejected")
            stage = "create_target"
            target = Supervisor(ROOT, tmp_path, [reserve(0) for _ in range(3)])
            supervisors.append(target)
            _empty_target(target, source_config, resource_errors)
            stage = "restore"
            _restore(source, target, resource_errors)
            stage = "verify_metadata_and_original"
            restored = await _artifact(
                target, original["metadata"]["artifact_id"], resource_errors
            )
            source_after = await _artifact(
                source, original["metadata"]["artifact_id"], resource_errors
            )
            if source_after != original:
                raise ControlledError("backup_source_changed")
            if (
                original != restored
                or original["row_count"] != 1
                or original["schema_head"] != "0066"
            ):
                raise ControlledError("backup_integrity_mismatch")
            try:
                _require_empty_target(source, target)
            except ControlledError as exc:
                if exc.reason != "backup_target_not_empty":
                    raise
            else:
                raise ControlledError("backup_nonempty_not_rejected")
            result.update(
                status="passed",
                source_owner=source.owner,
                target_owner=target.owner,
                source=original,
                target=restored,
                same_owner_rejected=True,
                nonempty_target_rejected=True,
                application_writers=0,
                dump_retained=False,
                source_unchanged=True,
            )
        except Exception:  # noqa: BLE001 原始SDK/SQL异常、配置、dump不进入pytest输出
            primary_failed = True
            raise AssertionError("backup_restore_failed:" + stage) from None
        finally:
            cleanup = []
            for supervisor in reversed(supervisors):
                ids = list(supervisor.containers.ids) if supervisor.containers else []
                supervisor.close()
                cleanup.append(
                    {
                        "owner": supervisor.owner,
                        "container_ids": ids,
                        "errors": supervisor.cleanup_errors,
                        "private_config_removed": not (
                            supervisor.directory / "config.json"
                        ).exists(),
                        "processes": [
                            {
                                "name": item.name,
                                "pid": item.process.pid,
                                "born": item.born,
                            }
                            for item in supervisor.processes
                        ],
                        "listener_fds": [
                            listener.fileno() for listener in supervisor.listeners
                        ],
                    }
                )
            result["cleanup"] = cleanup
            if any(
                item["errors"] or not item["private_config_removed"] for item in cleanup
            ):
                result["status"] = "cleanup_unknown"
            try:
                EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
                EVIDENCE.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False) + "\n"
                )
            except Exception:  # noqa: BLE001 证据写失败固定化，不能覆盖已有主失败
                if not primary_failed:
                    raise ControlledError("backup_evidence_write_failed") from None
        assert result["status"] == "passed"
        assert all(
            item["listener_fds"] == [-1, -1, -1, -1] for item in result["cleanup"]
        )
    finally:
        logging.disable(prior_logging)
