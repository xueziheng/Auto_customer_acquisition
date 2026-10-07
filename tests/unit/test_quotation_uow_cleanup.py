"""真实报价UoW的取消主异常优先级与受控session清理故障。"""

import asyncio
from collections.abc import Sequence
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.quotations.errors import QuotationUnavailableError
from infra.db.quotation_uow import SqlAlchemyQuotationUow
from shared.schemas.identifiers import TenantId, new_id


def cleanup_case(
    failing: Sequence[str], error_type: type[BaseException] = SQLAlchemyError,
    *, tenant_id: TenantId | None = None,
) -> tuple[SqlAlchemyQuotationUow, AsyncMock, list[str]]:
    session = AsyncMock(spec=AsyncSession)
    # begin本身同步，但其真实返回值AsyncSessionTransaction可await。
    session.begin = AsyncMock()
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
    uow = SqlAlchemyQuotationUow(
        cast(async_sessionmaker[AsyncSession], Mock(return_value=session)),
        tenant_id or new_id("tn"), lock_timeout_ms=100, statement_timeout_ms=200,
    )
    return uow, session, order


@pytest.mark.parametrize("point", ["begin", "lock_timeout", "statement_timeout", "exit"])
@pytest.mark.parametrize("failing", [("rollback",), ("close",), ("rollback", "close")])
@pytest.mark.parametrize("error_type", [SQLAlchemyError, RuntimeError, asyncio.CancelledError])
async def test_primary_cancellation_survives_every_cleanup_failure(
    point: str, failing: tuple[str, ...], error_type: type[BaseException], caplog,
) -> None:
    uow, session, order = cleanup_case(failing, error_type)
    primary = asyncio.CancelledError("private-primary-detail")
    if point == "begin":
        session.begin.side_effect = primary
    elif point == "lock_timeout":
        session.execute.side_effect = primary
    elif point == "statement_timeout":
        session.execute.side_effect = [None, primary]

    with pytest.raises(asyncio.CancelledError) as error:
        async with uow:
            assert point == "exit"
            raise primary

    assert error.value is primary
    assert order == ["rollback", "close"]
    session.commit.assert_not_awaited()
    records = [r for r in caplog.records if r.name == "infra.db.quotation_uow"]
    assert len(records) == len(failing)
    assert all(r.getMessage() == "报价事务取消后的清理失败" for r in records)
    assert all(not r.args and r.exc_info is None for r in records)
    assert "private-" not in caplog.text


@pytest.mark.parametrize("point", ["begin", "exit"])
async def test_primary_cancellation_with_successful_cleanup_is_unchanged(point: str) -> None:
    uow, session, order = cleanup_case(())
    primary = asyncio.CancelledError()
    if point == "begin":
        session.begin.side_effect = primary
    with pytest.raises(asyncio.CancelledError) as error:
        async with uow:
            raise primary
    assert error.value is primary
    assert order == ["rollback", "close"]


@pytest.mark.parametrize("failing", [("rollback",), ("close",), ("rollback", "close")])
async def test_cleanup_without_primary_cancellation_still_fails_closed(
    failing: tuple[str, ...],
) -> None:
    uow, _, order = cleanup_case(failing)
    with pytest.raises(QuotationUnavailableError) as error:
        async with uow:
            pass
    assert error.value.code == "storage_unknown"
    assert "private-" not in str(error.value)
    assert order == ["rollback", "close"]


@pytest.mark.parametrize("committed", [False, True])
async def test_normal_exit_keeps_explicit_commit_and_rollback_semantics(committed: bool) -> None:
    uow, session, order = cleanup_case(())
    async with uow:
        if committed:
            await uow.commit()
    assert order == (["close"] if committed else ["rollback", "close"])
    assert session.commit.await_count == int(committed)


async def test_cancellation_after_commit_does_not_rollback_and_survives_close_failure() -> None:
    uow, session, order = cleanup_case(("close",))
    primary = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as error:
        async with uow:
            await uow.commit()
            raise primary
    assert error.value is primary
    assert order == ["close"]
    session.commit.assert_awaited_once()


async def test_entry_sql_error_without_cancellation_still_maps_to_fixed_unavailable() -> None:
    uow, session, order = cleanup_case(())
    session.begin.side_effect = SQLAlchemyError("private-begin-detail")
    with pytest.raises(QuotationUnavailableError) as error:
        async with uow:
            pytest.fail("failed begin must not enter body")
    assert error.value.code == "storage_unknown"
    assert "private-" not in str(error.value)
    assert order == ["rollback", "close"]
