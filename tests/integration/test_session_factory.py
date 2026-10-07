"""S2-2 会话工厂集成测试：SELECT 1 roundtrip 与 get_session context。

RED 前置：``infra.db.session`` 尚未创建；测试函数第一步用 importlib 延迟导入，
把 ModuleNotFoundError 转成 pytest.fail（行为失败），非收集错误。
引擎清理用公开 API：从 ``AsyncSession.bind`` 取引擎并 dispose（不依赖私有字典）；
dispose 放 finally，异常时也释放。
"""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def _load(symbol: str):
    """延迟导入 ``infra.db.session`` 的符号；缺失转成行为失败。"""
    try:
        return getattr(importlib.import_module("infra.db.session"), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：infra.db.session.{symbol} 尚未创建（{exc}）")


async def test_select_one_roundtrip(db_url: str) -> None:
    session_factory = _load("session_factory")
    get_session = _load("get_session")
    sf = session_factory(db_url)
    engine: AsyncEngine | None = None
    try:
        async with get_session(sf) as session:
            engine = session.bind
            assert isinstance(engine, AsyncEngine)
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        if engine is not None:
            await engine.dispose()


async def test_get_session_context(db_url: str) -> None:
    session_factory = _load("session_factory")
    get_session = _load("get_session")
    sf = session_factory(db_url)
    engine: AsyncEngine | None = None
    try:
        async with get_session(sf) as session:
            engine = session.bind
            assert isinstance(engine, AsyncEngine)
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
        assert not session.in_transaction()  # 上下文退出后事务已结束
    finally:
        if engine is not None:
            await engine.dispose()
