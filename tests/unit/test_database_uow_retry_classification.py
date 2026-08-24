"""审批与合规 UoW 仅把可识别的暂态数据库故障转换为脱敏错误。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from sqlalchemy.exc import (
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
)
from sqlalchemy.exc import (
    TimeoutError as SqlAlchemyTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.compliance_uow import SqlAlchemyComplianceUnitOfWork
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import TenantId

_SAFE_MESSAGE = "数据库事务暂时不可用"


class _Session:
    def __init__(self, commit_error: BaseException | None = None) -> None:
        self.commit_error = commit_error
        self.rollback_calls = 0
        self.close_calls = 0

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class _SqlStateError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__("postgres://user:sensitive@db/private")
        self.sqlstate = sqlstate


UowFactory = Callable[
    [async_sessionmaker[AsyncSession], TenantId],
    SqlAlchemyApprovalUnitOfWork | SqlAlchemyComplianceUnitOfWork,
]


@pytest.fixture(params=[SqlAlchemyApprovalUnitOfWork, SqlAlchemyComplianceUnitOfWork])
def uow_factory(request: pytest.FixtureRequest) -> UowFactory:
    return cast(UowFactory, request.param)


def _session_factory(session: _Session) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(Any, lambda: session))


def _operational_error() -> OperationalError:
    return OperationalError(
        "SELECT postgres://user:sensitive@db/private",
        {},
        RuntimeError("database password sensitive"),
    )


def _interface_error() -> InterfaceError:
    return InterfaceError(
        "SELECT postgres://user:sensitive@db/private",
        {},
        RuntimeError("socket sensitive"),
    )


def _sqlstate_error(sqlstate: str) -> DBAPIError:
    return DBAPIError(
        "SELECT sensitive",
        {},
        _SqlStateError(sqlstate),
        connection_invalidated=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        _operational_error,
        _interface_error,
        lambda: SqlAlchemyTimeoutError("pool password sensitive"),
        lambda: DBAPIError(
            "SELECT sensitive",
            {},
            RuntimeError("connection lost sensitive"),
            connection_invalidated=True,
        ),
        lambda: _sqlstate_error("40001"),
        lambda: _sqlstate_error("40P01"),
    ],
    ids=[
        "operational",
        "interface",
        "pool-timeout",
        "connection-invalidated",
        "serialization",
        "deadlock",
    ],
)
async def test_uow_translates_only_recognized_retryable_database_failures(
    uow_factory: UowFactory,
    error_factory: Callable[[], BaseException],
) -> None:
    session = _Session()
    uow = uow_factory(_session_factory(session), TenantId("tn_retry"))

    with pytest.raises(TransientError, match=f"^{_SAFE_MESSAGE}$") as caught:
        async with uow:
            raise error_factory()

    assert "sensitive" not in str(caught.value)
    assert session.rollback_calls == 1
    assert session.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: IntegrityError(
            "INSERT sensitive", {}, RuntimeError("duplicate sensitive")
        ),
        lambda: _sqlstate_error("23505"),
        lambda: DBAPIError(
            "SELECT sensitive",
            {},
            RuntimeError("unknown sensitive"),
            connection_invalidated=False,
        ),
        lambda: ValidationError("validation sensitive"),
        lambda: RuntimeError("programming sensitive"),
    ],
    ids=[
        "integrity",
        "non-retryable-sqlstate",
        "generic-dbapi",
        "validation",
        "programming",
    ],
)
async def test_uow_preserves_non_retryable_failures(
    uow_factory: UowFactory,
    error_factory: Callable[[], BaseException],
) -> None:
    session = _Session()
    uow = uow_factory(_session_factory(session), TenantId("tn_permanent"))
    expected = error_factory()

    with pytest.raises(type(expected)) as caught:
        async with uow:
            raise expected

    assert caught.value is expected
    assert session.rollback_calls == 1
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_uow_translates_retryable_commit_failure_after_rollback(
    uow_factory: UowFactory,
) -> None:
    session = _Session(commit_error=_operational_error())
    uow = uow_factory(_session_factory(session), TenantId("tn_commit_retry"))

    with pytest.raises(TransientError, match=f"^{_SAFE_MESSAGE}$") as caught:
        async with uow:
            pass

    assert "sensitive" not in str(caught.value)
    assert session.rollback_calls == 1
    assert session.close_calls == 1
