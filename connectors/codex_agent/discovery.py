"""仅本次模型调用存活的串行工具会话；复用Gateway的task-owned结果槽。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Literal, Self

from shared.schemas.discovery_diagnostic import DiscoveryDiagnosticError

DISCOVERY_SEARCH_LIMIT = 2
DISCOVERY_TOOL_LIMIT = 7

TOOL_NAMES = ("source_search", "read_public_page", "browse_public_page")
ToolExecutor = Callable[[str, dict], Awaitable[dict]]


class DiscoveryToolFailure(ValueError):
    """装配层显式声明页面失败或可纠正顺序；权限和不确定请求不允许降格。"""

    def __init__(
        self,
        code: Literal[
            "page_unavailable",
            "budget_exhausted",
            "source_read_required",
            "page_id_unknown",
            "search_limit_reached",
        ],
    ) -> None:
        if code not in (
            "page_unavailable",
            "budget_exhausted",
            "source_read_required",
            "page_id_unknown",
            "search_limit_reached",
        ):
            raise ValueError("采集错误类别无效")
        super().__init__("采集工具未取得材料")
        self.code = code

    def result(self) -> dict:
        return {"error": {"code": self.code, "retryable": False}}


class DiscoverySession:
    """调用者只能注入已经授权的Gateway适配器，不允许直接外部transport。"""

    def __init__(
        self,
        execute: ToolExecutor,
        *,
        max_requests: int,
        timeout_seconds: int,
        max_result_bytes: int,
        remaining_searches: Callable[[], int] | None = None,
    ) -> None:
        if any(
            type(v) is not int or v <= 0
            for v in (max_requests, timeout_seconds, max_result_bytes)
        ):
            raise ValueError("采集预算无效")
        self._remaining_searches = remaining_searches
        self.execute = execute
        self.max_requests = max_requests
        self.timeout_seconds = timeout_seconds
        self.max_result_bytes = max_result_bytes
        self._used = 0
        self._exhausted = False
        self._closed = True
        self._queue: asyncio.Queue[tuple[str, dict, asyncio.Future]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._deadline = 0.0
        self._reserved_by_tool = dict.fromkeys(TOOL_NAMES, 0)
        self._outcomes = dict.fromkeys(
            (
                "succeeded",
                "search_limit_reached",
                "source_read_required",
                "page_id_unknown",
                "page_unavailable",
                "budget_exhausted",
                "failed_closed",
            ),
            0,
        )
        self._queued = 0
        self._inflight = 0
        self._cancelled = 0
        self._unreserved = {"unreserved_budget": 0, "unreserved_deadline": 0}
        self._unreserved_overflow = False

    async def __aenter__(self) -> Self:
        if self._worker is not None:
            raise ValueError("采集会话不可复用")
        self._closed = False
        self._deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        self._worker = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        """取消会停止当前外部调用并销毁整次会话，不能换请求继续。"""
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        while not self._queue.empty():
            _, _, future = self._queue.get_nowait()
            self._queued -= 1
            self._cancelled += 1
            future.cancel()

    async def call(self, name: str, arguments: dict) -> dict:
        """预算在派发前扣除，任何不确定结果均不退款。"""
        if self._closed or name not in TOOL_NAMES or not isinstance(arguments, dict):
            raise ValueError("采集工具或预算无效")
        if (
            self._used >= self.max_requests
            or asyncio.get_running_loop().time() >= self._deadline
        ):
            reason = (
                "unreserved_budget"
                if self._used >= self.max_requests
                else "unreserved_deadline"
            )
            if self._unreserved[reason] < 100:
                self._unreserved[reason] += 1
            else:
                self._unreserved_overflow = True
            return self._with_budget(DiscoveryToolFailure("budget_exhausted").result())
        self._used += 1
        self._reserved_by_tool[name] += 1
        self._queued += 1
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((name, arguments, future))
        try:
            async with asyncio.timeout_at(self._deadline):
                return await future
        except (asyncio.CancelledError, TimeoutError):
            await self.close()
            raise

    def usage_snapshot(self) -> dict[str, int | bool]:
        """只返回固定计数；装配层向领域严格摘要投影，不包含工具输入输出。"""
        return {
            "tool_limit": self.max_requests,
            "reserved": sum(self._reserved_by_tool.values()),
            **self._reserved_by_tool,
            "queued": self._queued,
            "inflight": self._inflight,
            "terminal": sum(self._outcomes.values()),
            "cancelled": self._cancelled,
            **self._outcomes,
            **self._unreserved,
            "unreserved_overflow": self._unreserved_overflow,
        }

    def _with_budget(self, result: dict) -> dict:
        """库存组合显式启用；已预留及失败调用均扣除；超时无可用工具，不退款。"""
        if self._remaining_searches is None:
            return result
        return {
            **result,
            "meta": {
                "remaining_tools": (
                    0
                    if asyncio.get_running_loop().time() >= self._deadline
                    else max(0, self.max_requests - self._used)
                ),
                "remaining_searches": self._remaining_searches(),
            },
        }

    async def _run(self) -> None:
        while True:
            name, arguments, future = await self._queue.get()
            self._queued -= 1
            self._inflight = 1
            try:
                if self._closed:
                    raise ValueError("采集会话已关闭")
                if (
                    self._exhausted
                    or asyncio.get_running_loop().time() >= self._deadline
                ):
                    raise DiscoveryToolFailure("budget_exhausted")
                result = await self.execute(name, arguments)
                if isinstance(result, dict):
                    result = self._with_budget(result)
                if (
                    not isinstance(result, dict)
                    or len(json.dumps(result).encode()) > self.max_result_bytes
                ):
                    raise ValueError("采集工具输出超限")
                self._outcomes["succeeded"] += 1
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                self._cancelled += 1
                future.cancel()
                raise
            except DiscoveryToolFailure as failure:
                self._outcomes[failure.code] += 1
                if failure.code == "budget_exhausted":
                    self._used = self.max_requests
                    self._exhausted = True
                if not future.done():
                    future.set_result(self._with_budget(failure.result()))
            except DiscoveryDiagnosticError as failure:
                self._outcomes["failed_closed"] += 1
                self._closed = True
                if not future.done():
                    future.set_exception(failure)
            except Exception:  # noqa: BLE001 -- 外部数据只返回固定安全类别。
                self._outcomes["failed_closed"] += 1
                self._closed = True
                if not future.done():
                    future.set_exception(ValueError("采集工具失败"))
            finally:
                self._inflight = 0
