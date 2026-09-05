"""本owner容器与子进程的身份核验和有界清理。"""

from __future__ import annotations

import os
import signal
import subprocess
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
    children: dict[int, float] = field(default_factory=dict)

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
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environ,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=pass_fds,
        )
        return cls(name, process, psutil.Process(process.pid).create_time())

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
        """先TERM完成在途cycle；必要时只KILL仍有原出生身份的进程组。"""
        if self.process.poll() is not None:
            self._close_children(timeout)
            return
        self._record_children()
        if not self.verified():
            raise ControlledError("process_owner_unknown")
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if not self.verified():
                raise ControlledError("process_owner_unknown") from None
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
            raise ControlledError("process_forced_stop") from None
        self._close_children(timeout)
        if self.process.returncode not in {0, -signal.SIGTERM, 128 + signal.SIGTERM}:
            raise ControlledError("process_cleanup_failed")

    def _record_children(self) -> None:
        if self.process.poll() is None and self.verified():
            try:
                for child in psutil.Process(self.process.pid).children(recursive=True):
                    self.children[child.pid] = child.create_time()
            except psutil.NoSuchProcess:
                pass

    def _close_children(self, timeout: int) -> None:
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
            raise ControlledError("process_forced_stop")

    def public(self) -> dict[str, object]:
        self._record_children()
        return {
            "name": self.name,
            "pid": self.process.pid,
            "born": self.born,
            "exit": self.process.poll(),
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
