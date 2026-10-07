"""Linux受限一次性解析器；启动必须先通过私有实际资源探针。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import platform
import signal
import struct
import sys
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import ValidationError

from shared.schemas.evidence_read import (
    EvidenceParseLimits,
    EvidenceParserCapability,
    EvidenceProbeLimits,
    EvidenceProfile,
    ParsedEvidenceText,
    QuoteEvidenceError,
)

_PROTOCOL: Literal["evidence-worker-v1"] = "evidence-worker-v1"


class _ReadPipeState(Protocol):
    """仅CPython固定runtime的StreamReader私有管道/缓存类型桥接。"""

    _transport: asyncio.ReadTransport
    _buffer: bytearray


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _runtime() -> tuple[str, Literal["platform", "runtime"] | None]:
    try:
        version = importlib.metadata.version("pypdf")
    except importlib.metadata.PackageNotFoundError:
        version = "missing"
    files = {}
    try:
        for name in ("client.py", "worker.py", "profiles.py", "probe.py"):
            files[name] = hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
    except OSError:
        return _digest({"runtime": "unavailable"}), "runtime"
    digest = _digest(
        {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.system(),
            "pypdf": version,
            "protocol": _PROTOCOL,
            "modules": files,
        }
    )
    if platform.system() != "Linux":
        return digest, "platform"
    if (
        platform.python_implementation() != "CPython"
        or sys.version_info[:3] != (3, 12, 14)
        or version != "6.16.2"
    ):
        return digest, "runtime"
    return digest, None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("协议重复字段")
        result[key] = value
    return result


class LinuxEvidenceTextParser:
    """父进程只做有界IPC与资源生命周期，不在event loop解析资料。"""

    def __init__(
        self, *, limits: EvidenceParseLimits, probe_limits: EvidenceProbeLimits
    ) -> None:
        try:
            self._limits = EvidenceParseLimits.model_validate(limits.model_dump())
            self._probe_limits = EvidenceProbeLimits.model_validate(
                probe_limits.model_dump()
            )
        except (ValidationError, AttributeError):
            raise QuoteEvidenceError("invalid_input") from None
        self._limits_hash = _digest(
            {
                "parse": self._limits.model_dump(),
                "probe": self._probe_limits.model_dump(),
                "protocol": _PROTOCOL,
            }
        )
        self._runtime_hash, failure = _runtime()
        self._status: Literal["available", "unavailable"] = "unavailable"
        self._failure: Literal["platform", "resource", "protocol", "runtime"] | None = (
            failure
        )
        self._closed = False
        self._probing = False
        self._probe_generation = 0
        self._probe_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self._limits.maximum_concurrency)
        self._calls: set[asyncio.Task] = set()
        self._probes: set[asyncio.Task] = set()
        self._launches: set[asyncio.Task[asyncio.subprocess.Process]] = set()
        self._processes: set[asyncio.subprocess.Process] = set()

    def capability(self) -> EvidenceParserCapability:
        """无spawn/原件/秘密的公开摘要，不能用于反向启用。"""
        return EvidenceParserCapability(
            status=self._status,
            profile_version=_PROTOCOL,
            limits_hash=self._limits_hash,
            runtime_hash=self._runtime_hash,
            failure=self._failure,
        )

    async def probe(self) -> EvidenceParserCapability:
        """登记整个probe（含排队/启动窗口），关闭必须等待其完整收口。"""
        owner = asyncio.current_task()
        assert owner is not None
        self._probes.add(owner)
        try:
            return await self._probe()
        finally:
            self._probes.discard(owner)

    async def _probe(self) -> EvidenceParserCapability:
        """成功必须来自本实例真实四探针；取消/失败立即清除旧成功。"""
        self._status = "unavailable"
        self._probe_generation += 1
        generation = self._probe_generation
        async with self._probe_lock:
            if self._closed:
                self._failure = "resource"
                return self.capability()
            self._probing = True
            self._runtime_hash, failure = _runtime()
            self._failure = failure
            try:
                await self._cancel_calls()
                if failure is not None:
                    return self.capability()
                limits = self._probe_limits
                for action in ("cpu", "as", "wall", "ipc"):
                    if self._closed:
                        return self.capability()
                    code, result, reason = await self._run(
                        "connectors.evidence_text.probe",
                        {"action": action, "limits": limits.model_dump()},
                        b"",
                        limits.maximum_result_bytes,
                        limits.wall_timeout_ms,
                        limits.termination_grace_ms,
                    )
                    valid = (
                        (
                            action == "cpu"
                            and code == -signal.SIGXCPU
                            and reason == "short"
                        )
                        or (
                            action == "as"
                            and code == 0
                            and reason is None
                            and result == {"probe": "as", "limited": True}
                        )
                        or (
                            action == "wall"
                            and reason == "timeout"
                            and code in {-signal.SIGTERM, -signal.SIGKILL}
                        )
                        or (
                            action == "ipc"
                            and reason == "oversized"
                            and code in {-signal.SIGTERM, -signal.SIGKILL}
                        )
                    )
                    if not valid:
                        self._failure = (
                            "resource"
                            if action in {"cpu", "as", "wall"}
                            else "protocol"
                        )
                        return self.capability()
                if self._closed or generation != self._probe_generation:
                    self._failure = "resource"
                    return self.capability()
                self._status = "available"
                self._failure = None
                return self.capability()
            except asyncio.CancelledError:
                self._failure = "resource"
                raise
            except Exception:  # noqa: BLE001 - 探针失败不输出环境或子进程异常
                self._failure = "protocol"
                return self.capability()
            finally:
                self._probing = False

    async def parse(
        self, content: bytes, *, profile: EvidenceProfile, page: int | None
    ) -> ParsedEvidenceText:
        """只处理固定parse请求，无fault、程序、路径或外部capability参数。"""
        if self._closed or self._probing or self._status != "available":
            raise QuoteEvidenceError("parse_unavailable")
        runtime_hash, failure = _runtime()
        if failure is not None or runtime_hash != self._runtime_hash:
            self._status, self._failure = "unavailable", "runtime"
            raise QuoteEvidenceError("parse_unavailable")
        if (
            type(content) is not bytes
            or not content
            or not (
                (profile == "pdf-text-v1" and type(page) is int and page >= 1)
                or (profile == "rfc822-plain-v1" and page is None)
            )
        ):
            raise QuoteEvidenceError("invalid_input")
        if len(content) > self._limits.maximum_input_bytes:
            raise QuoteEvidenceError("source_limit_exceeded")
        owner = asyncio.current_task()
        assert owner is not None
        self._calls.add(owner)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(), self._limits.queue_timeout_ms / 1000
                )
            except TimeoutError:
                raise QuoteEvidenceError("parse_timeout") from None
            acquired = True
            if self._closed or self._probing or self._status != "available":
                raise QuoteEvidenceError("parse_unavailable")
            code, payload, reason = await self._run(
                "connectors.evidence_text.worker",
                {
                    "protocol": _PROTOCOL,
                    "operation": "parse",
                    "profile": profile,
                    "page": page,
                    "input_bytes": len(content),
                    "limits": self._limits.model_dump(),
                },
                content,
                self._limits.maximum_result_bytes,
                self._limits.wall_timeout_ms,
                self._limits.termination_grace_ms,
            )
            if reason == "timeout":
                raise QuoteEvidenceError("parse_timeout")
            if reason == "oversized":
                self._status, self._failure = "unavailable", "protocol"
                raise QuoteEvidenceError("parse_limit_exceeded")
            if code == -signal.SIGXCPU or code == 71:
                raise QuoteEvidenceError("parse_limit_exceeded")
            if code != 0 or reason is not None or type(payload) is not dict:
                raise QuoteEvidenceError("parse_unavailable")
            if set(payload) == {"error"}:
                error = payload["error"]
                if error in {
                    "parse_unavailable",
                    "parse_unsupported",
                    "parse_limit_exceeded",
                    "source_limit_exceeded",
                    "locator_mismatch",
                    "invalid_input",
                }:
                    raise QuoteEvidenceError(error)
                raise QuoteEvidenceError("parse_unavailable")
            if (
                set(payload) != {"protocol", "profile", "page", "text"}
                or payload["protocol"] != _PROTOCOL
                or payload["profile"] != profile
                or payload["page"] != page
                or type(payload["page"]) is not type(page)
                or type(payload["text"]) is not str
            ):
                raise QuoteEvidenceError("parse_unavailable")
            text = payload["text"]
            if (
                len(text.encode("utf-8", errors="strict"))
                > self._limits.maximum_text_bytes
            ):
                raise QuoteEvidenceError("parse_limit_exceeded")
            return ParsedEvidenceText(profile=profile, page=page, text=text)
        except QuoteEvidenceError as error:
            if error.code == "parse_unavailable":
                self._status, self._failure = "unavailable", "protocol"
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 协议/启动异常不携原文
            self._status, self._failure = "unavailable", "protocol"
            raise QuoteEvidenceError("parse_unavailable") from None
        finally:
            if acquired:
                self._semaphore.release()
            self._calls.discard(owner)

    async def aclose(self) -> None:
        """拒绝新请求，取消并回收当前调用及全部实际子进程。"""
        self._closed = True
        self._status = "unavailable"
        owner = asyncio.current_task()

        async def finish() -> None:
            tasks = tuple(
                task for task in self._calls | self._probes if task is not owner
            )
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for launch in tuple(self._launches):
                process = await launch
                await self._reap(process, self._limits.termination_grace_ms)
            for process in tuple(self._processes):
                await self._reap(process, self._limits.termination_grace_ms)

        await self._wait_cleanup(asyncio.create_task(finish()))

    async def _cancel_calls(self) -> None:
        tasks = tuple(
            task for task in self._calls if task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _reap(self, process: asyncio.subprocess.Process, grace_ms: int) -> None:
        async def finish() -> None:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            # CPython固定runtime的读管道桥接：先关闭输入来源，不能等背压stdout自行EOF。
            if process.stdout is not None:
                cast(_ReadPipeState, process.stdout)._transport.close()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), grace_ms / 1000)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            await process.wait()
            if process.stdin is not None:
                process.stdin.close()
                try:
                    await process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            if process.stdout is not None:
                # 管道已close，不再进新bytes；仅有限排空当时已缓存的长度，随后等待EOF。
                buffered = len(cast(_ReadPipeState, process.stdout)._buffer)
                for offset in range(0, buffered, 65536):
                    await process.stdout.read(min(65536, buffered - offset))
                if await process.stdout.read(1):
                    raise RuntimeError("来源读管道未关闭")
            self._processes.discard(process)

        await self._wait_cleanup(asyncio.create_task(finish()))

    @staticmethod
    async def _wait_cleanup(cleanup: asyncio.Task[None]) -> None:
        """首次/重复取消均延迟到资源回收完成后传播，不把取消改成成功。"""
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _run(
        self,
        module: str,
        header: dict,
        content: bytes,
        maximum_result: int,
        wall_ms: int,
        grace_ms: int,
    ) -> tuple[int | None, dict | None, str | None]:
        if self._closed:
            raise QuoteEvidenceError("parse_unavailable")
        process = None
        deadline = asyncio.get_running_loop().time() + wall_ms / 1000
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                module,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                limit=65536,
            )
        )
        self._launches.add(launch)
        try:
            try:
                process = await asyncio.wait_for(asyncio.shield(launch), wall_ms / 1000)
            except (asyncio.CancelledError, TimeoutError) as error:
                while not launch.done():
                    try:
                        await asyncio.shield(launch)
                    except asyncio.CancelledError:
                        continue
                process = launch.result()
                if isinstance(error, TimeoutError):
                    await self._reap(process, grace_ms)
                    return process.returncode, None, "timeout"
                raise
            self._processes.add(process)
            assert process.stdin is not None and process.stdout is not None

            async def exchange() -> dict | None:
                assert process.stdin is not None and process.stdout is not None
                encoded = json.dumps(
                    header, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
                if len(encoded) > 16384:
                    raise ValueError("控制头超限")
                process.stdin.write(struct.pack("!I", len(encoded)) + encoded)
                await process.stdin.drain()
                for index in range(0, len(content), 65536):
                    process.stdin.write(content[index : index + 65536])
                    await process.stdin.drain()
                process.stdin.close()
                size = struct.unpack("!I", await process.stdout.readexactly(4))[0]
                if not 0 < size <= maximum_result:
                    raise OverflowError("结果帧超限")
                encoded_result = await process.stdout.readexactly(size)
                if await process.stdout.read(1):
                    raise ValueError("额外结果帧")
                await process.wait()
                payload = json.loads(
                    encoded_result.decode("utf-8", errors="strict"),
                    object_pairs_hook=_unique_object,
                )
                if type(payload) is not dict:
                    raise ValueError("结果形状不合法")
                return payload

            try:
                result = await asyncio.wait_for(
                    exchange(), max(0, deadline - asyncio.get_running_loop().time())
                )
                return process.returncode, result, None
            except TimeoutError:
                await self._reap(process, grace_ms)
                return process.returncode, None, "timeout"
            except OverflowError:
                await self._reap(process, grace_ms)
                return process.returncode, None, "oversized"
            except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError):
                try:
                    await asyncio.wait_for(
                        process.wait(),
                        max(0, deadline - asyncio.get_running_loop().time()),
                    )
                except TimeoutError:
                    await self._reap(process, grace_ms)
                    return process.returncode, None, "timeout"
                return process.returncode, None, "short"
        finally:
            try:
                if process is not None:
                    await self._reap(process, grace_ms)
            finally:
                self._launches.discard(launch)
