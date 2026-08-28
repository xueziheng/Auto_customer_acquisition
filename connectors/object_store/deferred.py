"""旧上传传输的惰性装配；报价来源和PDF不得回退到此旧无界读口。"""

from __future__ import annotations

import asyncio

from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import (
    ObjectStoreSecretResolver,
    S3ObjectBlobTransport,
    _require_key,
)
from shared.errors import TransientError, ValidationError


class DeferredS3ObjectBlobTransport:
    """首个合法调用才创建唯一旧delegate，不改变其线程收口与补偿语义。"""

    def __init__(
        self,
        settings: S3ObjectStoreSettings,
        secret_resolver: ObjectStoreSecretResolver,
    ) -> None:
        self._settings = settings
        self._secret_resolver = secret_resolver
        self._delegate: S3ObjectBlobTransport | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "DeferredS3ObjectBlobTransport()"

    async def _get_delegate(self) -> S3ObjectBlobTransport:
        async with self._lock:
            if self._delegate is None:
                try:
                    delegate = S3ObjectBlobTransport(
                        self._settings, self._secret_resolver
                    )
                except Exception:  # noqa: BLE001 - resolver与SDK初始化均固定脱敏
                    raise TransientError("Artifact 对象存储暂不可用") from None
                self._delegate = delegate
            return self._delegate

    async def put(self, object_key: str, content: bytes) -> None:
        key = _require_key(object_key)
        if not isinstance(content, bytes):
            raise ValidationError("Artifact 对象内容无效")
        delegate = await self._get_delegate()
        await delegate.put(key, content)

    async def get(self, object_key: str) -> bytes:
        key = _require_key(object_key)
        delegate = await self._get_delegate()
        return await delegate.get(key)

    async def delete(self, object_key: str) -> None:
        key = _require_key(object_key)
        delegate = await self._get_delegate()
        await delegate.delete(key)
