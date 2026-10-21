"""QQ 准备独占的同身份 DB 连接；截止可终止，不接触 scheduler 锁连接。"""
from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from connectors._deadline_process import PreparationDeadline
from infra.db.qq_startup_scope import (
    QQStartupConnection,
    QQStartupSession,
    abort_connection,
)


class QQPreparationDatabase:
    def __init__(self, url: str, deadline: PreparationDeadline) -> None:
        self.deadline = deadline
        self.connections: list[Any] = []
        parsed = make_url(url).set(drivername="postgresql")

        async def connect():
            remaining = deadline.remaining()
            redact_credentials = False
            connection = await asyncpg.connect(parsed.render_as_string(hide_password=redact_credentials),
                connection_class=QQStartupConnection,
                timeout=remaining, command_timeout=remaining,
                server_settings={"statement_timeout": str(max(1,int(remaining*1000))),
                    "idle_in_transaction_session_timeout": str(max(1,int(remaining*1000)))})
            self.connections.append(connection)
            try:
                deadline.remaining()
            except BaseException:
                abort_connection(connection)
                raise
            return connection

        self.engine = create_async_engine(url, async_creator=connect)
        session_type: type[AsyncSession] = QQStartupSession
        self.sessions = async_sessionmaker(self.engine, class_=session_type, expire_on_commit=False)
        deadline.register(self.abort)

        @event.listens_for(self.engine.sync_engine, "checkout")
        def checked_out(connection, record, proxy):
            deadline.remaining()

        @event.listens_for(self.engine.sync_engine, "before_cursor_execute")
        def before_statement(connection, cursor, statement, parameters, context, many):
            # 仅本准备连接；即使客户端已终止，服务端也不能继续长时间持查询/行锁。
            milliseconds = max(1,int(deadline.remaining()*1000))
            cursor.execute(f"SET LOCAL statement_timeout = {milliseconds}")
            deadline.remaining()

        @event.listens_for(self.engine.sync_engine, "commit")
        def before_commit(connection):
            milliseconds = max(1,int(deadline.remaining()*1000))
            cursor = connection.connection.cursor()
            try:
                cursor.execute(f"SET LOCAL statement_timeout = {milliseconds}")
            finally:
                cursor.close()
            deadline.remaining()

    def abort(self) -> None:
        for connection in self.connections:
            abort_connection(connection)

    async def close(self) -> None:
        self.abort()
        try:
            await self.engine.dispose()
        finally:
            self.deadline.unregister(self.abort)

    @property
    def factory(self) -> async_sessionmaker[AsyncSession]:
        return self.sessions
