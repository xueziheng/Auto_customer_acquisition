"""单次 Codex CLI 生命周期和唯一一次受信 Provider 调用，不暴露凭证。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import stat
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from connectors.deepseek.client import DeepSeekFailure
from connectors.obsidian.workspace import (
    AuthorizedKnowledgeDocument,
    KnowledgeTaskScope,
    TenantKnowledgeWorkspace,
)
from shared.schemas.model_invocation import ModelRequest, ModelResponse

MAX_HTTP_BYTES = 2 * 1024 * 1024


class DelegateProvider(Protocol):
    async def prepare(self) -> None: ...
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class CodexRunnerSettings:
    """部署方显式固定程序与容量；不包含密钥或任意挂载目录。"""

    codex_binary: Path
    bwrap_binary: Path
    python_binary: Path
    job_root: Path
    timeout_seconds: int
    max_input_bytes: int
    max_output_bytes: int

    def validate(self) -> None:
        for value in (
            self.timeout_seconds,
            self.max_input_bytes,
            self.max_output_bytes,
        ):
            if type(value) is not int or value <= 0:
                raise DeepSeekFailure("invalid_request", dispatched=False)
        if (
            self.max_input_bytes > 1024 * 1024
            or self.max_output_bytes > 4 * 1024 * 1024
        ):
            raise DeepSeekFailure("invalid_request", dispatched=False)
        for path in (self.codex_binary, self.bwrap_binary, self.python_binary):
            if (
                not path.is_absolute()
                or not path.is_file()
                or not os.access(path, os.X_OK)
            ):
                raise DeepSeekFailure("invalid_request", dispatched=False)
        current = Path("/")
        for part in self.job_root.parts[1:]:
            current /= part
            if current.is_symlink():
                raise DeepSeekFailure("invalid_request", dispatched=False)
        info = self.job_root.stat()
        if (
            not self.job_root.is_absolute()
            or ".." in self.job_root.parts
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or len(os.fsencode(self.job_root)) > 60
        ):
            raise DeepSeekFailure("invalid_request", dispatched=False)


def encode_codex_input(request: ModelRequest) -> bytes:
    """统一预检与 CLI 输入快照的 UTF-8 容量；图像留在父进程的独立受限请求内。"""
    return json.dumps(
        {"instructions": request.system_prompt, "input": request.payload},
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def response_sse(response: ModelResponse) -> str:
    """只桥接最终文本；原 usage 通过 Provider 契约交付，不伪造未知 token。"""
    message: dict[str, object] = {
        "id": "msg_tradeos",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": response.text, "annotations": []}],
    }
    complete: dict[str, object] = {
        "id": "resp_tradeos",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": response.model,
        "output": [message],
    }
    usage = response.usage
    if usage.input_tokens is not None and usage.output_tokens is not None:
        complete["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
        }
    events: list[dict[str, object]] = [
        {
            "type": "response.created",
            "response": {**complete, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**message, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": "msg_tradeos",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_tradeos",
            "output_index": 0,
            "content_index": 0,
            "delta": response.text,
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_tradeos",
            "output_index": 0,
            "content_index": 0,
            "text": response.text,
        },
        {
            "type": "response.content_part.done",
            "item_id": "msg_tradeos",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": response.text, "annotations": []},
        },
        {"type": "response.output_item.done", "output_index": 0, "item": message},
        {"type": "response.completed", "response": complete},
    ]
    return "".join(
        "event: "
        + str(event["type"])
        + "\ndata: "
        + json.dumps(event, ensure_ascii=False)
        + "\n\n"
        for event in events
    )


class OneShotBroker:
    """固定受信请求，不采用 CLI 的请求正文；一次发送后永久拒绝重试。"""

    def __init__(
        self,
        delegate: DelegateProvider,
        request: ModelRequest,
        *,
        max_output_bytes: int,
    ) -> None:
        self.delegate = delegate
        self.request = request
        self.max_output_bytes = max_output_bytes
        self.dispatched = False
        self.response: ModelResponse | None = None
        self.failure: DeepSeekFailure | None = None
        self.tasks: set[asyncio.Task[None]] = set()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        envelope: dict[str, object] = {
            "status": 409,
            "body": '{"error":"single_call_only"}',
        }
        try:
            async with asyncio.timeout(300):
                length = struct.unpack("!I", await reader.readexactly(4))[0]
                if not 0 < length <= MAX_HTTP_BYTES:
                    raise ValueError
                raw = await reader.readexactly(length)
                if not isinstance(json.loads(raw), dict):
                    raise TypeError
                if not self.dispatched:
                    self.dispatched = True
                    try:
                        response = await self.delegate.generate(self.request)
                        if (
                            response.model != self.request.model
                            or len(response.text.encode()) > self.max_output_bytes
                            or not isinstance(json.loads(response.text), dict)
                        ):
                            raise DeepSeekFailure("invalid_response")
                        json.dumps(json.loads(response.text), allow_nan=False)
                        self.response = response
                        envelope = {"status": 200, "body": response_sse(response)}
                    except DeepSeekFailure as failure:
                        self.failure = failure
                    except Exception:  # noqa: BLE001 - 进程边界禁止回显 Provider 和资料原文。
                        self.failure = DeepSeekFailure("unknown")
            content = json.dumps(envelope, ensure_ascii=False).encode()
            writer.write(struct.pack("!I", len(content)) + content)
            await writer.drain()
        except (
            ValueError,
            RecursionError,
            TypeError,
            OSError,
            asyncio.IncompleteReadError,
            TimeoutError,
        ):
            pass
        finally:
            if task is not None and task.cancelling():
                writer.transport.abort()
            else:
                writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            if task is not None:
                self.tasks.discard(task)

    async def close(self) -> None:
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _sandbox_command(
    settings: CodexRunnerSettings, job: Path, workspace: Path
) -> list[str]:
    """从空挂载树组装，只挂载系统运行库、固定程序及本任务输入/桥接 socket。"""
    command = [
        str(settings.bwrap_binary),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--dir",
        "/usr",
        "--dir",
        "/usr/bin",
        "--ro-bind",
        "/usr/lib",
        "/usr/lib",
        "--ro-bind",
        "/lib",
        "/lib",
    ]
    if Path("/lib64").exists():
        command += ["--ro-bind", "/lib64", "/lib64"]
    command += [
        "--ro-bind",
        str(settings.python_binary.resolve()),
        "/usr/bin/python3",
        "--ro-bind",
        str(settings.codex_binary.resolve()),
        "/codex",
        "--ro-bind",
        str(Path(__file__).with_name("runner.py").resolve()),
        "/runner.py",
        "--ro-bind",
        str(job / "settings.json"),
        "/settings.json",
        "--ro-bind",
        str(job / "broker.sock"),
        "/broker.sock",
        "--ro-bind",
        str(workspace),
        "/input",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/state",
        "--tmpfs",
        "/output",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--chdir",
        "/input",
        "/usr/bin/python3",
        "-I",
        "-B",
        "/runner.py",
    ]
    return command


async def _bounded_stdout(stream: asyncio.StreamReader, maximum: int) -> bytes:
    result = bytearray()
    while True:
        chunk = await stream.read(min(65536, maximum + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > maximum:
            raise ValueError("codex_output_limit")


async def _kill(process: asyncio.subprocess.Process) -> None:
    """杀掉独立 bwrap 进程组；PID 命名空间父进程退出同时回收内部子进程。"""
    try:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _settle[T](task: asyncio.Task[T]) -> bool:
    """进程创建与清理期间的二次取消只延后传播，不能留下子进程。"""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    return interrupted


async def _cleanup_job(
    process: asyncio.subprocess.Process | None,
    server: asyncio.Server | None,
    broker: OneShotBroker,
    job: Path,
    identity: int,
) -> None:
    if process is not None:
        await _kill(process)
    if server is not None:
        server.close()
    await broker.close()
    if server is not None:
        await server.wait_closed()
    if not job.is_dir() or job.is_symlink() or job.stat().st_ino != identity:
        raise DeepSeekFailure("unknown", dispatched=broker.dispatched)
    shutil.rmtree(job)


class CodexCliProvider:
    """无跨任务缓存的单次 Provider；身份由受信工厂闭包传入。"""

    def __init__(
        self,
        delegate: DelegateProvider,
        *,
        settings: CodexRunnerSettings,
        scope: KnowledgeTaskScope,
    ) -> None:
        self._delegate = delegate
        self._settings = settings
        self._scope = scope
        self._closed = False
        self._used = False

    async def prepare(self) -> None:
        """仅核本地配置与准备父进程 SDK，绝不调用模型。"""
        try:
            self._settings.validate()
            self._scope.__post_init__()
            if self._closed:
                raise ValueError
            await self._delegate.prepare()
        except DeepSeekFailure:
            raise
        except Exception:  # noqa: BLE001 - 进程边界禁止回显 Provider 和资料原文。
            raise DeepSeekFailure("invalid_request", dispatched=False) from None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self._closed or self._used:
            raise DeepSeekFailure("invalid_request", dispatched=False)
        self._used = True
        await self.prepare()
        content = encode_codex_input(request)
        if len(content) > self._settings.max_input_bytes:
            raise DeepSeekFailure("invalid_request", dispatched=False)
        job = Path(tempfile.mkdtemp(prefix="cx-", dir=self._settings.job_root))
        identity = job.stat().st_ino
        broker = OneShotBroker(
            self._delegate, request, max_output_bytes=self._settings.max_output_bytes
        )
        server: asyncio.Server | None = None
        process: asyncio.subprocess.Process | None = None
        try:
            settings = self._settings
            local_settings = {
                "model": request.model,
                "timeout_seconds": settings.timeout_seconds,
                "max_input_bytes": settings.max_input_bytes,
                "max_output_bytes": settings.max_output_bytes,
            }
            (job / "settings.json").write_text(json.dumps(local_settings))
            (job / "settings.json").chmod(0o400)
            workspace_root = job / "workspace"
            workspace_root.mkdir(mode=0o700)
            workspaces = TenantKnowledgeWorkspace(
                workspace_root,
                maximum_documents=1,
                maximum_bytes=settings.max_input_bytes,
            )
            document = AuthorizedKnowledgeDocument(
                scope=self._scope,
                source_id="request",
                version="v1",
                source_name="request.md",
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
            server = await asyncio.start_unix_server(
                broker.handle, path=job / "broker.sock"
            )
            (job / "broker.sock").chmod(0o600)
            async with workspaces.prepare(self._scope, (document,)) as prepared:
                command = _sandbox_command(settings, job, prepared.verified_path())
                async with asyncio.timeout(settings.timeout_seconds + 5):
                    spawning = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            *command,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                            env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
                            start_new_session=True,
                        )
                    )
                    try:
                        process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        await _settle(spawning)
                        process = spawning.result()
                        raise
                    assert process.stdout is not None
                    output = await _bounded_stdout(
                        process.stdout, settings.max_output_bytes * 6 + 1024
                    )
                    code = await process.wait()
                    if broker.failure is not None:
                        raise broker.failure
                    if code != 0 or broker.response is None:
                        raise DeepSeekFailure(
                            "invalid_response", dispatched=broker.dispatched
                        )
                    final = json.loads(output)["final"]
                    if not isinstance(final, str) or json.dumps(
                        json.loads(final), sort_keys=True, allow_nan=False
                    ) != json.dumps(
                        json.loads(broker.response.text),
                        sort_keys=True,
                        allow_nan=False,
                    ):
                        raise DeepSeekFailure("invalid_response")
                    prepared.verified_path()
                    return broker.response
        except DeepSeekFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 进程边界禁止回显 Provider 和资料原文。
            raise DeepSeekFailure(
                "unknown" if broker.dispatched else "invalid_request",
                dispatched=broker.dispatched,
            ) from None
        finally:
            cleanup = asyncio.create_task(
                _cleanup_job(process, server, broker, job, identity)
            )
            try:
                interrupted = await _settle(cleanup)
            except Exception:  # noqa: BLE001 - 清理异常同样禁止暴露路径和资料。
                raise DeepSeekFailure("unknown", dispatched=broker.dispatched) from None
            if interrupted:
                raise asyncio.CancelledError

    async def aclose(self) -> None:
        self._closed = True
        await self._delegate.aclose()
