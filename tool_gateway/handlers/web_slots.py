"""公开搜索 task-local 批次与页面快照交接槽。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from connectors.web_search.client import PageSnapshot, WebSearchResult
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.checks.web_discovery import ApprovedSearchBatch

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_SEARCH_HANDLE = re.compile(rf"wsb_{_ULID}")
_PAGE_HANDLE = re.compile(rf"wpb_{_ULID}")


@dataclass(frozen=True, repr=False)
class SearchResultBatch:
    handle: str
    tenant_id: TenantId
    country: str
    category: str
    results: tuple[WebSearchResult, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.handle, str)
            or _SEARCH_HANDLE.fullmatch(self.handle) is None
            or not isinstance(self.tenant_id, str)
            or not self.tenant_id
            or type(self.results) is not tuple
            or len(self.results) > 20
            or any(not isinstance(item, WebSearchResult) for item in self.results)
        ):
            raise ValidationError("搜索结果批次无效")
        ApprovedSearchBatch(
            self.tenant_id,
            self.country,
            self.category,
            tuple(item.url for item in self.results),
        )


class WebSearchResultSlot:
    """同 asyncio task 内保存有界搜索批次，供 page 工具校验来源。"""

    def __init__(
        self,
        id_factory: Callable[[str], str],
        *,
        maximum_batches: int,
    ) -> None:
        if (
            not callable(id_factory)
            or type(maximum_batches) is not int
            or not 1 <= maximum_batches <= 100
        ):
            raise ValidationError("公开搜索批次槽配置无效")
        self._id_factory = id_factory
        self._maximum_batches = maximum_batches
        self._entries: ContextVar[
            tuple[object | None, dict[str, SearchResultBatch]] | None
        ] = ContextVar("tradeos_web_search_batches", default=None)

    def put(
        self,
        tenant_id: TenantId,
        country: str,
        category: str,
        results: tuple[WebSearchResult, ...],
    ) -> SearchResultBatch:
        owner, entries = self._owned_entries()
        if len(entries) >= self._maximum_batches:
            raise ValidationError("公开搜索批次槽已满")
        handle = self._id_factory("wsb")
        batch = SearchResultBatch(
            handle,
            tenant_id,
            country,
            category,
            results,
        )
        copied = dict(entries)
        copied[handle] = batch
        self._entries.set((owner, copied))
        return batch

    def get_batch(self, handle: str) -> SearchResultBatch:
        _owner, entries = self._owned_entries()
        try:
            batch = entries[handle]
        except (KeyError, TypeError):
            raise ValidationError("公开搜索批次句柄无效") from None
        return batch

    def get_approved_batch(self, handle: str) -> ApprovedSearchBatch:
        batch = self.get_batch(handle)
        return ApprovedSearchBatch(
            batch.tenant_id,
            batch.country,
            batch.category,
            tuple(item.url for item in batch.results),
        )

    def discard(self, handle: str) -> None:
        owner, entries = self._owned_entries()
        if handle not in entries:
            raise ValidationError("公开搜索批次句柄无效")
        copied = dict(entries)
        del copied[handle]
        self._entries.set((owner, copied))

    def discard_all(self) -> None:
        self._entries.set(None)

    def _owned_entries(self) -> tuple[object | None, dict[str, SearchResultBatch]]:
        owner = _current_task()
        entry = self._entries.get()
        if entry is None or entry[0] is not owner:
            return owner, {}
        return owner, entry[1]


class WebPageSnapshotSlot:
    """页面正文容量一、领取即删除；数据库只看见 `wpb_` handle。"""

    def __init__(self, id_factory: Callable[[str], str]) -> None:
        if not callable(id_factory):
            raise ValidationError("公开页面快照槽配置无效")
        self._id_factory = id_factory
        self._entry: ContextVar[
            tuple[object | None, str, PageSnapshot] | None
        ] = ContextVar("tradeos_web_page_snapshot", default=None)

    def put(self, snapshot: PageSnapshot) -> str:
        if not isinstance(snapshot, PageSnapshot):
            raise ValidationError("公开页面快照槽结果无效")
        owner = _current_task()
        entry = self._entry.get()
        if entry is not None and entry[0] is owner:
            raise ValidationError("公开页面快照槽已占用")
        handle = self._id_factory("wpb")
        if not isinstance(handle, str) or _PAGE_HANDLE.fullmatch(handle) is None:
            raise ValidationError("公开页面快照句柄无效")
        self._entry.set((owner, handle, snapshot))
        return handle

    def take(self, handle: str) -> PageSnapshot:
        owner = _current_task()
        entry = self._entry.get()
        if (
            not isinstance(handle, str)
            or _PAGE_HANDLE.fullmatch(handle) is None
            or entry is None
            or entry[0] is not owner
            or entry[1] != handle
        ):
            raise ValidationError("公开页面快照句柄无效")
        self._entry.set(None)
        return entry[2]

    def discard_all(self) -> None:
        self._entry.set(None)


def _current_task() -> object | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


__all__ = (
    "SearchResultBatch",
    "WebPageSnapshotSlot",
    "WebSearchResultSlot",
)
