"""shared.errors 错误基类与 CurrencyMismatchError 的单测（行为断言，无 mock/Any/type-ignore/密钥数据）。

拦截的变异：
1. test_context_defaults_empty / test_context_is_roundtripped —— 若 `__init__` 未存 context
2. test_context_is_copied_from_input —— 若直接引用外部可变 dict
3. test_subclass_inherits_context —— 若子类不兼容基类的 context 机制
4. test_retryable_flags —— 若 `is_retryable` 被改
5. test_str_is_exactly_message —— 若把 context 拼进消息
6. test_currency_mismatch_error_is_validation_error —— 若 CurrencyMismatchError 挂错层级/可重试
"""
from __future__ import annotations

from shared.errors import (
    CurrencyMismatchError,
    TradeOSError,
    TransientError,
    ValidationError,
)


def test_context_defaults_empty() -> None:
    err = TradeOSError("boom")
    assert err.context == {}


def test_context_is_roundtripped() -> None:
    err = TradeOSError("boom", context={"tenant_id": "t1"})
    assert err.context == {"tenant_id": "t1"}


def test_context_is_copied_from_input() -> None:
    supplied: dict[str, str] = {"tenant_id": "t1"}
    err = TradeOSError("boom", context=supplied)
    supplied["tenant_id"] = "t2"  # 构造后再改入参，不得影响错误对象
    assert err.context == {"tenant_id": "t1"}
    assert err.context is not supplied


def test_subclass_inherits_context() -> None:
    err = ValidationError("boom", context={"tenant_id": "t1"})
    assert err.context == {"tenant_id": "t1"}


def test_retryable_flags() -> None:
    assert TradeOSError("boom").is_retryable is False
    assert ValidationError("boom").is_retryable is False
    assert TransientError("boom").is_retryable is True


def test_str_is_exactly_message() -> None:
    err = TradeOSError("boom", context={"tenant_id": "t1"})
    assert str(err) == "boom"


def test_currency_mismatch_error_is_validation_error() -> None:
    assert issubclass(CurrencyMismatchError, ValidationError)
    assert CurrencyMismatchError("boom").is_retryable is False
