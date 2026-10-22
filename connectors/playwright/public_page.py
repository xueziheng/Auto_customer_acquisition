"""Gateway内部公开浏览器连接器；真实Playwright仅在browser_worker子进程运行。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
from contextlib import suppress
from datetime import UTC, datetime
from hashlib import sha256

from artifact_store.store import RawArtifactKind, RawArtifactStore
from connectors.web_search.client import PageSnapshot, _VisibleTextParser
from shared.schemas.identifiers import TenantId, UserId
from shared.schemas.page_failure import PageReadFailure


class BrowserPublicPageReader:
    def __init__(self, artifacts: RawArtifactStore) -> None:
        self.artifacts = artifacts

    async def read_page(
        self, tenant_id: TenantId, url: str, uploaded_by: UserId
    ) -> PageSnapshot:
        """子进程无Provider凭证；取消及超时终止整个进程组。"""
        stage = "browser_init"
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "apps.browser_worker.public_page",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": os.path.expanduser("~"),
                    "LANG": "C.UTF-8",
                },
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
        except Exception:  # noqa: BLE001 -- 进程启动只返回固定阶段。
            raise PageReadFailure(stage, "failed") from None
        try:
            stage = "browser_ipc"
            async with asyncio.timeout(50):
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(json.dumps({"url": url}).encode())
                await process.stdin.drain()
                process.stdin.close()
                chunks = bytearray()
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > 8 * 1024 * 1024:
                        raise ValueError("browser_output_limit")
                raw = bytes(chunks)
                await process.wait()
                if process.returncode != 0:
                    raise decode_browser_failure(raw)
                result = json.loads(raw)
                if not isinstance(result, dict) or set(result) != {"url", "html", "screenshot", "trace"} or any(not isinstance(v, str) for v in result.values()):
                    raise PageReadFailure("browser_ipc", "invalid_frame")
                stage = "browser_art"
                html = result["html"].encode()
                # 每个产物独立不可变；trace以base64标准文本保存，便于审计下载还原。
                await self.artifacts.put(
                    tenant_id,
                    RawArtifactKind.IMAGE,
                    base64.b64decode(result["screenshot"], validate=True),
                    "image/png",
                    uploaded_by,
                )
                await self.artifacts.put(
                    tenant_id,
                    RawArtifactKind.TEXT,
                    result["trace"].encode(),
                    "text/plain",
                    uploaded_by,
                )
                meta = await self.artifacts.put(
                    tenant_id,
                    RawArtifactKind.WEB_SNAPSHOT,
                    html,
                    "text/html",
                    uploaded_by,
                )
                if (
                    meta.tenant_id != tenant_id
                    or meta.content_hash != sha256(html).hexdigest()
                ):
                    raise ValueError("browser_artifact_invalid")
                parser = _VisibleTextParser()
                parser.feed(result["html"])
                parser.close()
                return PageSnapshot(
                    parser.text(),
                    result["url"],
                    datetime.now(UTC),
                    meta.content_hash,
                    meta.artifact_id,
                )
        except PageReadFailure:
            raise
        except TimeoutError:
            raise PageReadFailure(stage, "timeout") from None
        except Exception:  # noqa: BLE001 -- 不跨边界暴露浏览器或存储异常。
            raise PageReadFailure(stage, "failed") from None
        finally:
            if process.returncode is None:
                primary_failure = sys.exc_info()[0] is not None
                try:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                except Exception:  # noqa: BLE001 -- 清理不能泄露或覆盖首个故障。
                    if not primary_failure:
                        raise PageReadFailure("browser_end", "failed") from None


def decode_browser_failure(raw: bytes) -> PageReadFailure:
    """只接受子进程的固定错误帧，不信任额外字段与自由文本。"""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"error"}:
            raise ValueError
        error = value["error"]
        if not isinstance(error, dict) or set(error) != {"stage", "reason"}:
            raise ValueError
        if not all(isinstance(v, str) for v in error.values()):
            raise ValueError
        return PageReadFailure(error["stage"], error["reason"])
    except (ValueError, TypeError, RecursionError):
        return PageReadFailure("browser_ipc", "invalid_frame")
