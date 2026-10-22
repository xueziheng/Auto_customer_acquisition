"""仅 QQ 准备使用的对象传输；原 Artifact 编排不变，同一截止点内隔离 SDK。"""
from dataclasses import asdict

from artifact_store.transport import BlobObjectNotFoundError, BlobReadLimitExceeded
from connectors._deadline_process import (
    DeadlineProcess,
    PreparationDeadline,
    PreparationIOError,
)
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import ObjectStoreSecretResolver, _require_key
from shared.errors import TransientError


class QQPreparationObjects:
    def __init__(self, settings: S3ObjectStoreSettings, resolver: ObjectStoreSecretResolver,
                 deadline: PreparationDeadline) -> None:
        self.settings, self.resolver, self.deadline = settings, resolver, deadline
        self.worker: DeadlineProcess | None = None

    async def _call(self, operation: str, key: str, *args):
        key = _require_key(key)
        self.deadline.remaining()
        if self.worker is None:
            self.worker = DeadlineProcess(self.deadline, "objects")
            await self.worker.call("open", asdict(self.settings),
                self.resolver.resolve(self.settings.access_key_ref),
                self.resolver.resolve(self.settings.secret_key_ref))
        try:
            return await self.worker.call(operation, key, *args)
        except PreparationIOError as error:
            if error.code == "not_found":
                raise BlobObjectNotFoundError() from None
            if error.code == "limit":
                raise BlobReadLimitExceeded() from None
            raise TransientError("Artifact 对象存储暂不可用") from None

    async def put(self, object_key: str, content: bytes) -> None:
        if not isinstance(content, bytes) or not 0 < len(content) <= 16384:
            raise ValueError("invalid preparation object")
        await self._call("put", object_key, content)

    async def get(self, object_key: str) -> bytes:
        raise ValueError("unbounded preparation read forbidden")

    async def get_bounded(self, object_key: str, *, maximum_bytes: int) -> bytes:
        if type(maximum_bytes) is not int or not 0 < maximum_bytes <= 16384:
            raise ValueError("invalid preparation object limit")
        content = await self._call("get", object_key, maximum_bytes)
        if not isinstance(content, bytes) or len(content) > maximum_bytes:
            raise ValueError("invalid preparation object")
        return content

    async def delete(self, object_key: str) -> None:
        await self._call("delete", object_key)

    async def aclose(self, *, abort: bool = False) -> None:
        if self.worker is not None:
            try:
                if not abort and not self.deadline.aborted:
                    await self.worker.call("close")
            finally:
                self.worker.close()
                self.worker = None
