"""QQ 准备专用阻塞 IO 隔离；私有 socket IPC，不在线程中遗留 DNS/SDK。"""
from __future__ import annotations

import asyncio
import base64
import json
import socket
import subprocess
import sys
from collections.abc import Callable
from time import monotonic
from typing import Any

CLEANUP_GRACE_SECONDS = 2.0
MAX_PACKET = 262144


class PreparationIOError(RuntimeError):
    def __init__(self, code: str = "unknown") -> None:
        self.code = code if code in {"not_found", "limit"} else "unknown"
        super().__init__("preparation IO failed")


class PreparationDeadline:
    """一个准备阶段只有一个截止点；清理不获得新的业务 IO 时间。"""

    def __init__(self, seconds: float) -> None:
        self.at = monotonic() + seconds
        self.aborted = False
        self._aborters: list[Callable[[], None]] = []
        self._timer: asyncio.TimerHandle | None = None
        self._timeout: asyncio.Timeout | None = None

    def watch(self, timeout: asyncio.Timeout) -> None:
        self._timeout = timeout
        self._arm()

    def _arm(self) -> None:
        self.disarm()
        remaining = max(0, self.at - monotonic())
        loop = asyncio.get_running_loop()
        self._timer = loop.call_later(remaining, self.abort)
        if self._timeout is not None:
            self._timeout.reschedule(loop.time() + remaining)

    def shorten(self, at: float) -> None:
        if at < self.at:
            self.at = at
            if self._timeout is not None:
                self._arm()
        self.remaining()

    def disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def remaining(self) -> float:
        remaining = self.at - monotonic()
        if self.aborted or remaining <= 0:
            raise TimeoutError()
        return remaining

    def register(self, abort: Callable[[], None]) -> None:
        self.remaining()
        self._aborters.append(abort)

    def unregister(self, abort: Callable[[], None]) -> None:
        if abort in self._aborters:
            self._aborters.remove(abort)

    def abort(self) -> None:
        self.aborted = True
        for abort in tuple(self._aborters):
            abort()


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return {"tuple": [_encode(x) for x in value]}
    if isinstance(value, list):
        return [_encode(x) for x in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("invalid IPC value")


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"bytes"}:
            return base64.b64decode(value["bytes"], validate=True)
        if set(value) == {"tuple"}:
            return tuple(_decode(x) for x in value["tuple"])
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(x) for x in value]
    return value


def pack(value: Any) -> bytes:
    payload = json.dumps(_encode(value), separators=(",", ":")).encode()
    if not 0 < len(payload) <= MAX_PACKET:
        raise ValueError("invalid IPC size")
    return len(payload).to_bytes(4, "big") + payload


def unpack(payload: bytes) -> Any:
    return _decode(json.loads(payload))


class DeadlineProcess:
    """单次准备持有的子进程；取消立即 kill，固定宽限内 wait/reap。"""

    def __init__(self, deadline: PreparationDeadline, kind: str) -> None:
        if kind not in {"sent", "objects"}:
            raise ValueError("invalid preparation worker")
        self.deadline, self.kind = deadline, kind
        self.process: subprocess.Popen[bytes] | None = None
        self.channel: socket.socket | None = None
        self.deadline.register(self.abort)

    def _command(self, fd: int) -> list[str]:
        return [sys.executable, "-m", "connectors._deadline_child", str(fd),
                str(self.deadline.at), self.kind]

    def _start(self) -> None:
        self.deadline.remaining()
        parent, child = socket.socketpair()
        try:
            self.process = subprocess.Popen(self._command(child.fileno()), pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=True)
            parent.setblocking(False)
            self.channel = parent
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.deadline.remaining()

    async def _receive(self, size: int) -> bytes:
        assert self.channel is not None
        result = bytearray()
        loop = asyncio.get_running_loop()
        while len(result) < size:
            part = await loop.sock_recv(self.channel, size - len(result))
            if not part:
                raise TimeoutError("preparation worker stopped")
            result.extend(part)
        return bytes(result)

    async def call(self, operation: str, *args: Any) -> Any:
        try:
            async with asyncio.timeout(self.deadline.remaining()):
                if self.process is None:
                    self._start()
                assert self.channel is not None
                await asyncio.get_running_loop().sock_sendall(self.channel, pack([operation, args]))
                size = int.from_bytes(await self._receive(4), "big")
                if not 0 < size <= MAX_PACKET:
                    raise ValueError("invalid IPC size")
                reply = unpack(await self._receive(size))
                self.deadline.remaining()
                if not isinstance(reply, dict) or reply.get("ok") is not True:
                    raise PreparationIOError(reply.get("code", "unknown") if isinstance(reply, dict) else "unknown")
                return reply["value"]
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass

    def close(self) -> None:
        self.abort()
        try:
            if self.process is not None:
                # SIGKILL 后不运行 Python/SDK 清理；由内核关闭 FD。禁止遗留线程。
                self.process.wait(timeout=max(.001, min(CLEANUP_GRACE_SECONDS,
                    self.deadline.at + CLEANUP_GRACE_SECONDS - monotonic())))
        finally:
            if self.channel is not None:
                self.channel.close()
            self.deadline.unregister(self.abort)
