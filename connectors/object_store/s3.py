"""S3 / MinIO 的 bucket-bound bytes 传输适配器。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from artifact_store.transport import BlobObjectNotFoundError
from connectors.object_store.config import S3ObjectStoreSettings
from shared.errors import TransientError, ValidationError

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_OBJECT_KEY = re.compile(rf"(?:raw|generated)/tn_{_ULID}/art_{_ULID}")


@runtime_checkable
class ObjectStoreSecretResolver(Protocol):
    """只解析调用方给出的安全引用，不枚举或展示凭证。"""

    def resolve(self, secret_ref: str) -> str: ...


def _require_key(object_key: object) -> str:
    if not isinstance(object_key, str) or _OBJECT_KEY.fullmatch(object_key) is None:
        raise ValidationError("Artifact object key 无效")
    return object_key


async def _shielded_thread[T](operation: Callable[[], T]) -> T:
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except BaseException:  # noqa: BLE001,S110 - secondary 不得覆盖 cancellation
            pass
        raise


class S3ObjectBlobTransport:
    """将 boto3 的同步调用隔离在线程中，并只暴露固定错误。"""

    def __init__(
        self,
        settings: S3ObjectStoreSettings,
        secret_resolver: ObjectStoreSecretResolver,
    ) -> None:
        access_key = secret_resolver.resolve(settings.access_key_ref)
        secret_key = secret_resolver.resolve(settings.secret_key_ref)
        try:
            self._client: Any = boto3.client(
                "s3",
                endpoint_url=settings.endpoint,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=settings.region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                ),
            )
        except Exception:  # noqa: BLE001 - connector 边界统一脱敏 SDK 初始化错误
            raise TransientError("Artifact 对象存储暂不可用") from None
        finally:
            del access_key, secret_key
        self._bucket = settings.bucket
        self._closed = False
        self._close_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "S3ObjectBlobTransport()"

    async def aclose(self) -> None:
        """在线程中关闭自有 SDK，失败保留引用，完成后重复关闭无副作用。"""
        async with self._close_lock:
            if not self._closed:
                await self._call(self._client.close)
                self._closed = True

    async def _call[T](self, operation: Callable[[], T]) -> T:
        if self._closed:
            raise TransientError("Artifact 对象存储已关闭")
        try:
            return await _shielded_thread(operation)
        except asyncio.CancelledError:
            raise
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise BlobObjectNotFoundError() from None
            raise TransientError("Artifact 对象存储暂不可用") from None
        except Exception:  # noqa: BLE001 - connector 边界统一脱敏 SDK/网络错误
            raise TransientError("Artifact 对象存储暂不可用") from None

    async def put(self, object_key: str, content: bytes) -> None:
        key = _require_key(object_key)
        if not isinstance(content, bytes):
            raise ValidationError("Artifact 对象内容无效")
        await self._call(
            lambda: self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
            )
        )

    async def get(self, object_key: str) -> bytes:
        key = _require_key(object_key)

        def read() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            try:
                value = body.read()
                if not isinstance(value, bytes):
                    raise TypeError("object response was not bytes")
                return value
            finally:
                body.close()

        return await self._call(read)

    async def delete(self, object_key: str) -> None:
        key = _require_key(object_key)
        await self._call(
            lambda: self._client.delete_object(Bucket=self._bucket, Key=key)
        )
