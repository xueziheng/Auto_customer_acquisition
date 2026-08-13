"""Artifact Store 依赖的最窄对象传输契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.errors import TradeOSError


class BlobObjectNotFoundError(TradeOSError):
    """对象传输层内部的固定不存在信号。"""

    def __init__(self) -> None:
        super().__init__("Artifact 对象不存在")


@runtime_checkable
class ObjectBlobTransport(Protocol):
    """bucket-bound 的不可解释 bytes 传输能力。"""

    async def put(self, object_key: str, content: bytes) -> None: ...

    async def get(self, object_key: str) -> bytes: ...

    async def delete(self, object_key: str) -> None: ...
