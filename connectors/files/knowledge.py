"""已授权上传原件的离线解析与受限视觉输入；不取得资料权限。"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path

from shared.schemas.model_invocation import ModelInputImage

SUPPORTED_KNOWLEDGE_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
        "image/jpeg",
        "image/webp",
    }
)
PARSE_REASONS = frozenset(
    {
        "unsupported_type",
        "type_mismatch",
        "invalid_document",
        "needs_ocr",
        "size_limit",
        "text_limit",
        "page_limit",
        "archive_limit",
        "timeout",
        "parser_unavailable",
        "unsafe_xml",
        "encrypted_document",
        "empty_document",
        "source_limit_exceeded",
    }
)
PARSE_WARNINGS = frozenset(
    {
        "external_links_ignored",
        "spreadsheet_raw_values",
        "formulas_not_evaluated",
        "vision_transcription_required",
        "image_resized_1600",
        "visual_pages_rendered_1600",
    }
)
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024


class KnowledgeParseFailure(ValueError):
    """固定分类，不携带原件、路径或下层异常。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in PARSE_REASONS else "invalid_document"
        super().__init__(self.reason)


@dataclass(frozen=True)
class ParsedKnowledgeText:
    text: str = field(repr=False)
    warnings: tuple[str, ...]
    images: tuple[ModelInputImage, ...] = field(default=(), repr=False)
    image_source_pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class KnowledgeParseLimits:
    max_bytes: int = 10 * 1024 * 1024
    max_chars: int = 60000
    max_pages: int = 100
    timeout_seconds: int = 30

    def validate(self) -> None:
        for value, maximum in (
            (self.max_bytes, 10 * 1024 * 1024),
            (self.max_chars, 60000),
            (self.max_pages, 100),
            (self.timeout_seconds, 30),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise KnowledgeParseFailure("parser_unavailable")


async def _read(stream: asyncio.StreamReader, limit: int) -> bytes:
    parts = bytearray()
    while True:
        part = await stream.read(min(65536, limit + 1 - len(parts)))
        if not part:
            return bytes(parts)
        parts.extend(part)
        if len(parts) > limit:
            raise KnowledgeParseFailure("source_limit_exceeded")


async def _write(stream: asyncio.StreamWriter, data: bytes) -> None:
    try:
        for start in range(0, len(data), 65536):
            stream.write(data[start : start + 65536])
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        # 子进程可能在读正文前已固定报错，最终分类由有界 stdout 决定。
        pass
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _settle[T](task: asyncio.Task[T]) -> bool:
    """取消不能打断进程创建的收尾或资源清理；完成后由外层传播取消。"""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    return interrupted


async def _cleanup(
    process: asyncio.subprocess.Process | None,
    writer: asyncio.Task[None] | None,
    reader: asyncio.Task[bytes] | None,
) -> None:
    if process is not None:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
    tasks = [task for task in (writer, reader) if task is not None]
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _decode_output(
    raw: bytes, code: int, limits: KnowledgeParseLimits
) -> ParsedKnowledgeText:
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise KnowledgeParseFailure("invalid_document")
        if code != 0:
            reason = body.get("reason", "invalid_document")
            raise KnowledgeParseFailure(
                reason if isinstance(reason, str) else "invalid_document"
            )
        text = body["text"]
        warnings = body["warnings"]
        encoded_images = body["images"]
        pages = body["image_source_pages"]
        if (
            not isinstance(text, str)
            or len(text) > limits.max_chars
            or not isinstance(warnings, list)
            or len(warnings) > 4
            or not all(
                isinstance(value, str) and value in PARSE_WARNINGS for value in warnings
            )
            or not isinstance(encoded_images, list)
            or len(encoded_images) > MAX_IMAGES
            or not isinstance(pages, list)
            or not all(
                type(value) is int and 1 <= value <= limits.max_pages for value in pages
            )
            or pages != sorted(set(pages))
            or len(pages) != len(encoded_images)
        ):
            raise KnowledgeParseFailure("invalid_document")
        images: list[ModelInputImage] = []
        total = 0
        for item in encoded_images:
            if not isinstance(item, dict) or set(item) != {
                "mime_type",
                "data",
                "sha256",
                "byte_length",
            }:
                raise KnowledgeParseFailure("invalid_document")
            data = base64.b64decode(item["data"], validate=True)
            total += len(data)
            if len(data) > MAX_IMAGE_BYTES or total > MAX_TOTAL_IMAGE_BYTES:
                raise KnowledgeParseFailure("source_limit_exceeded")
            images.append(
                ModelInputImage(
                    mime_type=item["mime_type"],
                    data=data,
                    sha256=item["sha256"],
                    byte_length=item["byte_length"],
                )
            )
        if not text.strip() and not images:
            raise KnowledgeParseFailure("empty_document")
        return ParsedKnowledgeText(
            text=text,
            warnings=tuple(warnings),
            images=tuple(images),
            image_source_pages=tuple(pages),
        )
    except KnowledgeParseFailure:
        raise
    except Exception:  # noqa: BLE001 - 子进程材料与验证异常不进入上层日志。
        raise KnowledgeParseFailure("invalid_document") from None


class KnowledgeFileParser:
    """固定 bwrap 离线程序；不使用主进程 env，不提供无沙箱降级。"""

    def __init__(
        self,
        *,
        python_binary: Path,
        bwrap_binary: Path,
        limits: KnowledgeParseLimits | None = None,
        pdftoppm_binary: Path | None = None,
    ) -> None:
        limits = limits or KnowledgeParseLimits()
        limits.validate()
        self._python = python_binary
        self._bwrap = bwrap_binary
        self._pdftoppm = pdftoppm_binary or Path("/usr/bin/pdftoppm")
        self._limits = limits

    def _command(self, mime_type: str) -> list[str]:
        for path in (self._python, self._bwrap):
            if (
                not path.is_absolute()
                or not path.is_file()
                or not os.access(path, os.X_OK)
            ):
                raise KnowledgeParseFailure("parser_unavailable")
        command = [
            str(self._bwrap),
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
            "--setenv",
            "HOME",
            "/tmp",
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
            command.extend(["--ro-bind", "/lib64", "/lib64"])
        command.extend(
            [
                "--ro-bind",
                str(self._python.resolve()),
                "/usr/bin/python3",
                "--ro-bind",
                str(Path(__file__).with_name("_knowledge_parser.py").resolve()),
                "/parser.py",
                "--tmpfs",
                "/tmp",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
            ]
        )
        visual = mime_type == "application/pdf" or mime_type.startswith("image/")
        if visual:
            command.extend(["--dir", "/parser-libs"])
            for package in (
                ("pypdf", "PIL") if mime_type == "application/pdf" else ("PIL",)
            ):
                spec = importlib.util.find_spec(package)
                if spec is None or spec.origin is None:
                    raise KnowledgeParseFailure("parser_unavailable")
                package_root = Path(spec.origin).parent
                command.extend(
                    ["--ro-bind", str(package_root), "/parser-libs/" + package]
                )
                if package == "PIL":
                    bundled = package_root.parent / "pillow.libs"
                    if bundled.is_dir():
                        command.extend(
                            ["--ro-bind", str(bundled), "/parser-libs/pillow.libs"]
                        )
            if mime_type == "application/pdf" and self._pdftoppm.is_file():
                if not self._pdftoppm.is_absolute() or not os.access(
                    self._pdftoppm, os.X_OK
                ):
                    raise KnowledgeParseFailure("parser_unavailable")
                command.extend(
                    ["--ro-bind", str(self._pdftoppm.resolve()), "/pdftoppm"]
                )
                if Path("/usr/share/fonts").is_dir():
                    command.extend(
                        [
                            "--dir",
                            "/usr/share",
                            "--ro-bind",
                            "/usr/share/fonts",
                            "/usr/share/fonts",
                        ]
                    )
                if Path("/etc/fonts").is_dir():
                    command.extend(
                        ["--dir", "/etc", "--ro-bind", "/etc/fonts", "/etc/fonts"]
                    )
        command.extend(["/usr/bin/python3", "-I", "-B", "/parser.py"])
        return command

    async def extract(self, data: bytes, mime_type: str) -> ParsedKnowledgeText:
        if mime_type not in SUPPORTED_KNOWLEDGE_MIMES:
            raise KnowledgeParseFailure("unsupported_type")
        limits = self._limits
        if not isinstance(data, bytes) or not data or len(data) > limits.max_bytes:
            raise KnowledgeParseFailure("size_limit")
        header = (
            json.dumps(
                {
                    "mime_type": mime_type,
                    "max_bytes": limits.max_bytes,
                    "max_chars": limits.max_chars,
                    "max_pages": limits.max_pages,
                }
            ).encode()
            + b"\n"
        )
        process: asyncio.subprocess.Process | None = None
        writer_task: asyncio.Task[None] | None = None
        reader_task: asyncio.Task[bytes] | None = None
        try:
            async with asyncio.timeout(limits.timeout_seconds):
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *self._command(mime_type),
                        stdin=asyncio.subprocess.PIPE,
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
                assert process.stdin is not None and process.stdout is not None
                writer_task = asyncio.create_task(_write(process.stdin, header + data))
                reader_task = asyncio.create_task(
                    _read(
                        process.stdout,
                        limits.max_chars * 6 + MAX_TOTAL_IMAGE_BYTES * 2 + 32768,
                    )
                )
                await writer_task
                raw = await reader_task
                code = await process.wait()
                return _decode_output(raw, code, limits)
        except KnowledgeParseFailure:
            raise
        except TimeoutError:
            raise KnowledgeParseFailure("timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 不向上层泄漏资料和子进程异常。
            raise KnowledgeParseFailure("invalid_document") from None
        finally:
            cleanup = asyncio.create_task(_cleanup(process, writer_task, reader_task))
            try:
                interrupted = await _settle(cleanup)
            except Exception:  # noqa: BLE001 - 清理故障同样不得回显原始路径。
                raise KnowledgeParseFailure("parser_unavailable") from None
            if interrupted:
                raise asyncio.CancelledError
