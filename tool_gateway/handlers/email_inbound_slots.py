"""局部task-owned容量一结果槽，子task不能领取或清理父task结果。"""

import asyncio
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from shared.schemas.email_inbound import ArchivedInboundPage, InboundError
from shared.schemas.identifiers import new_id


@dataclass(frozen=True, repr=False)
class _Entry:
    owner: asyncio.Task[Any]
    handle: str
    page: ArchivedInboundPage


class InboundPageSlot:
    def __init__(self) -> None:
        self._entry: ContextVar[_Entry | None] = ContextVar(
            "inbound_page", default=None
        )

    def _current(self) -> _Entry | None:
        entry = self._entry.get()
        return (
            entry
            if entry is not None and entry.owner is asyncio.current_task()
            else None
        )

    @property
    def is_empty(self) -> bool:
        return self._current() is None

    def put(self, page: ArchivedInboundPage) -> str:
        owner = asyncio.current_task()
        if (
            owner is None
            or not self.is_empty
            or not isinstance(page, ArchivedInboundPage)
        ):
            raise InboundError()
        handle = new_id("ipg")
        self._entry.set(_Entry(owner, handle, page))
        return handle

    def take(self, handle: str) -> ArchivedInboundPage:
        entry = self._current()
        if entry is None:
            raise InboundError()
        # 同task任何领取尝试都消耗本槽，错handle不能留下可重放内容。
        self._entry.set(None)
        if (
            not isinstance(handle, str)
            or re.fullmatch(r"ipg_[0-7][0-9A-HJKMNP-TV-Z]{25}", handle) is None
            or entry.handle != handle
        ):
            raise InboundError()
        return entry.page

    def discard_all(self) -> None:
        if self._current() is not None:
            self._entry.set(None)
