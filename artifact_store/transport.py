"""Artifact Store 依赖的最窄对象传输契约。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.errors import TradeOSError


class BlobReadLimitExceeded(TradeOSError):
    """有限流超过本次上限；不得被转换为重试。"""

    def __init__(self) -> None:
        super().__init__("Artifact 对象超过读取限制")


class BoundedObjectBlobTransport(Protocol):
    """有限读取能力，与旧全量get互不回退。"""

    async def get_bounded(self, object_key: str, *, maximum_bytes: int) -> bytes:
        """最多读取上限加一个哨兵，超限拒绝且关闭流。"""
        ...


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
