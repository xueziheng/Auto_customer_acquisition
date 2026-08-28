"""Artifact Store 依赖的最窄对象传输契约。"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from shared.errors import TradeOSError


class QuotePdfBlobTransportError(TradeOSError):
    """专用写入结果分类，禁止携带SDK原文或对象地址。"""

    def __init__(self, code: Literal["invalid_input", "read_unsupported", "unavailable", "outcome_unknown"]) -> None:
        messages = {"invalid_input": "报价文件对象输入无效", "read_unsupported": "报价文件对象须使用有界读取",
            "unavailable": "报价文件对象传输不可用", "outcome_unknown": "报价文件对象操作结果未知"}
        if code not in messages:
            raise ValueError("无效报价对象错误码")
        self.code = code
        super().__init__(messages[code])


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
