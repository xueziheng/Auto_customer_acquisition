"""本人邮箱 API 与只读同步的受管生命周期；不自动迁移或创建账号。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from infra.controlled.resources import OwnedProcess
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.pilot.config import PilotError, exclusive_profile_lock, private_write
from infra.pilot.mailbox_config import MailboxConfig
from infra.pilot.resources import ProcessIdentity
from scripts.run_mailbox import (
    MailboxDatabase,
    SafeParser,
    child_environment,
    supervisor_directory,
    supervisor_identity,
)


def service_commands(
    config: MailboxConfig, path: Path, web_build: Path
) -> dict[str, list[str]]:
    """子进程参数只含显式配置路径和不透明绑定 ID，不含 OAuth 或邮箱地址。"""
    return {
        "api": [
            sys.executable,
            "-m",
            "apps.api.mailbox",
            "--profile",
            str(path),
            "--web-build",
            str(web_build),
        ],
        **{
            binding.binding_id: [
                sys.executable,
                "-m",
                "apps.email_feedback_worker.mailbox",
                "sync",
                "--mailbox-profile",
                str(path),
                "--binding-id",
                binding.binding_id,
                "--watch",
            ]
            for binding in config.bindings
        },
    }


class ChildServices:
    """锚点保护退出时收拢子进程；崩溃指数退避，不高速反复启动。"""

    def __init__(
        self,
        path: Path,
        web_build: Path,
        *,
        spawn: Callable[..., Any] = OwnedProcess.start,
    ) -> None:
        self.path, self.web_build, self.spawn = path, web_build, spawn
        self.children: dict[str, Any] = {}
        self.retry_at: dict[str, float] = {}
        self.delays: dict[str, int] = {}

    def reconcile(self, config: MailboxConfig, *, now: float) -> None:
        wanted = service_commands(config, self.path, self.web_build)
        for name, child in list(self.children.items()):
            if name not in wanted or child.process.poll() is not None:
                child.stop(timeout=120)
                del self.children[name]
                delay = min(300, self.delays.get(name, 15) * 2)
                self.delays[name] = delay
                self.retry_at[name] = now + delay
        for name, command in wanted.items():
            if name not in self.children and now >= self.retry_at.get(name, 0):
                self.children[name] = self.spawn(
                    name, command, cwd=ROOT, environ=child_environment()
                )

    def stop(self) -> None:
        failed = False
        for child in list(self.children.values()):
            try:
                child.stop(timeout=120)
            except Exception:  # noqa: BLE001 尽力关闭全部已知子进程后失败关闭
                failed = True
        self.children.clear()
        if failed:
            raise PilotError("process_stop_failed")


async def check_database(config: MailboxConfig) -> None:
    """启动只检查 schema，暂时断连不能触发自动迁移。"""
    engine = create_engine_from(config.database_url.get_secret_value())
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await assert_database_schema_current(engine)
    finally:
        await engine.dispose()


def run(path: Path, web_build: Path) -> None:
    config = MailboxConfig.read(path)
    if not (web_build / "index.html").is_file():
        raise PilotError("web_build_missing")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_args: stop.set())
    children = ChildServices(path, web_build)
    identity = ProcessIdentity.current()
    previous_status = None

    def publish(status: str) -> None:
        nonlocal previous_status
        if status == previous_status:
            return
        with exclusive_profile_lock(path.parent):
            private_write(
                path.parent / "supervisor.json",
                json.dumps(
                    {
                        "owner_id": config.owner_id,
                        "identity": identity.model_dump(),
                        "status": status,
                    }
                ).encode(),
            )
        previous_status = status

    with exclusive_profile_lock(supervisor_directory(path)):
        database = None
        try:
            publish("starting")
            while not stop.is_set():
                try:
                    if database is None:
                        database = MailboxDatabase(path)
                    database.start()
                    config = MailboxConfig.read(path)
                    asyncio.run(check_database(config))
                    children.reconcile(config, now=time.monotonic())
                    publish(
                        "services_started"
                        if config.bindings
                        else "waiting_mailbox_binding"
                    )
                    stop.wait(5)
                except Exception:  # noqa: BLE001 等待 Docker/DB 恢复，不回显凭证或反复日志
                    children.stop()
                    publish("waiting_database_or_service")
                    stop.wait(30)
        finally:
            children.stop()
            if database is not None:
                database.close()
            publish("stopped")


def launch(path: Path, web_build: Path) -> None:
    """显式命令启动脱离终端的 supervisor；不在 import 时执行。"""
    config = MailboxConfig.read(path)
    if supervisor_identity(path, config) is not None:
        return
    if not (web_build / "index.html").is_file():
        raise PilotError("web_build_missing")
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--profile",
            str(path),
            "--web-build",
            str(web_build),
        ],
        cwd=ROOT,
        env=child_environment(),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        if process.poll() is not None:
            raise PilotError("supervisor_start_failed")
        if supervisor_identity(path, config) is not None:
            # 长寿后台进程由 reaper 回收，CLI 返回后不阻塞解释器退出。
            threading.Thread(target=process.wait, daemon=True).start()
            return
        time.sleep(0.1)
    raise PilotError("supervisor_start_pending")


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    try:
        parser: argparse.ArgumentParser = SafeParser(allow_abbrev=False)
        parser.add_argument("--profile", type=Path, required=True)
        parser.add_argument("--web-build", type=Path, required=True)
        args = parser.parse_args(argv)
        run(args.profile.absolute(), args.web_build.absolute())
        return 0
    except Exception:  # noqa: BLE001 守护退出不输出底层异常
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
