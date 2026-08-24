"""SQLAlchemy 事务边界的窄范围暂态故障识别与脱敏。"""

from __future__ import annotations

from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import (
    TimeoutError as SqlAlchemyTimeoutError,
)

from shared.errors import TransientError

_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
TRANSIENT_DATABASE_MESSAGE = "数据库事务暂时不可用"


def is_retryable_database_error(error: BaseException) -> bool:
    """只识别连接、超时和 PostgreSQL 明确可重试事务故障。"""
    if isinstance(error, IntegrityError):
        return False
    if isinstance(error, (OperationalError, InterfaceError, SqlAlchemyTimeoutError)):
        return True
    if not isinstance(error, DBAPIError):
        return False
    if error.connection_invalidated:
        return True
    sqlstate = getattr(error.orig, "sqlstate", None)
    if sqlstate is None:
        sqlstate = getattr(error.orig, "pgcode", None)
    return sqlstate in _RETRYABLE_SQLSTATES


def raise_transient_database_error(error: BaseException) -> None:
    """识别到暂态数据库故障时抛固定消息，不传播驱动异常内容。"""
    if is_retryable_database_error(error):
        raise TransientError(TRANSIENT_DATABASE_MESSAGE) from None


__all__ = (
    "TRANSIENT_DATABASE_MESSAGE",
    "is_retryable_database_error",
    "raise_transient_database_error",
)
