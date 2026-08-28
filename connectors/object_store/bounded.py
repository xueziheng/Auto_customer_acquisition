"""专用来源有界S3读取；与旧写入补偿线程互不影响。"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from artifact_store.transport import BlobObjectNotFoundError, BlobReadLimitExceeded
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import ObjectStoreSecretResolver, _require_key
from shared.errors import TransientError, ValidationError
from shared.schemas.evidence_read import ObjectReadLimits


class S3BoundedObjectBlobTransport:
    """构造无IO，执行才解析凭证与有限读取。"""

    def __init__(
        self,
        settings: S3ObjectStoreSettings,
        secret_resolver: ObjectStoreSecretResolver,
        *,
        limits: ObjectReadLimits,
    ) -> None:
        self._settings = settings
        self._secret_resolver = secret_resolver
        self._limits = ObjectReadLimits.model_validate(limits.model_dump())

    def __repr__(self) -> str:
        return "S3BoundedObjectBlobTransport()"

    async def get_bounded(self, object_key: str, *, maximum_bytes: int) -> bytes:
        """先固定有限读取预算再执行一次来源请求。"""
        key = _require_key(object_key)
        if type(maximum_bytes) is not int or maximum_bytes <= 0:
            raise ValidationError("Artifact 读取限制无效")
        stopped = threading.Event()
        deadline = time.monotonic() + self._limits.total_timeout_ms / 1000

        def check() -> None:
            if stopped.is_set() or time.monotonic() >= deadline:
                raise TransientError("Artifact 对象存储暂不可用")

        def read() -> bytes:
            client: Any = None
            body: Any = None
            try:
                check()
                access = self._secret_resolver.resolve(self._settings.access_key_ref)
                secret = self._secret_resolver.resolve(self._settings.secret_key_ref)
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
                            retries={
                                "total_max_attempts": self._limits.maximum_attempts,
                                "mode": "standard",
                            },
                        ),
                    )
                finally:
                    del access, secret
                check()
                response = client.get_object(Bucket=self._settings.bucket, Key=key)
                body = response["Body"]
                check()
                content = bytearray()
                while True:
                    check()
                    count = min(
                        self._limits.chunk_bytes, maximum_bytes + 1 - len(content)
                    )
                    chunk = body.read(count)
                    check()
                    if type(chunk) is not bytes or len(chunk) > count:
                        raise TransientError("Artifact 对象存储暂不可用")
                    if not chunk:
                        return bytes(content)
                    content.extend(chunk)
                    if len(content) > maximum_bytes:
                        raise BlobReadLimitExceeded()
            finally:
                try:
                    if body is not None:
                        body.close()
                finally:
                    if client is not None:
                        client.close()

        task = asyncio.create_task(asyncio.to_thread(read))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 - 次级SDK异常不能覆盖取消
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise
        except BlobReadLimitExceeded:
            raise BlobReadLimitExceeded() from None
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "NoSuchKey",
                "404",
                "NotFound",
            }:
                raise BlobObjectNotFoundError() from None
            raise TransientError("Artifact 对象存储暂不可用") from None
        except Exception:  # noqa: BLE001 - SDK、认证、流及初始化一律固定脱敏
            raise TransientError("Artifact 对象存储暂不可用") from None
