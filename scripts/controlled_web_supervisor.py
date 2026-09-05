"""仅监督基础设施、子进程、当前健康和停止；业务装配留给各进程。"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, cast
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

import boto3  # type: ignore[import-untyped]
import psutil  # type: ignore[import-untyped]
from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]
from pydantic import SecretStr
from sqlalchemy.engine import URL

from infra.controlled.config import (
    ControlledConfig,
    ControlledError,
    ControlledIdentity,
)
from infra.controlled.resources import OwnedContainers, OwnedProcess
from shared.schemas.identifiers import new_id


class Supervisor:
    """单次owner生命周期；状态只保存安全投影，失败也逐层清理。"""

    def __init__(
        self, root: Path, parent: Path | None, listeners: list[socket.socket]
    ) -> None:
        self.root = root
        self.owner = uuid4().hex
        self.directory = (parent or Path(tempfile.gettempdir())) / (
            "tradeos-controlled-" + self.owner
        )
        self.directory.mkdir(mode=0o700)
        self.listeners = listeners
        self.containers: OwnedContainers | None = None
        self.processes: list[OwnedProcess] = []
        self.config: ControlledConfig | None = None
        self.stopping = False
        self.restarting = False
        self.cleanup_errors: list[str] = []
        self.reason = "starting"
        self.status = "starting"
        self.node = shutil.which("node")
        self.environ = {
            "PATH": os.defpath,
            "PYTHONPATH": str(root),
            "PYTHON_DOTENV_DISABLED": "1",
            "LANG": "C.UTF-8",
            "HOME": str(self.directory),
            "AWS_EC2_METADATA_DISABLED": "true",
        }
        self.http = build_opener(ProxyHandler({}))

    def write_status(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason
        payload: dict[str, object] = {
            "updated_at": time.time(),
            "supervisor": {"pid": os.getpid(), "born": psutil.Process().create_time()},
            "owner": self.owner,
            "status": status,
            "reason": reason,
            "processes": [p.public() for p in self.processes],
            "container_ids": self.containers.ids if self.containers else [],
            "cleanup_errors": self.cleanup_errors,
        }
        if self.config:
            payload.update(
                tenant_id=self.config.tenant_id,
                identities=[i.model_dump() for i in self.config.identities],
                api_url=f"http://127.0.0.1:{self.config.api_port}",
                web_url=f"http://127.0.0.1:{self.config.web_port}",
                scheduler_url=f"http://127.0.0.1:{self.config.scheduler_port}",
            )
        target = self.directory / "status.json"
        pending = self.directory / "status.pending"
        pending.write_text(json.dumps(payload))
        pending.replace(target)

    def check_stop(self) -> None:
        if self.stopping:
            raise ControlledError("startup_interrupted")

    def start_infrastructure(self) -> None:
        self.write_status("starting", "infrastructure")
        if (
            self.node is None
            or subprocess.run(
                [self.node, "--version"], capture_output=True, check=False, timeout=5
            ).stdout.split(b".")[0]
            != b"v24"
        ):
            raise ControlledError("dependency_missing")
        if not (self.root / "apps/web/node_modules/vite/bin/vite.js").is_file():
            raise ControlledError("dependency_missing")
        self.containers = OwnedContainers(self.owner)
        database_passphrase = secrets.token_hex(24)
        object_access, object_secret = secrets.token_hex(16), secrets.token_hex(32)
        database_id, database_port = self.containers.create(
            "pgvector/pgvector:pg16",
            port=5432,
            environment={
                "POSTGRES_USER": "controlled",
                "POSTGRES_PASSWORD": database_passphrase,
                "POSTGRES_DB": "controlled",
            },
        )
        self.write_status("starting", "database_wait")
        deadline = time.monotonic() + 30
        while True:
            self.check_stop()
            if (
                self.containers.verify(database_id)
                .exec_run(["pg_isready", "-U", "controlled", "-d", "controlled"])
                .exit_code
                == 0
            ):
                break
            if time.monotonic() >= deadline:
                raise ControlledError("database_not_ready")
            time.sleep(0.1)
        _, object_port = self.containers.create(
            "minio/minio:RELEASE.2025-04-22T22-12-26Z",
            port=9000,
            environment={
                "MINIO_ROOT_USER": object_access,
                "MINIO_ROOT_PASSWORD": object_secret,
            },
            command=[
                "server",
                "/data",
                "--address",
                ":9000",
                "--console-address",
                "127.0.0.1:9001",
            ],
        )
        self.write_status("starting", "objects_wait")
        bucket = "controlled-" + self.owner
        client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{object_port}",
            aws_access_key_id=object_access,
            aws_secret_access_key=object_secret,
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
            deadline = time.monotonic() + 30
            while True:
                self.check_stop()
                try:
                    client.create_bucket(Bucket=bucket)
                    break
                except Exception:  # noqa: BLE001 安全边界只保留固定失败类别
                    if time.monotonic() >= deadline:
                        raise ControlledError("object_store_not_ready") from None
                    time.sleep(0.1)
        finally:
            client.close()
        identities = tuple(
            ControlledIdentity(
                employee_id=str(new_id("emp")),
                user_id=str(new_id("usr")),
                role=cast(
                    Literal["boss", "manager", "sales", "sourcing", "product"], role
                ),
                label=label,
            )
            for role, label in [
                ("boss", "演练老板甲（提案）"),
                ("boss", "演练老板乙（独立审批）"),
                ("manager", "演练经理"),
                ("sales", "演练销售"),
                ("sourcing", "演练寻源"),
                ("product", "演练产品"),
            ]
        )
        self.config = ControlledConfig(
            owner=self.owner,
            tenant_id=str(new_id("tn")),
            api_port=self.listeners[0].getsockname()[1],
            web_port=self.listeners[1].getsockname()[1],
            scheduler_port=self.listeners[2].getsockname()[1],
            database_port=database_port,
            object_port=object_port,
            database_url=SecretStr(
                URL.create(
                    "postgresql+asyncpg",
                    "controlled",
                    database_passphrase,
                    "127.0.0.1",
                    database_port,
                    "controlled",
                ).render_as_string(False)
            ),
            bucket=bucket,
            identities=identities,
            secrets={
                k: SecretStr(v)
                for k, v in {
                    "CONTROLLED_OBJECT_ACCESS": object_access,
                    "CONTROLLED_OBJECT_SECRET": object_secret,
                    "CONTROLLED_GMAIL": secrets.token_hex(32),
                    "CONTROLLED_FINGERPRINT": secrets.token_hex(32),
                    "CONTROLLED_UNSUBSCRIBE": secrets.token_hex(32),
                }.items()
            },
        )
        self.config.write(self.directory / "config.json")
        self.write_status("starting", "migration")
        self.containers.verify(database_id)
        self.run_once(
            "migration",
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            {
                **self.environ,
                "DATABASE_URL": self.config.database_url.get_secret_value(),
            },
        )
        self.write_status("starting", "identities")
        self.run_once(
            "identities",
            [
                sys.executable,
                "-m",
                "apps.api.controlled",
                str(self.directory / "config.json"),
                "initialize-identities",
            ],
            self.environ,
        )

    def run_once(
        self, name: str, command: list[str], environment: dict[str, str]
    ) -> None:
        process = OwnedProcess.start(name, command, cwd=self.root, environ=environment)
        self.processes.append(process)
        self.write_status("starting", name)
        deadline = time.monotonic() + 60
        while process.process.poll() is None:
            self.check_stop()
            if time.monotonic() >= deadline:
                raise ControlledError(name + "_timeout")
            time.sleep(0.1)
        if process.process.returncode != 0:
            raise ControlledError(name + "_failed")
        process.stop()
        self.processes.remove(process)

    def start_apps(self) -> None:
        config = self.config
        if config is None or self.node is None:
            raise ControlledError("configuration_invalid")
        self.write_status("starting", "applications")
        path = str(self.directory / "config.json")
        api_socket = self.listeners[0]
        self.processes.append(
            OwnedProcess.start(
                "api",
                [
                    sys.executable,
                    "-m",
                    "apps.api.controlled",
                    path,
                    str(api_socket.fileno()),
                ],
                cwd=self.root,
                environ=self.environ,
                pass_fds=(api_socket.fileno(),),
            )
        )
        self.listeners[2].close()
        self.processes.append(
            OwnedProcess.start(
                "scheduler",
                [sys.executable, "-m", "apps.scheduler_worker.controlled", path],
                cwd=self.root,
                environ=self.environ,
            )
        )
        public = {
            "owner": config.owner,
            "tenantId": config.tenant_id,
            "identities": [
                {"employeeId": i.employee_id, "label": i.label}
                for i in config.identities
            ],
            "schedulerUrl": f"http://127.0.0.1:{config.scheduler_port}",
        }
        # Vite不读取项目.env；受控配置仅显式注入dev值，未知主机与代理均不启用。
        vite_config = self.root / "apps/web/vite.controlled.config.ts"
        web_environment = {
            **self.environ,
            "CONTROLLED_STATUS_PATH": str(self.directory / "status.json"),
            "VITE_API_BASE_URL": f"http://127.0.0.1:{config.api_port}",
            "VITE_CONTROLLED_CONFIG": json.dumps(public),
            "VITE_TENANT_ID": config.tenant_id,
            "VITE_EMPLOYEE_ID": config.identities[0].employee_id,
        }
        self.listeners[1].close()
        self.processes.append(
            OwnedProcess.start(
                "web",
                [
                    self.node,
                    str(self.root / "apps/web/node_modules/vite/bin/vite.js"),
                    "--config",
                    str(vite_config),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(config.web_port),
                    "--strictPort",
                ],
                cwd=self.root / "apps/web",
                environ=web_environment,
            )
        )
        self.write_status("starting", "applications_health")
        deadline = time.monotonic() + 60
        while True:
            self.check_stop()
            self.assert_alive()
            if self.healthy():
                self.write_status("ready", "controlled_configuration_required")
                return
            if time.monotonic() >= deadline:
                raise ControlledError("health_timeout")
            time.sleep(0.2)

    def assert_alive(self) -> None:
        if any(p.process.poll() is not None for p in self.processes):
            raise ControlledError("child_exited")

    def healthy(self) -> bool:
        config = self.config
        if config is None:
            return False
        for port, path in (
            (config.api_port, "/health/ready"),
            (config.scheduler_port, "/health/ready"),
            (config.web_port, "/"),
        ):
            try:
                with self.http.open(
                    Request(
                        f"http://127.0.0.1:{port}{path}",
                        headers={
                            "X-Tenant-Id": config.tenant_id,
                            "X-Employee-Id": config.identities[0].employee_id,
                        },
                    ),
                    timeout=1,
                ) as response:
                    if response.status != 200:
                        return False
            except Exception:  # noqa: BLE001 安全边界只保留固定失败类别
                return False
        return True

    def stop_apps(self) -> list[str]:
        errors: list[str] = []
        for process in reversed(self.processes):
            try:
                process.stop()
            except Exception as exc:  # noqa: BLE001 安全进程边界不得回显异常
                errors.append(
                    exc.reason
                    if isinstance(exc, ControlledError)
                    else "process_cleanup_unknown"
                )
        return errors

    def restart(self) -> None:
        self.write_status("restarting", "applications")
        errors = self.stop_apps()
        if errors:
            self.cleanup_errors.extend(errors)
            raise ControlledError("restart_cleanup_failed")
        self.processes.clear()
        from run_web_core_controlled import reserve

        if self.config is None:
            raise ControlledError("configuration_invalid")
        self.listeners[1] = reserve(self.config.web_port)
        self.listeners[2] = reserve(self.config.scheduler_port)
        self.start_apps()

    def close(self) -> None:
        self.cleanup_errors.extend(self.stop_apps())
        if self.containers:
            try:
                self.cleanup_errors.extend(self.containers.close())
            except BaseException:  # noqa: BLE001 基础设施失败也必须继续清理文件
                self.cleanup_errors.append("container_cleanup_unknown")
        for listener in self.listeners:
            try:
                listener.close()
            except OSError:
                self.cleanup_errors.append("listener_cleanup_unknown")
        for name in (
            "config.json",
            "mail.sqlite",
            "mail.sqlite-journal",
            "mail.sqlite-wal",
            "mail.sqlite-shm",
        ):
            try:
                (self.directory / name).unlink(missing_ok=True)
            except OSError:
                self.cleanup_errors.append("private_file_cleanup_unknown")


def run(root: Path, parent: Path | None, listeners: list[socket.socket]) -> int:
    supervisor = Supervisor(root, parent, listeners)
    previous: dict[signal.Signals, Any] = {}

    def stop(_sig: int, _frame: object) -> None:
        supervisor.stopping = True

    def restart(_sig: int, _frame: object) -> None:
        supervisor.restarting = True

    for sig, handler in (
        (signal.SIGTERM, stop),
        (signal.SIGINT, stop),
        (signal.SIGHUP, restart),
    ):
        previous[sig] = signal.signal(sig, handler)
    primary: str | None = None
    try:
        supervisor.start_infrastructure()
        supervisor.start_apps()
        if supervisor.config is None:
            raise ControlledError("configuration_invalid")
        print(
            json.dumps(
                {
                    "status": "ready",
                    "directory": str(supervisor.directory),
                    "web_url": f"http://127.0.0.1:{supervisor.config.web_port}",
                }
            ),
            flush=True,
        )
        while not supervisor.stopping:
            supervisor.assert_alive()
            if supervisor.restarting:
                supervisor.restarting = False
                supervisor.restart()
            supervisor.write_status(
                "ready" if supervisor.healthy() else "not_ready",
                "controlled_configuration_required",
            )
            time.sleep(0.2)
    except BaseException as exc:  # noqa: BLE001 保留主失败同时完成清理
        primary = (
            exc.reason
            if isinstance(exc, ControlledError)
            else supervisor.reason + "_failed"
        )
    finally:
        try:
            supervisor.write_status("stopping", primary or "requested_stop")
        except BaseException:  # noqa: BLE001 状态文件失败也不能阻止释放资源
            supervisor.cleanup_errors.append("status_write_failed")
        try:
            supervisor.close()
        except BaseException:  # noqa: BLE001 进程边界固定失败，不能回显底层异常
            supervisor.cleanup_errors.append("cleanup_unknown")
        for restore_sig, restore_handler in previous.items():
            signal.signal(restore_sig, restore_handler)
        failed = primary is not None or bool(supervisor.cleanup_errors)
        try:
            supervisor.write_status(
                "failed" if failed else "stopped",
                primary or ("cleanup_unknown" if failed else "requested_stop"),
            )
        except BaseException:  # noqa: BLE001 固定输出仍报告未知状态
            failed = True
            supervisor.status = "failed"
            supervisor.reason = primary or "status_write_failed"
            supervisor.cleanup_errors.append("status_write_failed")
        print(
            json.dumps(
                {
                    "status": supervisor.status,
                    "reason": supervisor.reason,
                    "directory": str(supervisor.directory),
                    "cleanup_errors": supervisor.cleanup_errors,
                }
            ),
            flush=True,
        )
    return 2 if failed else 0
