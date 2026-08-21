"""async task-local、容量一、领取即删除的 typed 结果槽。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextvars import ContextVar

from shared.errors import ValidationError

_PREFIX_RE = re.compile(r"(?:ceb|veb)")
_HANDLE_RE = re.compile(r"(?:ceb|veb)_[0-7][0-9A-HJKMNP-TV-Z]{25}")


class ContextLocalSingleResultSlot[T]:
    """每个 asyncio context 独立的单结果槽；handle 只能领取一次。"""

    def __init__(
        self,
        handle_prefix: str,
        id_factory: Callable[[str], str],
    ) -> None:
        if (
            not isinstance(handle_prefix, str)
            or _PREFIX_RE.fullmatch(handle_prefix) is None
            or not callable(id_factory)
        ):
            raise ValidationError("single result slot 配置无效")
        self._handle_prefix = handle_prefix
        self._id_factory = id_factory
        self._entry: ContextVar[tuple[object | None, str, T] | None] = ContextVar(
            f"tradeos_{handle_prefix}_single_result",
            default=None,
        )

    @property
    def is_empty(self) -> bool:
        entry = self._entry.get()
        return entry is None or entry[0] is not _current_task()

    def put(self, value: T) -> str:
        owner = _current_task()
        entry = self._entry.get()
        if entry is not None and entry[0] is owner:
            raise ValidationError("single result slot 已占用")
        handle = self._id_factory(self._handle_prefix)
        if (
            not isinstance(handle, str)
            or _HANDLE_RE.fullmatch(handle) is None
            or not handle.startswith(f"{self._handle_prefix}_")
        ):
            raise ValidationError("single result handle 无效")
        self._entry.set((owner, handle, value))
        return handle

    def take(self, handle: str) -> T:
        owner = _current_task()
        entry = self._entry.get()
        if (
            not isinstance(handle, str)
            or _HANDLE_RE.fullmatch(handle) is None
            or entry is None
            or entry[0] is not owner
            or entry[1] != handle
        ):
            raise ValidationError("single result handle 无效")
        self._entry.set(None)
        return entry[2]

    def discard_all(self) -> None:
        self._entry.set(None)


def _current_task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


__all__ = ("ContextLocalSingleResultSlot",)
