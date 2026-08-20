"""集成 DB fixture 契约（本地 PG harness 小任务；纯单元，零 Docker/DB 触发）。

RED 预期：旧 conftest 的 ``db_url(_migrated, _postgres_container)`` 为静态 fixture
依赖（单参调用 TypeError）、``_to_asyncpg`` 不认 asyncpg scheme（ValueError）。
GREEN 后：TEST_DATABASE_URL 已设置时选择逻辑与真实 fixture 均不触碰容器
（spy/哨兵证明）；未设置时保持既有容器 fallback（CI 兼容）；URL 一律 asyncpg。
本文件置于 tests/integration/：同目录 conftest 自然提供 db_url fixture，
无需 pytest_plugins（其跨目录注册与全量收集冲突，CI 实证）。
"""

from __future__ import annotations

import importlib

import pytest

from tests.integration.conftest import RedactedUrl, _resolve_db_url, _to_asyncpg


class _SpyRequest:
    """记录 getfixturevalue 调用；被请求即失败（env 路径不得触碰容器 fixture）。"""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def getfixturevalue(self, name: str):
        self.requested.append(name)
        raise AssertionError(f"TEST_DATABASE_URL 路径不应请求 fixture: {name}")


class _FakeContainer:
    def get_connection_url(self) -> str:
        return "postgresql+psycopg2://tradeos@127.0.0.1:5432/tradeos_test"


class _FallbackRequest:
    """未设置 TEST_DATABASE_URL 时的 fallback 路径：记录请求顺序并返回假容器。"""

    def __init__(self) -> None:
        self.requested: list[str] = []

    def getfixturevalue(self, name: str):
        self.requested.append(name)
        if name == "_migrated":
            return None
        if name == "_postgres_container":
            return _FakeContainer()
        raise KeyError(name)


def test_resolve_db_url_env_priority_never_calls_container_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TEST_DATABASE_URL 已设置 → 直接返回 asyncpg URL，容器 getter 零调用。"""
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://tradeos@127.0.0.1:55432/tradeos_test",
    )
    url = _resolve_db_url(
        "postgresql+asyncpg://tradeos@127.0.0.1:55432/tradeos_test",
        _fail_never_called,
    )
    assert url == "postgresql+asyncpg://tradeos@127.0.0.1:55432/tradeos_test"


def test_resolve_db_url_unset_keeps_container_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未设置 → 惰性调用容器 getter 并转 asyncpg（既有 fallback 语义）。"""
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    url = _resolve_db_url(None, lambda: "postgresql+psycopg2://tradeos@127.0.0.1:5432/tradeos_test")
    assert url == "postgresql+asyncpg://tradeos@127.0.0.1:5432/tradeos_test"


def test_to_asyncpg_conversions() -> None:
    """三种 scheme 转换：psycopg2/裸 postgresql → asyncpg；asyncpg 原样透传。"""
    assert _to_asyncpg("postgresql+psycopg2://u@h:5432/d") == (
        "postgresql+asyncpg://u@h:5432/d"
    )
    assert _to_asyncpg("postgresql://u@h:5432/d") == (
        "postgresql+asyncpg://u@h:5432/d"
    )
    assert _to_asyncpg("postgresql+asyncpg://u@h:5432/d") == (
        "postgresql+asyncpg://u@h:5432/d"
    )
    with pytest.raises(ValueError):
        _to_asyncpg("mysql://u@h:3306/d")


def _fail_never_called() -> str:
    raise AssertionError("TEST_DATABASE_URL 路径不应调用容器 getter")


def test_db_url_fixture_env_path_never_touches_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接验证 fixture 构建逻辑，避免 session 缓存掩盖 env 优先级。"""
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://tradeos@127.0.0.1:55432/tradeos_test",
    )
    conftest = importlib.import_module("tests.integration.conftest")
    fixture_request = _SpyRequest()

    url = conftest._build_db_url(fixture_request)

    assert url == "postgresql+asyncpg://tradeos@127.0.0.1:55432/tradeos_test"
    assert isinstance(url, RedactedUrl)
    assert fixture_request.requested == []
