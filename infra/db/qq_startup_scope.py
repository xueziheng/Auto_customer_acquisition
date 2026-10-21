"""QQ 启动专用资源归属；不改变事务、授权或运行期连接池规则。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from time import monotonic
from types import TracebackType
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import asyncpg.connect_utils  # type: ignore[import-untyped]
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, AsyncSessionTransaction

from connectors._deadline_process import CLEANUP_GRACE_SECONDS, PreparationDeadline
from shared.schemas.qq_trial import QQTrialStopped

_current: ContextVar[QQStartupScope | None] = ContextVar("qq_startup_scope", default=None)


def own_cleanup() -> None:
    scope = _current.get()
    if scope is not None:
        task = asyncio.current_task()
        if task is not None:
            scope.tasks.add(task)


def abort_connection(connection: Any) -> None:
    """asyncpg closing 不等于 socket 已关；只中止已明确归属的连接。"""
    scope = _current.get()
    if scope is not None:
        scope.tasks.update(connection._cancellations)
        if scope.deadline.aborted:
            scope.cancel_backend(connection)
    # asyncpg 0.31 的 terminate 会在 protocol closing 时跳过 _abort。
    # 先终止确切 transport，再让原驱动清理其 statement/cancel tasks。
    transport = connection._transport
    if transport is not None:
        transport.abort()
    connection.terminate()


class QQStartupConnection(asyncpg.Connection):
    async def close(self, *, timeout=None):
        own_cleanup()  # 包括 SQLAlchemy shield 创建的 graceful-close task。
        scope, task = _current.get(), asyncio.current_task()
        if scope is not None and task is not None:
            scope.close_tasks.add(task)
        return await super().close(timeout=timeout)


class QQStartupTransaction(AsyncSessionTransaction):
    async def __aexit__(self, type_: type[BaseException] | None,
                        value: BaseException | None, traceback: TracebackType | None) -> None:
        own_cleanup()  # 保留原 sessionmaker.begin 的 shield/go task。
        await super().__aexit__(type_, value, traceback)


class QQStartupSession(AsyncSession):
    def begin(self) -> AsyncSessionTransaction:
        if _current.get() is None:
            return super().begin()
        return QQStartupTransaction(self)

    async def close(self) -> None:
        own_cleanup()  # 保留原 AsyncSession.__aexit__ 创建的 shield/close task。
        await super().close()


def configure_qq_engine(engine: AsyncEngine) -> None:
    """仅 QQ trial composition 调用；共享 engine 工厂及其他 provider 不变。"""
    @event.listens_for(engine.sync_engine, "do_connect")
    def connect(dialect, record, args, kwargs):
        kwargs["connection_class"] = QQStartupConnection
        scope = _current.get()
        if scope is not None:
            kwargs["timeout"] = min(kwargs.get("timeout", 60), scope.deadline.remaining())


class QQStartupScope:
    def __init__(self, engine: AsyncEngine, deadline: PreparationDeadline) -> None:
        self.engine, self.deadline = engine, deadline
        self.tasks: set[asyncio.Task[Any]] = set()
        self.close_tasks: set[asyncio.Task[Any]] = set()
        self.operation: asyncio.Task[None] | None = None
        self.connections: dict[Any, Any] = {}
        self.cleanup_at: float | None = None
        self.cleanup_confirmed = False
        self.backend_cancels: dict[Any, asyncio.Task[bool]] = {}
        self._listeners: list[tuple[str, Any]] = []

    def _listen(self, name: str, callback: Any) -> None:
        event.listen(self.engine.sync_engine, name, callback)
        self._listeners.append((name, callback))

    def _install(self) -> None:
        def checkout(connection, record, proxy):
            if _current.get() is self:
                self.deadline.remaining()
                self.connections[record] = connection.driver_connection
            else:
                # 已归还的连接被无关任务借用后，不再属于本次启动。
                self.connections.pop(record, None)

        def before_statement(connection, cursor, statement, parameters, context, many):
            if _current.get() is self:
                milliseconds = max(1, int(self.deadline.remaining() * 1000))
                cursor.execute(f"SET LOCAL statement_timeout = {milliseconds}")
                self.deadline.remaining()

        def before_commit(connection):
            if _current.get() is self:
                milliseconds = max(1, int(self.deadline.remaining() * 1000))
                cursor = connection.connection.cursor()
                try:
                    cursor.execute(f"SET LOCAL statement_timeout = {milliseconds}")
                finally:
                    cursor.close()
                self.deadline.remaining()

        self._listen("checkout", checkout)
        self._listen("before_cursor_execute", before_statement)
        self._listen("commit", before_commit)
        self.deadline.register(self.abort)

    def cancel_backend(self, connection: Any) -> None:
        """取消原 backend 的命令，再断开 socket；不创建业务会话或重试。"""
        protocol = connection._protocol
        if connection in self.backend_cancels or protocol is None:
            return
        # 必须在 terminate 丢弃 protocol 前捕获；不记录 endpoint/secret。
        kwargs = {"loop": connection._loop, "addr": connection._addr, "params": connection._params,
                  "backend_pid": protocol.backend_pid, "backend_secret": protocol.backend_secret}

        async def cancel() -> bool:
            try:
                async with asyncio.timeout(.5):
                    await asyncpg.connect_utils._cancel(**kwargs)
                return True
            except ConnectionResetError:
                return True  # asyncpg 文档：服务端处理 CancelRequest 后可能直接 reset。
            except (OSError, TimeoutError):
                return False

        task = asyncio.create_task(cancel(), name="qq-startup-backend-cancel")
        self.backend_cancels[connection] = task
        self.tasks.add(task)

    def abort(self) -> None:
        cutoff = min(monotonic(), self.deadline.at) + CLEANUP_GRACE_SECONDS
        self.cleanup_at = min(self.cleanup_at, cutoff) if self.cleanup_at else cutoff
        for record, connection in tuple(self.connections.items()):
            self.tasks.update(connection._cancellations)
            abort_connection(connection)
            # 只失效本次借用的 record；不得 dispose/terminate 整个运行池。
            if record.driver_connection is connection:
                record.invalidate(soft=True)
        # protocol 已进入 closing 时，transport.abort 不会唤醒其 cancel_waiter。
        # 仅取消已跟踪的驱动关闭 task，让原 close 的异常清理结束；不丢弃 shield。
        for task in self.close_tasks:
            if task is not self.operation and not task.done() and not task.cancelling():
                task.cancel()

    async def _drain(self, operation: asyncio.Task[None]) -> None:
        while True:
            pending = {t for t in self.tasks | {operation} if not t.done()}
            if not pending:
                # 允许 connection_lost / SQLAlchemy 后继清理先跑完，再确认一次。
                await asyncio.sleep(0)
                pending = {t for t in self.tasks | {operation} if not t.done()}
                if not pending:
                    break
            cutoff = self.cleanup_at or self.deadline.at + CLEANUP_GRACE_SECONDS
            if monotonic() >= cutoff:
                raise QQTrialStopped("startup_cleanup_unconfirmed")
            await asyncio.wait(pending, timeout=min(.02, cutoff - monotonic()))
        for task in self.tasks | {operation}:
            if not task.cancelled():
                task.exception()
        if (self.cleanup_at is not None and self.deadline.at > self.cleanup_at
                and any(t.cancelled() or t.exception() is not None or not t.result()
                        for t in self.backend_cancels.values())):
            raise QQTrialStopped("startup_cleanup_unconfirmed")
        self.cleanup_confirmed = True

    async def run(self, callback: Callable[[], Awaitable[None]]) -> None:
        token = _current.set(self)
        try:
            self._install()
        except BaseException:
            for name, callback_ in self._listeners:
                event.remove(self.engine.sync_engine, name, callback_)
            _current.reset(token)
            raise
        primary: BaseException | None = None

        async def execute() -> None:
            async with asyncio.timeout(self.deadline.remaining()) as timeout:
                self.deadline.watch(timeout)
                await callback()
                self.deadline.remaining()

        operation = asyncio.create_task(execute(), name="qq-startup-operation")
        self.operation = operation
        try:
            try:
                await asyncio.shield(operation)
            except BaseException as exc:  # noqa: BLE001 - 保留取消并收拢本次启动资源后再传播。
                primary = exc
                self.deadline.abort()
                if not operation.done():
                    operation.cancel()
            drain = asyncio.create_task(self._drain(operation), name="qq-startup-cleanup")
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError as exc:
                    primary = exc
                    self.deadline.abort()
            drain.result()
        finally:
            self.deadline.disarm()
            self.deadline.unregister(self.abort)
            for name, callback_ in self._listeners:
                event.remove(self.engine.sync_engine, name, callback_)
            _current.reset(token)
        if isinstance(primary, asyncio.CancelledError):
            raise primary
        if primary is not None:
            raise QQTrialStopped("preparation_failed") from None
