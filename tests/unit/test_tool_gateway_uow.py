"""Tool Gateway UoW 的事务与 cleanup 行为契约。"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from shared.schemas.identifiers import TenantId


class _Session:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.commit_error: BaseException | None = None
        self.rollback_error: BaseException | None = None
        self.close_error: BaseException | None = None

    async def commit(self) -> None:
        self.calls.append("commit")
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.calls.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error

    async def close(self) -> None:
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error


def _uow(session: _Session):
    try:
        cls = importlib.import_module(
            "infra.db.tool_gateway_uow"
        ).SqlAlchemyToolGatewayUnitOfWork
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：Tool Gateway UoW 尚未实现（{exc}）")
    return cls(lambda: session, TenantId("tn_uow"))


async def test_clean_exit_commits_once_then_closes() -> None:
    session = _Session()
    async with _uow(session) as uow:
        assert uow.calls is not None
    assert session.calls == ["commit", "close"]


async def test_body_error_rolls_back_and_preserves_primary() -> None:
    session = _Session()
    primary = RuntimeError("primary-secret")
    session.rollback_error = asyncio.CancelledError("rollback-secret")
    session.close_error = RuntimeError("close-secret")

    with pytest.raises(RuntimeError) as caught:
        async with _uow(session):
            raise primary

    assert caught.value is primary
    assert session.calls == ["rollback", "close"]


async def test_commit_error_rolls_back_and_cleanup_never_overrides_primary() -> None:
    session = _Session()
    primary = RuntimeError("commit-secret")
    session.commit_error = primary
    session.rollback_error = asyncio.CancelledError("rollback-secret")
    session.close_error = KeyboardInterrupt("close-secret")

    with pytest.raises(RuntimeError) as caught:
        async with _uow(session):
            pass

    assert caught.value is primary
    assert session.calls == ["commit", "rollback", "close"]


async def test_close_failure_without_primary_propagates() -> None:
    session = _Session()
    close_error = asyncio.CancelledError("close-secret")
    session.close_error = close_error

    with pytest.raises(asyncio.CancelledError) as caught:
        async with _uow(session):
            pass

    assert caught.value is close_error
    assert session.calls == ["commit", "close"]


async def test_each_entry_uses_a_new_session() -> None:
    created: list[_Session] = []

    def factory() -> _Session:
        session = _Session()
        created.append(session)
        return session

    module = importlib.import_module("infra.db.tool_gateway_uow")
    cls = module.SqlAlchemyToolGatewayUnitOfWork
    unit = cls(factory, TenantId("tn_fresh"))
    async with unit:
        pass
    async with unit:
        pass

    assert len(created) == 2
    assert created[0] is not created[1]
    assert [item.calls for item in created] == [
        ["commit", "close"],
        ["commit", "close"],
    ]
