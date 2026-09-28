"""本 profile 三应用生命周期；CLI 与业务装配分离，停止不删除持久数据。"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.request import ProxyHandler, Request, build_opener

import psutil  # type: ignore[import-untyped]

from infra.controlled.resources import OwnedProcess
from infra.pilot.config import PilotError, exclusive_profile_lock
from infra.pilot.resources import PilotProfile, ProcessIdentity, reserve_port

ROOT = Path(__file__).resolve().parents[1]
START_FAILURES = frozenset(
    {
        "web_build_missing",
        "profile_busy",
        "profile_apps_running",
        "port_unavailable",
        "schema_not_current",
        "configuration_invalid",
        "restore_incomplete",
        "application_start_failed",
        "application_ready_timeout",
        "application_start_interrupted",
    }
)


class PilotSupervisor:
    """仅核验当前 owner；操作级锁内发布真实进程，寿命期间不占锁。"""

    def __init__(self, path: Path, *, root: Path = ROOT) -> None:
        self.root = root
        self.profile = PilotProfile(path)
        self.processes: list[OwnedProcess] = []
        self.stopping = False
        self.owned = False
        self.http = build_opener(ProxyHandler({}))

    def publish(
        self,
        status: Literal["starting", "running", "failed"],
        reason: Literal["storage_ready", "applications_ready", "operation_failed"],
    ) -> None:
        """调用者持锁，状态只包含精确进程身份及固定诊断。"""
        self.profile.publish_processes_locked(
            supervisor=ProcessIdentity.current(),
            processes=self.processes,
            status=status,
            reason=reason,
        )

    def ready(self) -> bool:
        """HTTP ready 必须来自三个仍然存活且出生时间匹配的进程。"""
        config = self.profile.config
        urls = (
            f"http://127.0.0.1:{config.api_port}/api/health/ready",
            f"http://127.0.0.1:{config.scheduler_port}/health/ready",
            f"http://127.0.0.1:{config.notification_port}/health/ready",
        )
        if len(self.processes) != 3 or any(
            p.process.poll() is not None or not p.verified() for p in self.processes
        ):
            return False
        try:
            for url in urls:
                with self.http.open(Request(url), timeout=2) as response:
                    if (
                        response.status != 200
                        or json.load(response).get("status") != "ready"
                    ):
                        return False
            return True
        except Exception:  # noqa: BLE001 不输出 HTTP/连接材料
            return False

    def start(self) -> None:
        """build/固定端口/schema 先检查；三个真实 ready 后才发布成功。"""
        if not (self.root / "apps/web/dist/index.html").is_file():
            raise PilotError("web_build_missing")
        with exclusive_profile_lock(self.profile.path):
            self.profile.reload()
            self.profile.require_no_processes()
            config = self.profile.config
            with reserve_port(config.api_port) as api_listener:
                for port in (config.scheduler_port, config.notification_port):
                    with reserve_port(port):
                        pass
                self.owned = True
                self.profile.start_storage_locked()
                self.profile.check_schema_locked()
                self.publish("starting", "storage_ready")
                environment = {
                    "PATH": os.defpath,
                    "PYTHONPATH": str(self.root),
                    "PYTHON_DOTENV_DISABLED": "1",
                    "AWS_EC2_METADATA_DISABLED": "true",
                    "LANG": "C.UTF-8",
                }
                for name, module in (
                    ("api", "apps.api.pilot"),
                    ("scheduler", "apps.scheduler_worker.pilot"),
                    ("notification", "apps.notification_worker.pilot"),
                ):
                    if self.stopping:
                        raise PilotError("application_start_interrupted")
                    command = [
                        sys.executable,
                        "-m",
                        module,
                        str(self.profile.path / "config.json"),
                    ]
                    if name == "api":
                        command.append(str(api_listener.fileno()))
                    self.processes.append(
                        OwnedProcess.start(
                            name,
                            command,
                            cwd=self.root,
                            environ=environment,
                            pass_fds=(api_listener.fileno(),) if name == "api" else (),
                        )
                    )
                    self.publish("starting", "storage_ready")
                deadline = time.monotonic() + 60
                while not self.ready():
                    if self.stopping or any(
                        p.process.poll() is not None for p in self.processes
                    ):
                        raise PilotError("application_start_failed")
                    if time.monotonic() >= deadline:
                        raise PilotError("application_ready_timeout")
                    time.sleep(0.1)
                self.publish("running", "applications_ready")

    def close(self, *, failed: bool = False) -> None:
        """先精确停止再清记录；保留故障终态，只有请求停机标记 requested_stop。"""
        try:
            if not self.owned:
                return
            cleanup_failed = False
            for process in reversed(self.processes):
                try:
                    process.stop(timeout=10)
                except BaseException:  # noqa: BLE001 独立回收其余 owned 子进程
                    cleanup_failed = True
            with exclusive_profile_lock(self.profile.path):
                self.profile.reload()
                if cleanup_failed:
                    self.publish("failed", "operation_failed")
                    raise PilotError("application_stop_failed")
                self.profile.publish_processes_locked(
                    supervisor=None,
                    processes=(),
                    status="stopped",
                    reason="requested_stop",
                )
                try:
                    self.profile.stop_storage_locked()
                except BaseException:
                    self.profile.publish_processes_locked(
                        supervisor=None,
                        processes=(),
                        status="failed",
                        reason="operation_failed",
                    )
                    raise
                if failed:
                    self.profile.publish_processes_locked(
                        supervisor=None,
                        processes=(),
                        status="failed",
                        reason="operation_failed",
                    )
            self.owned = False
        finally:
            self.profile.client.close()


def reap_in_background(process: subprocess.Popen[bytes]) -> threading.Thread:
    """持有并回收后台 supervisor，启动调用者不等待服务寿命结束。"""
    worker = threading.Thread(
        target=process.wait, name="tradeos-pilot-supervisor-reaper", daemon=True
    )
    worker.start()
    return worker


def launch(path: Path) -> None:
    """子 supervisor 握手：收到真实 ready 才返回，失败不伪装部分启动成功。"""
    read_fd, write_fd = os.pipe()
    process = None
    identity = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.pilot_web_supervisor",
                str(path),
                str(write_fd),
            ],
            cwd=ROOT,
            env={
                "PATH": os.defpath,
                "PYTHONPATH": str(ROOT),
                "PYTHON_DOTENV_DISABLED": "1",
                "AWS_EC2_METADATA_DISABLED": "true",
            },
            pass_fds=(write_fd,),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        identity = ProcessIdentity(
            pid=process.pid, born=psutil.Process(process.pid).create_time()
        )
        os.close(write_fd)
        write_fd = -1
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            readable, _, _ = select.select([read_fd], [], [], 0.2)
            if readable:
                message = os.read(read_fd, 64).decode("ascii", errors="ignore")
                if message == "ready":
                    reap_in_background(process)
                    return
                raise PilotError(
                    message if message in START_FAILURES else "application_start_failed"
                )
            if process.poll() is not None:
                raise PilotError("application_start_failed")
        raise PilotError("application_start_timeout")
    except BaseException:
        if identity is not None and identity.live():
            os.kill(identity.pid, signal.SIGTERM)
        if process is not None:
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                raise PilotError("application_stop_timeout") from None
        raise
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def main() -> int:
    """信号只发停止意图，清理由当前 supervisor 的 OwnedProcess 对象完成。"""
    supervisor = None
    result = 2
    failure = "application_start_failed"
    ready_fd = int(sys.argv[2])
    try:
        supervisor = PilotSupervisor(Path(sys.argv[1]))

        def stop(signum: int, frame: object) -> None:
            supervisor.stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        supervisor.start()
        os.write(ready_fd, b"ready")
        os.close(ready_fd)
        ready_fd = -1
        result = 0
        while not supervisor.stopping:
            if any(
                p.process.poll() is not None or not p.verified()
                for p in supervisor.processes
            ):
                result = 2
                break
            time.sleep(0.2)
    except BaseException as error:  # noqa: BLE001 只传递固定 allowlist，禁止异常原文
        if isinstance(error, PilotError) and error.reason in START_FAILURES:
            failure = error.reason
        result = 2
    finally:
        if supervisor is not None:
            try:
                supervisor.close(failed=result != 0 and not supervisor.stopping)
            except BaseException:  # noqa: BLE001 清理失败不得宣称成功
                result = 2
        if ready_fd >= 0:
            try:
                os.write(ready_fd, failure.encode("ascii"))
            except OSError:
                pass
            finally:
                os.close(ready_fd)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
