"""本机持久卷与精确归属核验；停止永不 remove，不继承 Docker 环境。"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

import boto3  # type: ignore[import-untyped]
import docker  # type: ignore[import-untyped]
import psutil  # type: ignore[import-untyped]
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.authentication.service import PostgresAuthentication
from infra.controlled.resources import OwnedProcess, process_identity
from infra.db.session import create_engine_from
from shared.schemas.identifiers import TenantId

from .config import (
    PilotConfig,
    PilotError,
    StorageIdentity,
    StrictModel,
    exclusive_profile_lock,
    private_read,
    private_write,
)

OWNER_LABEL = "tradeos.pilot.owner"
ROLE_LABEL = "tradeos.pilot.role"
PILOT_MINIO_IMAGE = (
    "bitnamilegacy/minio@"
    "sha256:50cec18ac4184af4671a78aedd5554942c8ae105d51a465fa82037949046da01"
)
IMAGES = {
    "database": "pgvector/pgvector:pg16",
    "objects": PILOT_MINIO_IMAGE,
}
MOUNTS = {
    "database": "/var/lib/postgresql/data",
    "objects": "/bitnami/minio/data",
}
MINIO_CERTS_TMPFS = "rw,noexec,nosuid,size=65536"
ROOT = Path(__file__).resolve().parents[2]


def _mounts_match(
    kind: Literal["database", "objects"],
    mounts: Sequence[Mapping[str, Any]],
    volume_name: str,
) -> bool:
    """只接受持久数据卷；MinIO 的证书目录必须是非持久 tmpfs。"""
    data = [mount for mount in mounts if mount.get("Destination") == MOUNTS[kind]]
    if len(data) != 1:
        return False
    target = data[0]
    if (
        target.get("Type") != "volume"
        or target.get("Name") != volume_name
        or target.get("RW") is not True
    ):
        return False
    return len(mounts) == 1


def reserve_port(port: int) -> socket.socket:
    """保留精确 loopback 端口供应用继承；占用时拒绝而不漂移入口。"""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        return listener
    except OSError:
        listener.close()
        raise PilotError("port_unavailable") from None


class ProcessIdentity(StrictModel):
    pid: int = Field(strict=True, gt=0)
    born: float = Field(gt=0)

    @classmethod
    def current(cls) -> ProcessIdentity:
        return cls(pid=os.getpid(), born=process_identity(os.getpid()))

    def live(self) -> bool:
        """出生时间不匹配不是已停止，必须拒绝错误 PID 的后续操作。"""
        if not sys.platform.startswith("linux") and self.born < psutil.boot_time():
            return False
        try:
            process = psutil.Process(self.pid)
            if process_identity(self.pid) != self.born:
                raise PilotError("process_identity_invalid")
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.Error:
            raise PilotError("process_identity_invalid") from None


class ProcessRecord(ProcessIdentity):
    name: Literal["api", "scheduler", "notification", "migration"]
    anchor: ProcessIdentity
    children: tuple[ProcessIdentity, ...]
    exit: int | None


class RuntimeState(StrictModel):
    owner: str
    supervisor: ProcessIdentity | None = None
    processes: tuple[ProcessRecord, ...] = ()
    status: Literal["stopped", "starting", "running", "failed"] = "stopped"
    reason: Literal[
        "requested_stop",
        "storage_ready",
        "applications_ready",
        "operation_failed",
        "restore_failed",
    ] = "requested_stop"


class PilotProfile:
    """每次公开变更重读私有配置并持操作锁；*_locked 仅供持锁的 supervisor。"""

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()
        self.config = PilotConfig.read(self.path / "config.json")
        try:
            self.client = docker.DockerClient(
                base_url="unix:///var/run/docker.sock", timeout=30
            )
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("docker_unavailable") from None

    def __repr__(self) -> str:
        return "PilotProfile()"

    def reload(self) -> PilotConfig:
        self.config = PilotConfig.read(self.path / "config.json")
        return self.config

    def save(self) -> None:
        """只允许已持 profile 锁的调用方保存当前端口与资源身份。"""
        self.config.write(self.path / "config.json")

    def runtime_state(self) -> RuntimeState:
        target = self.path / "runtime.json"
        if not target.exists() and not target.is_symlink():
            return RuntimeState(owner=self.config.owner)
        try:
            state = RuntimeState.model_validate_json(private_read(target))
            if state.owner != self.config.owner:
                raise ValueError()
            names = [p.name for p in state.processes]
            if len(set(names)) != len(names):
                raise ValueError()
            return state
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("runtime_state_invalid") from None

    def publish_processes_locked(
        self,
        *,
        supervisor: ProcessIdentity | None,
        processes: Sequence[OwnedProcess],
        status: Literal["stopped", "starting", "running", "failed"],
        reason: Literal[
            "requested_stop",
            "storage_ready",
            "applications_ready",
            "operation_failed",
            "restore_failed",
        ],
    ) -> None:
        """Task 4 发布实际 OwnedProcess 身份，禁止伪造 PID；由调用者持锁。"""
        records = tuple(
            ProcessRecord.model_validate(p.public()) for p in processes if not p.closed
        )
        if supervisor is not None and supervisor != ProcessIdentity.current():
            raise PilotError("process_identity_invalid")
        state = RuntimeState(
            owner=self.config.owner,
            supervisor=supervisor,
            processes=records,
            status=status,
            reason=reason,
        )
        private_write(self.path / "runtime.json", state.model_dump_json().encode())

    def require_no_processes(self) -> None:
        state = self.runtime_state()
        identities: list[ProcessIdentity] = []
        if state.supervisor:
            identities.append(state.supervisor)
        for record in state.processes:
            identities.extend([record, record.anchor, *record.children])
        if any(identity.live() for identity in identities):
            raise PilotError("profile_apps_running")

    def verify(self, kind: Literal["database", "objects"]) -> Any:
        """核对精确 ID、镜像、owner、卷 CreatedAt、挂载和独占使用，错误不回显 Docker。"""
        try:
            identity = self.config.storage[kind]
            if not identity.container_id or not identity.volume_created_at:
                raise ValueError()
            container = self.client.containers.get(identity.container_id)
            container.reload()
            labels = container.labels
            if (
                container.id != identity.container_id
                or labels.get(OWNER_LABEL) != self.config.owner
                or labels.get(ROLE_LABEL) != kind
                or container.attrs["Image"] != identity.image_id
            ):
                raise ValueError()
            volume = self.client.volumes.get(identity.volume_name)
            attrs = volume.attrs
            if (
                attrs["Name"] != identity.volume_name
                or attrs["CreatedAt"] != identity.volume_created_at
                or attrs["Labels"].get(OWNER_LABEL) != self.config.owner
                or attrs["Labels"].get(ROLE_LABEL) != kind
            ):
                raise ValueError()
            mounts = container.attrs["Mounts"]
            if not _mounts_match(kind, mounts, identity.volume_name):
                raise ValueError()
            tmpfs = container.attrs["HostConfig"].get("Tmpfs") or {}
            if kind == "objects":
                if container.attrs["Config"]["User"] != "1001" or tmpfs != {
                    "/certs": MINIO_CERTS_TMPFS
                }:
                    raise ValueError()
            elif tmpfs:
                raise ValueError()
            attached = self.client.containers.list(
                all=True, filters={"volume": identity.volume_name}
            )
            if {item.id for item in attached} != {identity.container_id}:
                raise ValueError()
            return container
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("resource_identity_invalid") from None

    def verify_all(self) -> dict[str, Any]:
        if set(self.config.storage) != {"database", "objects"}:
            raise PilotError("storage_uninitialized")
        return {kind: self.verify(kind) for kind in ("database", "objects")}

    def provision_storage(self, image_ids: Mapping[str, str] | None = None) -> None:
        with exclusive_profile_lock(self.path):
            self.reload()
            self.provision_storage_locked(image_ids)

    def provision_storage_locked(
        self, image_ids: Mapping[str, str] | None = None
    ) -> None:
        """只创建新 owner 的空卷和停止容器；失败保留已记录资源以便核对。"""
        self.require_no_processes()
        if self.config.storage:
            raise PilotError("storage_exists")
        try:
            images = {
                kind: self.client.images.get((image_ids or IMAGES)[kind]).id
                for kind in IMAGES
            }
            records = {
                kind: StorageIdentity(
                    image_id=image_id,
                    volume_name=f"tradeos-pilot-{self.config.owner}-{kind}",
                )
                for kind, image_id in images.items()
            }
            self.config = self.config.model_copy(update={"storage": records})
            self.save()
            for kind, identity in records.items():
                # create 可复用同名卷，因此必须先确认不存在；owner 名是随机且不可复用的。
                try:
                    self.client.volumes.get(identity.volume_name)
                except docker.errors.NotFound:
                    pass
                else:
                    raise PilotError("resource_identity_invalid")
                labels = {OWNER_LABEL: self.config.owner, ROLE_LABEL: kind}
                volume = self.client.volumes.create(
                    name=identity.volume_name, labels=labels
                )
                volume.reload()
                environment = (
                    {
                        "POSTGRES_USER": "pilot",
                        "POSTGRES_PASSWORD": self.config.resolve(
                            "PILOT_DATABASE_PASSWORD"
                        ),
                        "POSTGRES_DB": "pilot",
                    }
                    if kind == "database"
                    else {
                        "MINIO_ROOT_USER": self.config.resolve("PILOT_OBJECT_ACCESS"),
                        "MINIO_ROOT_PASSWORD": self.config.resolve(
                            "PILOT_OBJECT_SECRET"
                        ),
                    }
                )
                command = (
                    None
                    if kind == "database"
                    else [
                        "server",
                        MOUNTS[kind],
                        "--address",
                        ":9000",
                        "--console-address",
                        "127.0.0.1:9001",
                    ]
                )
                port = 5432 if kind == "database" else 9000
                container = self.client.containers.create(
                    identity.image_id,
                    command=command,
                    environment=environment,
                    labels=labels,
                    volumes={
                        identity.volume_name: {"bind": MOUNTS[kind], "mode": "rw"}
                    },
                    tmpfs=(
                        None if kind == "database" else {"/certs": MINIO_CERTS_TMPFS}
                    ),
                    user=None if kind == "database" else "1001",
                    ports={f"{port}/tcp": ("127.0.0.1", None)},
                    restart_policy={"Name": "no"},
                )
                records[kind] = identity.model_copy(
                    update={
                        "volume_created_at": volume.attrs["CreatedAt"],
                        "container_id": container.id,
                    }
                )
                self.config = self.config.model_copy(update={"storage": dict(records)})
                self.save()
            self.verify_all()
        except PilotError:
            raise
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("storage_provision_failed") from None

    def start_storage(self) -> None:
        with exclusive_profile_lock(self.path):
            self.reload()
            self.start_storage_locked()

    def start_storage_locked(self, *, restoring: bool = False) -> None:
        """只启动存储，重写 Docker 实际分配端口；不自动迁移或装配应用。"""
        self.require_no_processes()
        if self.config.restore_state in {"pending", "failed"} and not restoring:
            raise PilotError("restore_incomplete")
        containers = self.verify_all()
        try:
            for kind, container in containers.items():
                if container.status not in {"running", "created", "exited"}:
                    raise PilotError("storage_state_invalid")
                if container.status != "running":
                    container.start()
                container.reload()
                port = 5432 if kind == "database" else 9000
                bindings = container.attrs["NetworkSettings"]["Ports"][f"{port}/tcp"]
                if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
                    raise PilotError("container_binding_invalid")
                self.config = self.config.model_copy(
                    update={
                        "database_port" if kind == "database" else "object_port": int(
                            bindings[0]["HostPort"]
                        )
                    }
                )
                self.save()
            deadline = time.monotonic() + 45
            while (
                self.verify("database")
                .exec_run(
                    ["pg_isready", "-h", "127.0.0.1", "-U", "pilot", "-d", "pilot"]
                )
                .exit_code
                != 0
            ):
                if time.monotonic() >= deadline:
                    raise PilotError("database_not_ready")
                time.sleep(0.1)
            with self.object_client() as client:
                while True:
                    try:
                        client.head_bucket(Bucket=self.config.bucket)
                        break
                    except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
                        try:
                            client.create_bucket(Bucket=self.config.bucket)
                            break
                        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
                            if time.monotonic() >= deadline:
                                raise PilotError("object_store_not_ready") from None
                            time.sleep(0.1)
        except PilotError:
            raise
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("storage_start_failed") from None

    def request_application_stop(self, timeout: int = 40) -> None:
        """锁内取精确 supervisor 身份，锁外 TERM；Task 4 用 OwnedProcess 收口后发布 stopped。"""
        if type(timeout) is not int or timeout <= 0:
            raise PilotError("pilot_input_invalid")
        with exclusive_profile_lock(self.path):
            self.reload()
            self.verify_all()
            state = self.runtime_state()
            if state.supervisor is None or not state.supervisor.live():
                self.require_no_processes()
                return
            supervisor = state.supervisor
            if supervisor.pid == os.getpid():
                raise PilotError("supervisor_self_stop_rejected")
            if supervisor.live():
                try:
                    os.kill(supervisor.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + timeout
        while supervisor.live():
            if time.monotonic() >= deadline:
                raise PilotError("application_stop_timeout")
            time.sleep(0.1)
        with exclusive_profile_lock(self.path):
            self.reload()
            if self.runtime_state().status == "failed":
                raise PilotError("application_stop_failed")

    def stop(self, *, timeout: int = 40) -> None:
        self.request_application_stop(timeout=timeout)
        with exclusive_profile_lock(self.path):
            self.reload()
            self.stop_storage_locked()

    def stop_storage_locked(self) -> None:
        """全部资源预核验后停止，绝不删除卷。应用必须已静止。"""
        self.require_no_processes()
        containers = self.verify_all()
        try:
            for container in reversed(list(containers.values())):
                if container.status == "running":
                    container.stop(timeout=30)
                container.reload()
                if container.status not in {"exited", "created"}:
                    raise PilotError("storage_stop_failed")
            self.publish_processes_locked(
                supervisor=None, processes=(), status="stopped", reason="requested_stop"
            )
        except PilotError:
            raise
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("storage_stop_failed") from None

    def require_stopped(self) -> None:
        self.require_no_processes()
        if any(
            container.status not in {"exited", "created"}
            for container in self.verify_all().values()
        ):
            raise PilotError("profile_not_stopped")

    def status(self) -> dict[str, object]:
        with exclusive_profile_lock(self.path):
            self.reload()
            containers = self.verify_all()
            states = {container.status for container in containers.values()}
            runtime = self.runtime_state()
            identities: list[ProcessIdentity] = []
            if runtime.supervisor:
                identities.append(runtime.supervisor)
            for record in runtime.processes:
                identities.extend([record, record.anchor, *record.children])
            live = [identity.live() for identity in identities]
            applications = (
                runtime.status if any(live) or runtime.status == "failed" else "stopped"
            )
            if runtime.status == "running" and any(live) and not all(live):
                applications = "failed"
            return {
                "storage": "stopped"
                if states <= {"created", "exited"}
                else "running"
                if states == {"running"}
                else "mixed",
                "applications": applications,
                "web_url": f"http://127.0.0.1:{self.config.api_port}",
            }

    @contextmanager
    def object_client(self) -> Iterator[Any]:
        """只访问已绑定的本机对象存储，原始客户端不得向 Agent 返回。"""
        client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{self.config.object_port}",
            aws_access_key_id=self.config.resolve("PILOT_OBJECT_ACCESS"),
            aws_secret_access_key=self.config.resolve("PILOT_OBJECT_SECRET"),
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
        try:
            yield client
        finally:
            client.close()

    def migrate(self) -> None:
        """显式 migrate 才升级；OwnedProcess 避免中断后留下失控迁移进程。"""
        with exclusive_profile_lock(self.path):
            self.reload()
            self.start_storage_locked()
            process = OwnedProcess.start(
                "migration",
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=ROOT,
                environ={
                    "PATH": os.defpath,
                    "PYTHONPATH": str(ROOT),
                    "PYTHON_DOTENV_DISABLED": "1",
                    "DATABASE_URL": self.config.database_url.get_secret_value(),
                },
            )
            try:
                self.publish_processes_locked(
                    supervisor=ProcessIdentity.current(),
                    processes=[process],
                    status="starting",
                    reason="storage_ready",
                )
                try:
                    result = process.process.wait(timeout=120)
                except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
                    raise PilotError("migration_failed") from None
                if result != 0:
                    raise PilotError("migration_failed")
            finally:
                try:
                    process.stop()
                except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
                    raise PilotError("migration_stop_failed") from None
                self.publish_processes_locked(
                    supervisor=None,
                    processes=(),
                    status="stopped",
                    reason="storage_ready",
                )
            self.check_schema_locked()

    def check_schema(self) -> None:
        with exclusive_profile_lock(self.path):
            self.reload()
            self.check_schema_locked()

    def check_schema_locked(self) -> None:
        """只比较 checkout 的单一 head，不执行任何迁移。"""
        self.verify_all()
        try:
            alembic = AlembicConfig(str(ROOT / "alembic.ini"))
            alembic.set_main_option("script_location", str(ROOT / "migrations"))
            alembic.set_main_option("path_separator", "os")
            head = ScriptDirectory.from_config(alembic).get_current_head()

            async def check() -> None:
                engine = create_engine_from(self.config.database_url.get_secret_value())
                try:
                    async with engine.connect() as connection:
                        versions = (
                            (
                                await connection.execute(
                                    text("SELECT version_num FROM alembic_version")
                                )
                            )
                            .scalars()
                            .all()
                        )
                    if versions != [head]:
                        raise PilotError("schema_not_current")
                finally:
                    await engine.dispose()

            asyncio.run(check())
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("schema_not_current") from None

    def revoke_restored_sessions_locked(self) -> None:
        """恢复专用：通过公开管理接口撤销原 tenant 会话，不改业务标识。"""

        async def revoke() -> None:
            engine = create_engine_from(self.config.database_url.get_secret_value())
            try:
                await PostgresAuthentication(
                    async_sessionmaker(engine, expire_on_commit=False),
                    TenantId(self.config.tenant_id),
                ).revoke_all()
            finally:
                await engine.dispose()

        try:
            asyncio.run(revoke())
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            raise PilotError("restore_session_revoke_failed") from None
