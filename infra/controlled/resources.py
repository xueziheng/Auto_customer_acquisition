"""本owner容器与子进程的身份核验和有界清理。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker  # type: ignore[import-untyped]
import psutil  # type: ignore[import-untyped]

from .config import ControlledError

OWNER_LABEL = "tradeos.controlled.owner"
DOCKER_API_TIMEOUT_SECONDS = 30

ProcessIdentity = int | float


def process_identity(pid: int) -> ProcessIdentity:
    """Return a boot-clock-independent identity on Linux, failing closed on races."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # ``comm`` may contain spaces or parentheses.  Field 22 is the
            # process start tick, and fields after the final ')' begin at 3.
            fields = stat[stat.rfind(")") + 2 :].split()
            return int(fields[19])
        except (IndexError, OSError, ValueError):
            raise psutil.NoSuchProcess(pid) from None
    return psutil.Process(pid).create_time()


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen[bytes]
    born: ProcessIdentity
    anchor_pid: int
    anchor_born: ProcessIdentity
    control: socket.socket = field(repr=False)
    children: dict[int, ProcessIdentity] = field(default_factory=dict)
    closed: bool = False
    stop_requested: bool = False
    running_at_stop: bool = False

    @classmethod
    def start(
        cls,
        name: str,
        command: list[str],
        *,
        cwd: Path,
        environ: dict[str, str],
        pass_fds: tuple[int, ...] = (),
    ) -> OwnedProcess:
        control, inherited = socket.socketpair()
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("process_anchor.py")),
                    "bootstrap",
                    str(inherited.fileno()),
                    *command,
                ],
                cwd=cwd,
                env=environ,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(*pass_fds, inherited.fileno()),
            )
            inherited.close()
            control.settimeout(5)
            anchor_pid = int(control.recv(32))
            result = cls(
                name,
                process,
                process_identity(process.pid),
                anchor_pid,
                process_identity(anchor_pid),
                control,
            )
            if not result.verified() or not result._anchor_verified():
                raise ControlledError("process_owner_unknown")
            control.sendall(b"R")
            control.settimeout(None)
            return result
        except BaseException:  # noqa: BLE001 握手失败通过私有通道触发锚点有界清理
            inherited.close()
            control.close()
            if process is not None:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    pass
            raise ControlledError("process_handshake_failed") from None

    def verified(self) -> bool:
        try:
            return (
                process_identity(self.process.pid) == self.born
                and os.getpgid(self.process.pid) == self.process.pid
            )
        except ProcessLookupError:
            return False
        except psutil.NoSuchProcess:
            return False

    def stop(self, timeout: int = 20) -> None:
        """先TERM完成在途cycle；升级时保留锚点，只KILL已核验的业务进程。"""
        self.request_stop()
        self.finish_stop(timeout)

    def request_stop(self) -> None:
        """核验归属并发送TERM；允许监督器先通知全部进程并行收尾。"""
        if self.closed:
            return
        if self.stop_requested:
            return
        running = self.process.poll() is None
        if not self._anchor_verified() or (running and not self.verified()):
            raise ControlledError("process_owner_unknown")
        self._record_children()
        os.killpg(self.process.pid, signal.SIGTERM)
        self.running_at_stop = running
        self.stop_requested = True

    def finish_stop(self, timeout: float = 20) -> None:
        """等待已请求的停止，随后核验并回收子进程与身份锚点。"""
        if self.closed:
            return
        if not self.stop_requested:
            self.request_stop()
        deadline = time.monotonic() + timeout
        running = self.running_at_stop
        forced = False
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not self.verified():
                raise ControlledError("process_owner_unknown") from None
            self.process.kill()
            self.process.wait(timeout=5)
            forced = True
        forced = self._close_children(max(0, deadline - time.monotonic())) or forced
        if self._group_members():
            raise ControlledError("process_cleanup_unknown")
        self.control.sendall(b"Q")
        self.control.close()
        deadline = time.monotonic() + 5
        while self._anchor_verified():
            if time.monotonic() >= deadline:
                raise ControlledError("process_cleanup_unknown")
            time.sleep(0.02)
        self.closed = True
        if forced:
            raise ControlledError("process_forced_stop")
        if running and self.process.returncode not in {
            0,
            -signal.SIGTERM,
            128 + signal.SIGTERM,
        }:
            raise ControlledError("process_cleanup_failed")

    def _anchor_verified(self) -> bool:
        try:
            anchor = psutil.Process(self.anchor_pid)
            return (
                process_identity(self.anchor_pid) == self.anchor_born
                and anchor.status() != psutil.STATUS_ZOMBIE
                and os.getpgid(self.anchor_pid) == self.process.pid
                and os.getsid(self.anchor_pid) == self.process.pid
            )
        except (ProcessLookupError, psutil.NoSuchProcess):
            return False

    def _group_members(self) -> list[Any]:
        if not self._anchor_verified():
            raise ControlledError("process_owner_unknown")
        members = []
        for candidate in psutil.process_iter():
            try:
                if (
                    candidate.pid != self.anchor_pid
                    and os.getpgid(candidate.pid) == self.process.pid
                    and candidate.status() != psutil.STATUS_ZOMBIE
                ):
                    members.append(candidate)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
        if not self._anchor_verified():
            raise ControlledError("process_owner_unknown")
        return members

    def _record_children(self) -> None:
        for member in self._group_members():
            if member.pid != self.process.pid:
                self.children[member.pid] = process_identity(member.pid)
        if self.process.poll() is None and self.verified():
            try:
                for child in psutil.Process(self.process.pid).children(recursive=True):
                    if child.pid != self.anchor_pid:
                        self.children[child.pid] = process_identity(child.pid)
            except psutil.NoSuchProcess:
                pass

    def _close_children(self, timeout: float) -> bool:
        self._record_children()
        live: list[Any] = []
        for pid, born in self.children.items():
            try:
                child = psutil.Process(pid)
                if child.status() == psutil.STATUS_ZOMBIE:
                    continue
                if process_identity(pid) != born or os.getpgid(pid) != self.process.pid:
                    raise ControlledError("process_owner_unknown")
                child.terminate()
                live.append(child)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
        deadline = time.monotonic() + timeout
        while live and time.monotonic() < deadline:
            live = [
                child
                for child in live
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE
            ]
            if live:
                time.sleep(0.02)
        if live:
            for child in live:
                if (
                    process_identity(child.pid) != self.children[child.pid]
                    or os.getpgid(child.pid) != self.process.pid
                ):
                    raise ControlledError("process_owner_unknown")
                child.kill()
            deadline = time.monotonic() + 5
            while self._group_members() and time.monotonic() < deadline:
                time.sleep(0.02)
            return True
        return False

    def public(self) -> dict[str, object]:
        if not self.closed:
            self._record_children()
        return {
            "name": self.name,
            "pid": self.process.pid,
            "born": self.born,
            "exit": self.process.poll(),
            "anchor": {"pid": self.anchor_pid, "born": self.anchor_born},
            "children": [
                {"pid": pid, "born": born} for pid, born in self.children.items()
            ],
        }


class OwnedContainers:
    """固定本机Docker socket，不继承DOCKER_HOST，不拉取镜像。"""

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.client = self._new_client()
        self.ids: list[str] = []

    @staticmethod
    def _new_client() -> Any:
        return docker.DockerClient(
            base_url="unix:///var/run/docker.sock",
            timeout=DOCKER_API_TIMEOUT_SECONDS,
        )

    def create(
        self,
        image: str,
        *,
        port: int,
        environment: dict[str, str],
        command: list[str] | None = None,
    ) -> tuple[str, int]:
        self.client.images.get(image)
        container = self.client.containers.create(
            image,
            command=command,
            environment=environment,
            labels={OWNER_LABEL: self.owner},
            ports={f"{port}/tcp": ("127.0.0.1", None)},
        )
        self.ids.append(container.id)
        container.start()
        container.reload()
        bindings = container.attrs["NetworkSettings"]["Ports"][f"{port}/tcp"]
        if len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise ControlledError("container_binding_invalid")
        return container.id, int(bindings[0]["HostPort"])

    def verify(self, container_id: str) -> Any:
        return self._verify_with(self.client, container_id)

    def _verify_with(self, client: Any, container_id: str) -> Any:
        if container_id not in self.ids:
            raise ControlledError("container_owner_unknown")
        container = client.containers.get(container_id)
        if (
            container.id != container_id
            or container.labels.get(OWNER_LABEL) != self.owner
        ):
            raise ControlledError("container_owner_unknown")
        return container

    def close(self) -> list[str]:
        errors: list[str] = []
        cleanup_client: Any | None = None
        try:
            # Docker Desktop may leave a long-lived SDK connection stalled after
            # several create/exec calls.  Cleanup uses a fresh connection so an
            # uncertain create can still be inventoried by its owner label.
            cleanup_client = self._new_client()
            for container in cleanup_client.containers.list(
                all=True, filters={"label": f"{OWNER_LABEL}={self.owner}"}
            ):
                if (
                    container.labels.get(OWNER_LABEL) == self.owner
                    and container.id not in self.ids
                ):
                    self.ids.append(container.id)
        except Exception:  # noqa: BLE001 创建结果不确定时保守核对本owner
            errors.append("container_inventory_unknown")
        try:
            if cleanup_client is not None:
                cleanup_client.close()
        except Exception:  # noqa: BLE001 保留所有清理结果
            errors.append("container_client_cleanup_unknown")
        try:
            self.client.close()
        except Exception:  # noqa: BLE001 保留所有清理结果
            errors.append("container_client_cleanup_unknown")
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(self.ids)))) as pool:
            for result in pool.map(self._remove_owned, reversed(self.ids)):
                if result is not None:
                    errors.append(result)
        return errors

    def _remove_owned(self, container_id: str) -> str | None:
        """用独立连接有界重试，并把超时后的实际删除视为已确认。"""
        for _attempt in range(2):
            client: Any | None = None
            try:
                client = self._new_client()
                container = self._verify_with(client, container_id)
                container.remove(force=True, v=True)
                try:
                    client.containers.get(container_id)
                except docker.errors.NotFound:
                    return None
            except docker.errors.NotFound:
                return None
            except Exception:  # noqa: BLE001, S110 下一独立连接再次核验同一owner
                pass
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001, S110 后续仍需完成归属核验
                        pass
        return "container_cleanup_unknown"
