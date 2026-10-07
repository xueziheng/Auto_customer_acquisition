"""Need unit UoW 进入取消时的首异常优先级与受控清理故障。"""

import asyncio
from collections.abc import Sequence
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.demand.errors import NeedUnitUnavailableError
from infra.db.need_unit_uow import SqlAlchemyNeedUnitUnitOfWork
from shared.schemas.identifiers import new_id


class _TerminalSignal(BaseException):
    """代表不得转换为存储错误的非 Exception 终止信号。"""


def cleanup_case(
    failing: Sequence[str],
    error_type: type[BaseException],
) -> tuple[SqlAlchemyNeedUnitUnitOfWork, AsyncMock, list[str]]:
    session = AsyncMock(spec=AsyncSession)
    order: list[str] = []

    async def rollback() -> None:
        order.append("rollback")
        if "rollback" in failing:
            raise error_type("private-rollback-detail")

    async def close() -> None:
        order.append("close")
        if "close" in failing:
            raise error_type("private-close-detail")

    session.rollback.side_effect = rollback
    session.close.side_effect = close
    uow = SqlAlchemyNeedUnitUnitOfWork(
        cast(async_sessionmaker[AsyncSession], Mock(return_value=session)),
        new_id("tn"),
        lock_timeout_ms=100,
        statement_timeout_ms=200,
    )
    return uow, session, order


@pytest.mark.parametrize("point", ["lock_timeout", "statement_timeout"])
@pytest.mark.parametrize("failing", [("rollback",), ("close",), ("rollback", "close")])
@pytest.mark.parametrize(
    "cleanup_error_type", [SQLAlchemyError, RuntimeError, asyncio.CancelledError]
)
async def test_entry_cancellation_survives_every_cleanup_failure(
    point: str,
    failing: tuple[str, ...],
    cleanup_error_type: type[BaseException],
    caplog,
) -> None:
    uow, session, order = cleanup_case(failing, cleanup_error_type)
    primary = asyncio.CancelledError("private-primary-detail")
    session.execute.side_effect = (
        primary if point == "lock_timeout" else [None, primary]
    )

    with pytest.raises(asyncio.CancelledError) as error:
        async with uow:
            pytest.fail("set_config 取消后不得进入业务体")

    assert error.value is primary
    assert session.execute.await_count == (1 if point == "lock_timeout" else 2)
    assert order == ["rollback", "close"]
    records = [r for r in caplog.records if r.name == "infra.db.need_unit_uow"]
    assert len(records) == len(failing)
    assert all(r.getMessage() == "需求单位事务进入失败后的清理失败" for r in records)
    assert all(not r.args and r.exc_info is None for r in records)
    assert "private-" not in caplog.text


@pytest.mark.parametrize("point", ["lock_timeout", "statement_timeout"])
async def test_entry_preserves_other_terminal_signal(point: str, caplog) -> None:
    uow, session, order = cleanup_case(
        ("rollback", "close"), asyncio.CancelledError
    )
    primary = _TerminalSignal("private-terminal-detail")
    session.execute.side_effect = (
        primary if point == "lock_timeout" else [None, primary]
    )

    with pytest.raises(_TerminalSignal) as error:
        async with uow:
            pytest.fail("终止信号后不得进入业务体")

    assert error.value is primary
    assert order == ["rollback", "close"]
    assert "private-" not in caplog.text


async def test_entry_exception_maps_original_failure_after_best_effort_cleanup(
    caplog,
) -> None:
    uow, session, order = cleanup_case(
        ("rollback", "close"), asyncio.CancelledError
    )
    session.execute.side_effect = RuntimeError("private-primary-detail")

    with pytest.raises(NeedUnitUnavailableError) as error:
        async with uow:
            pytest.fail("set_config 失败后不得进入业务体")

    assert error.value.code == "storage_unknown"
    assert "private-" not in str(error.value)
    assert order == ["rollback", "close"]
    records = [r for r in caplog.records if r.name == "infra.db.need_unit_uow"]
    assert len(records) == 2
    assert all(not r.args and r.exc_info is None for r in records)
    assert "private-" not in caplog.text
