"""专用本人邮箱的显式初始化、迁移与本机受管入口。"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import logging
import os
import plistlib
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Never
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import docker  # type: ignore[import-untyped]
from sqlalchemy import text

from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.pilot.config import (
    PilotError,
    checked_directory,
    exclusive_profile_lock,
    private_read,
)
from infra.pilot.mailbox_config import MailboxConfig, MailboxStorageIdentity
from infra.pilot.resources import ProcessIdentity

OWNER = "tradeos.mailbox.owner"
ROLE = "tradeos.mailbox.role"
IMAGE = "pgvector/pgvector:pg16"
MOUNT = "/var/lib/postgresql/data"


def child_environment() -> dict[str, str]:
    """只给显式子进程最小环境，不继承 .env 或 Provider 配置。"""
    return {"PATH": os.defpath, "PYTHONPATH": str(ROOT), "PYTHON_DOTENV_DISABLED": "1"}


def supervisor_directory(path: Path) -> Path:
    directory = path.parent / "supervisor"
    directory.mkdir(mode=0o700, exist_ok=True)
    checked_directory(directory)
    return directory


class MailboxDatabase:
    """只操作配置记录的精确本机数据库容器与独占卷。"""

    def __init__(self, path: Path, *, client: Any = None) -> None:
        self.path = path
        self.config = MailboxConfig.read(path)
        self.client = (
            client
            if client is not None
            else docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=10)
        )

    def close(self) -> None:
        self.client.close()

    def verify(self) -> Any:
        """同时验证 ID、镜像、归属、端口及卷身份，拒绝复用陌生资源。"""
        self.config = MailboxConfig.read(self.path)
        return self._verify_resource(self.config, self.config.storage)

    def _verify_resource(
        self, config: MailboxConfig, identity: MailboxStorageIdentity | None
    ) -> Any:
        """初始化恢复候选与常规启动使用完全相同的严格归属检查。"""
        try:
            if (
                identity is None
                or not identity.container_id
                or not identity.volume_created_at
            ):
                raise ValueError()
            container = self.client.containers.get(identity.container_id)
            container.reload()
            if (
                container.id != identity.container_id
                or container.labels.get(OWNER) != config.owner_id
                or container.labels.get(ROLE) != "database"
                or container.attrs["Image"] != identity.image_id
                or container.attrs["HostConfig"]["PortBindings"]
                != {
                    "5432/tcp": [
                        {"HostIp": "127.0.0.1", "HostPort": str(config.db_port)}
                    ]
                }
            ):
                raise ValueError()
            volume = self.client.volumes.get(identity.volume_name)
            volume.reload()
            if (
                volume.attrs["Name"] != identity.volume_name
                or volume.attrs["CreatedAt"] != identity.volume_created_at
                or volume.attrs["Labels"].get(OWNER) != config.owner_id
                or volume.attrs["Labels"].get(ROLE) != "database"
            ):
                raise ValueError()
            mounts = container.attrs["Mounts"]
            if len(mounts) != 1 or any(
                (
                    mounts[0]["Type"] != "volume",
                    mounts[0]["Name"] != identity.volume_name,
                    mounts[0]["Destination"] != MOUNT,
                    mounts[0]["RW"] is not True,
                )
            ):
                raise ValueError()
            attached = self.client.containers.list(
                all=True, filters={"volume": identity.volume_name}
            )
            if {item.id for item in attached} != {identity.container_id}:
                raise ValueError()
            return container
        except Exception:  # noqa: BLE001 Docker 元数据与异常不向外输出
            raise PilotError("resource_identity_invalid") from None

    def provision(self) -> None:
        """显式 init 可接续本 owner 的未完成创建；绝不删卷或采用陌生资源。"""
        with exclusive_profile_lock(self.path.parent):
            config = MailboxConfig.read(self.path)
            name = f"tradeos-mailbox-{config.owner_id}-database"
            identity = config.storage
            if identity is not None and identity.container_id:
                self.verify()
                return
            if identity is None:
                image = self.client.images.get(IMAGE)
                # 首次计划尚未落盘时绝不接管同名现有卷。
                try:
                    self.client.volumes.get(name)
                except docker.errors.NotFound:
                    pass
                else:
                    raise PilotError("resource_identity_invalid")
                identity = MailboxStorageIdentity(image_id=image.id, volume_name=name)
                config = config.model_copy(update={"storage": identity})
                config.write(self.path)
            else:
                # 恢复始终使用已固定的镜像 ID，不追随可变化的 tag。
                self.client.images.get(identity.image_id)
            labels = {OWNER: config.owner_id, ROLE: "database"}
            try:
                volume = self.client.volumes.get(name)
            except docker.errors.NotFound:
                if identity.volume_created_at:
                    raise PilotError("resource_identity_invalid") from None
                volume = self.client.volumes.create(name=name, labels=labels)
            volume.reload()
            if (
                volume.attrs["Name"] != name
                or volume.attrs["Labels"].get(OWNER) != config.owner_id
                or volume.attrs["Labels"].get(ROLE) != "database"
                or not volume.attrs["CreatedAt"]
                or (
                    identity.volume_created_at
                    and volume.attrs["CreatedAt"] != identity.volume_created_at
                )
            ):
                raise PilotError("resource_identity_invalid")
            try:
                container = self.client.containers.get(name)
            except docker.errors.NotFound:
                container = None
            attached = self.client.containers.list(all=True, filters={"volume": name})
            if container is None and attached:
                raise PilotError("resource_identity_invalid")
            if not identity.volume_created_at:
                identity = identity.model_copy(
                    update={"volume_created_at": volume.attrs["CreatedAt"]}
                )
                config = config.model_copy(update={"storage": identity})
                config.write(self.path)
            if container is None:
                container = self.client.containers.create(
                    identity.image_id,
                    name=name,
                    labels=labels,
                    environment={
                        "POSTGRES_USER": "mailbox",
                        "POSTGRES_DB": "mailbox",
                        "POSTGRES_PASSWORD": config.db_password.get_secret_value(),
                    },
                    volumes={name: {"bind": MOUNT, "mode": "rw"}},
                    ports={"5432/tcp": ("127.0.0.1", config.db_port)},
                    restart_policy={"Name": "no"},
                )
            container.reload()
            if container.name != name:
                raise PilotError("resource_identity_invalid")
            recovered = identity.model_copy(update={"container_id": container.id})
            self._verify_resource(config, recovered)
            config.model_copy(update={"storage": recovered}).write(self.path)
            self.verify()

    def start(self) -> None:
        with exclusive_profile_lock(self.path.parent):
            container = self.verify()
            if container.status != "running":
                container.start()

    def stop(self) -> None:
        """停止保留容器与卷，绝不删除数据。"""
        with (
            exclusive_profile_lock(self.path.parent),
            exclusive_profile_lock(supervisor_directory(self.path)),
        ):
            container = self.verify()
            if container.status == "running":
                container.stop(timeout=30)

    async def wait_ready(self) -> None:
        engine = create_engine_from(self.config.database_url.get_secret_value())
        try:
            for _ in range(30):
                try:
                    async with asyncio.timeout(2), engine.connect() as connection:
                        await connection.execute(text("SELECT 1"))
                    return
                except Exception:  # noqa: BLE001 有界等待不输出连接信息
                    await asyncio.sleep(1)
            raise PilotError("database_unavailable")
        finally:
            await engine.dispose()

    async def migrate(self) -> None:
        """仅显式 init/migrate 调用，应用启动永不迁移。"""
        with (
            exclusive_profile_lock(self.path.parent),
            exclusive_profile_lock(supervisor_directory(self.path)),
        ):
            container = self.verify()
            if container.status != "running":
                container.start()
            await self.wait_ready()
            engine = create_engine_from(self.config.database_url.get_secret_value())
            try:
                async with engine.connect() as connection:
                    locked = await connection.scalar(
                        text("SELECT pg_try_advisory_lock(74403)")
                    )
                    if not locked:
                        raise PilotError("migration_busy")
                    process = await asyncio.create_subprocess_exec(
                        sys.executable,
                        str(ROOT / "scripts/run_alembic.py"),
                        "upgrade",
                        "head",
                        cwd=ROOT,
                        env={
                            **child_environment(),
                            "DATABASE_URL": self.config.database_url.get_secret_value(),
                        },
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    try:
                        code = await asyncio.wait_for(process.wait(), timeout=180)
                    except BaseException:  # noqa: BLE001 取消也须终止迁移子进程后释放锁
                        process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), timeout=10)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                        raise PilotError("migration_failed") from None
                    if code:
                        raise PilotError("migration_failed")
                    await assert_database_schema_current(engine)
                    await connection.execute(text("SELECT pg_advisory_unlock(74403)"))
            finally:
                await engine.dispose()


def supervisor_identity(path: Path, config: MailboxConfig) -> ProcessIdentity | None:
    target = path.parent / "supervisor.json"
    if not target.exists():
        return None
    try:
        data = json.loads(private_read(target))
        if data["owner_id"] != config.owner_id:
            raise ValueError()
        identity = ProcessIdentity.model_validate(data["identity"])
        return identity if identity.live() else None
    except Exception:  # noqa: BLE001 不得向不明 PID 发送信号
        raise PilotError("process_identity_invalid") from None


def stop_supervisor(path: Path) -> None:
    identity = supervisor_identity(path, MailboxConfig.read(path))
    if identity is None:
        return
    os.kill(identity.pid, signal.SIGTERM)
    deadline = time.monotonic() + 150
    while identity.live():
        if time.monotonic() >= deadline:
            raise PilotError("supervisor_stop_pending")
        time.sleep(0.1)


def launch_agent_payload(
    config: MailboxConfig, path: Path, web_build: Path
) -> dict[str, object]:
    return {
        "Label": "com.tradeos.mailbox." + config.owner_id,
        "ProgramArguments": [
            sys.executable,
            str(ROOT / "scripts/mailbox_supervisor.py"),
            "--profile",
            str(path),
            "--web-build",
            str(web_build),
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def launch_agent_pid(label: str) -> int | None:
    """只投影 launchd 的 PID，环境与参数输出留在本进程且不记录。"""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
        match = re.search(r"^\s*pid = ([0-9]+)$", result.stdout, re.MULTILINE)
        return int(match[1]) if result.returncode == 0 and match else None
    except Exception:  # noqa: BLE001 launchd 原始信息不得向外输出
        return None


def mailbox_http_ready(origin: str) -> bool:
    """仅访问配置中的 loopback 健康入口，不读取邮箱或继承代理环境。"""
    target = urlsplit(origin)
    if target.scheme != "http" or target.hostname != "127.0.0.1" or target.port is None:
        return False
    connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=1)
    try:
        connection.request("GET", "/api/health/ready")
        response = connection.getresponse()
        if (
            response.status != 200
            or response.getheader("Content-Type", "").split(";", 1)[0]
            != "application/json"
        ):
            return False
        value = json.loads(response.read(4097))
        return isinstance(value, dict) and value.get("status") == "ready"
    except Exception:  # noqa: BLE001 本地等待不回显网络异常
        return False
    finally:
        connection.close()


def wait_for_launch_agent(path: Path, config: MailboxConfig) -> None:
    """注册不等于启动；必须由实际 launchd PID 写状态并通过 API 就绪检查。"""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        identity = supervisor_identity(path, config)
        if (
            identity is not None
            and identity.pid
            == launch_agent_pid("com.tradeos.mailbox." + config.owner_id)
            and mailbox_http_ready(config.origin)
        ):
            return
        time.sleep(0.5)
    # 保留已经注册的任务，操作者可以查看状态或显式停止，不谎报正常运行。
    raise PilotError("launch_agent_start_pending")


def install_agent(path: Path, web_build: Path) -> None:
    if sys.platform != "darwin":
        raise PilotError("launch_agent_unsupported")
    config = MailboxConfig.read(path)
    payload = launch_agent_payload(config, path, web_build)
    directory = Path.home() / "Library/LaunchAgents"
    if any(parent.is_symlink() for parent in (directory, *directory.parents)):
        raise PilotError("configuration_invalid")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / (str(payload["Label"]) + ".plist")
    encoded = plistlib.dumps(payload)
    if target.exists() or target.is_symlink():
        if (
            target.is_symlink()
            or target.stat().st_uid != os.getuid()
            or target.read_bytes() != encoded
        ):
            raise PilotError("launch_agent_conflict")
    else:
        descriptor = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    stop_supervisor(path)
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{payload['Label']}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode:
        raise PilotError("launch_agent_unavailable")
    wait_for_launch_agent(path, config)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise PilotError("mailbox_input_invalid")


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    database = None
    try:
        parser = SafeParser(allow_abbrev=False, description="本人邮箱专用持久服务")
        parser.add_argument(
            "command",
            choices=("init", "migrate", "start", "stop", "status", "install", "bind"),
        )
        parser.add_argument("--profile", type=Path, required=True)
        parser.add_argument("--web-build", type=Path, default=ROOT / "apps/web/dist")
        parser.add_argument("--employee-id")
        parser.add_argument("--email")
        parser.add_argument("--credentials-file", type=Path)
        args = parser.parse_args(argv)
        path = args.profile.absolute()
        if args.command == "init":
            if path.exists() or path.is_symlink():
                MailboxConfig.read(path)
            else:
                MailboxConfig.create(path)
            database = MailboxDatabase(path)
            database.provision()
            asyncio.run(database.migrate())
        elif args.command == "bind":
            if not all((args.employee_id, args.email, args.credentials_file)):
                raise PilotError("mailbox_input_invalid")
            MailboxConfig.bind(
                path,
                employee_id=args.employee_id,
                email=args.email,
                credentials_file=args.credentials_file,
            )
        elif args.command == "start":
            from scripts.mailbox_supervisor import launch

            launch(path, args.web_build.absolute())
        elif args.command == "install":
            install_agent(path, args.web_build.absolute())
        elif args.command == "stop":
            config = MailboxConfig.read(path)
            if sys.platform == "darwin":
                subprocess.run(
                    [
                        "launchctl",
                        "bootout",
                        f"gui/{os.getuid()}/com.tradeos.mailbox.{config.owner_id}",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            stop_supervisor(path)
            database = MailboxDatabase(path)
            database.stop()
        elif args.command == "migrate":
            database = MailboxDatabase(path)
            asyncio.run(database.migrate())
        elif args.command == "status":
            config = MailboxConfig.read(path)
            print(
                json.dumps(
                    {
                        "supervisor_status": "running"
                        if supervisor_identity(path, config)
                        else "stopped",
                        "api_status": "ready"
                        if mailbox_http_ready(config.origin)
                        else "unavailable",
                        "mailbox_sync": "not_checked",
                        "origin": config.origin,
                        "bound_mailboxes": len(config.bindings),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {"status": "completed", "command": args.command}, ensure_ascii=False
            )
        )
        return 0
    except PilotError as error:
        print(json.dumps({"status": "failed", "reason": error.reason}))
        return 2
    except Exception:  # noqa: BLE001 不输出底层原始异常
        print('{"status":"failed","reason":"mailbox_operation_failed"}')
        return 2
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
