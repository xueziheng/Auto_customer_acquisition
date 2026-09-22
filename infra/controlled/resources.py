"""本owner容器与子进程的身份核验和有界清理。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker  # type: ignore[import-untyped]
import psutil  # type: ignore[import-untyped]

from .config import ControlledError

OWNER_LABEL = "tradeos.controlled.owner"


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen[bytes]
    born: float
    anchor_pid: int
    anchor_born: float
    control: socket.socket = field(repr=False)
    children: dict[int, float] = field(default_factory=dict)
    closed: bool = False

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
                psutil.Process(process.pid).create_time(),
                anchor_pid,
                psutil.Process(anchor_pid).create_time(),
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
                psutil.Process(self.process.pid).create_time() == self.born
                and os.getpgid(self.process.pid) == self.process.pid
            )
        except ProcessLookupError:
            return False
        except psutil.NoSuchProcess:
            return False

    def stop(self, timeout: int = 20) -> None:
        """先TERM完成在途cycle；升级时保留锚点，只KILL已核验的业务进程。"""
        if self.closed:
            return
        running = self.process.poll() is None
        if not self._anchor_verified() or (running and not self.verified()):
            raise ControlledError("process_owner_unknown")
        self._record_children()
        os.killpg(self.process.pid, signal.SIGTERM)
        forced = False
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not self.verified():
                raise ControlledError("process_owner_unknown") from None
            self.process.kill()
            self.process.wait(timeout=5)
            forced = True
        forced = self._close_children(timeout) or forced
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
                anchor.create_time() == self.anchor_born
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
                self.children[member.pid] = member.create_time()
        if self.process.poll() is None and self.verified():
            try:
                for child in psutil.Process(self.process.pid).children(recursive=True):
                    if child.pid != self.anchor_pid:
                        self.children[child.pid] = child.create_time()
            except psutil.NoSuchProcess:
                pass

    def _close_children(self, timeout: int) -> bool:
        self._record_children()
        live: list[Any] = []
        for pid, born in self.children.items():
            try:
                child = psutil.Process(pid)
                if child.status() == psutil.STATUS_ZOMBIE:
                    continue
                if child.create_time() != born or os.getpgid(pid) != self.process.pid:
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
                    child.create_time() != self.children[child.pid]
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
        self.client = docker.DockerClient(
            base_url="unix:///var/run/docker.sock", timeout=10
        )
        self.ids: list[str] = []

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
        if container_id not in self.ids:
            raise ControlledError("container_owner_unknown")
        container = self.client.containers.get(container_id)
        if (
            container.id != container_id
            or container.labels.get(OWNER_LABEL) != self.owner
        ):
            raise ControlledError("container_owner_unknown")
        return container

    def close(self) -> list[str]:
        errors: list[str] = []
        try:
            for container in self.client.containers.list(
                all=True, filters={"label": f"{OWNER_LABEL}={self.owner}"}
            ):
                if (
                    container.labels.get(OWNER_LABEL) == self.owner
                    and container.id not in self.ids
                ):
                    self.ids.append(container.id)
        except Exception:  # noqa: BLE001 创建结果不确定时保守核对本owner
            errors.append("container_inventory_unknown")
        for container_id in reversed(self.ids):
            try:
                container = self.verify(container_id)
                container.remove(force=True, v=True)
                try:
                    self.client.containers.get(container_id)
                    errors.append("container_cleanup_unknown")
                except docker.errors.NotFound:
                    pass
            except Exception:  # noqa: BLE001 安全边界只保留固定失败类别
                errors.append("container_cleanup_unknown")
        try:
            self.client.close()
        except Exception:  # noqa: BLE001 保留所有清理结果
            errors.append("container_client_cleanup_unknown")
        return errors
