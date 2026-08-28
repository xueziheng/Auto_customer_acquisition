"""来源结果的同task一次性交接，无持久化原文。"""

import asyncio
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

from shared.schemas.evidence_read import (
    EvidenceTextResult,
    QuoteEvidenceError,
    QuoteEvidenceErrorCode,
)


@dataclass(frozen=True, repr=False)
class _Entry:
    owner: asyncio.Task
    handle: str | None
    result: EvidenceTextResult | None
    failure: QuoteEvidenceErrorCode | None


class QuoteEvidenceResultSlot:
    """容量一，成功结果与固定失败码互斥。"""

    def __init__(self, id_factory: Callable[[str], str]) -> None:
        self._ids = id_factory
        self._entry: ContextVar[_Entry | None] = ContextVar(
            "quote_evidence_result", default=None
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
        """仅当前task的占用状态，子task继承的内容不算自己的。"""
        return self._current() is None

    def put(self, result: EvidenceTextResult) -> str:
        """只保留当前task一个结果并生成canonical一次性handle。"""
        owner = asyncio.current_task()
        if (
            owner is None
            or not self.is_empty
            or not isinstance(result, EvidenceTextResult)
        ):
            raise QuoteEvidenceError("gateway_unavailable")
        handle = self._ids("qev")
        if (
            type(handle) is not str
            or re.fullmatch(r"qev_[0-7][0-9A-HJKMNP-TV-Z]{25}", handle) is None
        ):
            raise QuoteEvidenceError("gateway_unavailable")
        self._entry.set(_Entry(owner, handle, result, None))
        return handle

    def take(self, handle: str) -> EvidenceTextResult:
        """同task同handle领取即删，不能从ledger恢复内容。"""
        entry = self._current()
        if entry is None or entry.handle != handle or entry.result is None:
            raise QuoteEvidenceError("gateway_unavailable")
        self._entry.set(None)
        return entry.result

    def put_failure(self, code: QuoteEvidenceErrorCode) -> None:
        """失败仅固定code，不接收异常、原文或上下文字典。"""
        QuoteEvidenceError(code)
        owner = asyncio.current_task()
        if owner is None or not self.is_empty:
            raise QuoteEvidenceError("gateway_unavailable")
        self._entry.set(_Entry(owner, None, None, code))

    def take_failure(self) -> QuoteEvidenceErrorCode | None:
        """非成功路径只取细码，绝不领取或删除成功payload。"""
        entry = self._current()
        if entry is None or entry.failure is None:
            return None
        self._entry.set(None)
        return entry.failure

    def discard_all(self) -> None:
        """仅清当前task条目，子task不能删除父条目。"""
        if self._current() is not None:
            self._entry.set(None)
