"""QUOTE_PDF专用惰性单次写入；取消收口线程，不把未知写误称可安全重试。"""

import asyncio
import sys
import threading
import time
from typing import Any, Literal

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]

from artifact_store.transport import QuotePdfBlobTransportError
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import ObjectStoreSecretResolver, _require_key
from shared.errors import ValidationError
from shared.schemas.generated_documents import QuotePdfWriteLimits


class S3QuotePdfObjectBlobTransport:
    """每次put/delete才解析秘密并创建专属client；不保留凭证或连接。"""

    def __init__(
        self,
        settings: S3ObjectStoreSettings,
        secret_resolver: ObjectStoreSecretResolver,
        *,
        limits: QuotePdfWriteLimits,
    ) -> None:
        self._settings, self._resolver = settings, secret_resolver
        self._limits = QuotePdfWriteLimits.model_validate(limits.model_dump())

    def __repr__(self) -> str:
        return "S3QuotePdfObjectBlobTransport()"

    async def get(self, object_key: str) -> bytes:
        """旧get固定拒绝，不能借writer获得无界读取。"""
        raise QuotePdfBlobTransportError("read_unsupported")

    async def put(self, object_key: str, content: bytes) -> None:
        """只支持一次put_object，无multipart或自动重试。"""
        if type(content) is not bytes or not content:
            raise QuotePdfBlobTransportError("invalid_input")
        await self._operate("put", object_key, content)

    async def delete(self, object_key: str) -> None:
        """仅Store确认loser后可调用；中立Store不暴露删除。"""
        await self._operate("delete", object_key, None)

    async def _operate(
        self,
        operation: Literal["put", "delete"],
        object_key: str,
        content: bytes | None,
    ) -> None:
        try:
            key = _require_key(object_key)
            if not key.startswith("generated/"):
                raise ValueError
        except (ValueError, TypeError, ValidationError):
            raise QuotePdfBlobTransportError("invalid_input") from None
        deadline = time.monotonic() + self._limits.total_timeout_ms / 1000
        stopped = threading.Event()

        def write() -> None:
            client: Any = None
            attempted = False

            def check() -> None:
                if stopped.is_set() or time.monotonic() >= deadline:
                    raise QuotePdfBlobTransportError(
                        "outcome_unknown" if attempted else "unavailable"
                    )

            try:
                check()
                access = self._resolver.resolve(self._settings.access_key_ref)
                secret = self._resolver.resolve(self._settings.secret_key_ref)
                try:
                    check()
                    client = boto3.client(
                        "s3",
                        endpoint_url=self._settings.endpoint,
                        aws_access_key_id=access,
                        aws_secret_access_key=secret,
                        region_name=self._settings.region,
                        config=Config(
                            signature_version="s3v4",
                            s3={"addressing_style": "path"},
                            connect_timeout=self._limits.connect_timeout_ms / 1000,
                            read_timeout=self._limits.read_timeout_ms / 1000,
                            retries={"total_max_attempts": 1, "mode": "standard"},
                        ),
                    )
                finally:
                    del access, secret
                check()
                attempted = True
                if operation == "put":
                    client.put_object(
                        Bucket=self._settings.bucket, Key=key, Body=content
                    )
                else:
                    client.delete_object(Bucket=self._settings.bucket, Key=key)
                check()
            except QuotePdfBlobTransportError:
                raise
            except Exception:  # noqa: BLE001 -- 一旦开始SDK写，任何未知都可能已提交
                raise QuotePdfBlobTransportError(
                    "outcome_unknown" if attempted else "unavailable"
                ) from None
            finally:
                primary = sys.exc_info()[0] is not None
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001 -- 关闭错误不得覆盖更早未知结果
                        if not primary:
                            raise QuotePdfBlobTransportError(
                                "outcome_unknown" if attempted else "unavailable"
                            ) from None

        task = asyncio.create_task(asyncio.to_thread(write))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 -- SDK二次错误不得覆盖原取消
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
