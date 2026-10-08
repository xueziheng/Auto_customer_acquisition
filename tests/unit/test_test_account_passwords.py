"""显式测试账号策略不放宽普通账号和重置密码要求。"""

import secrets

import pytest
from pydantic import SecretStr

from infra.authentication.passwords import (
    hash_password,
    hash_test_password,
    verify_password,
)
from shared.authentication import AuthenticationInputInvalid


def test_short_password_requires_explicit_test_record() -> None:
    password = SecretStr(secrets.token_hex(3))
    with pytest.raises(AuthenticationInputInvalid):
        hash_password(password)
    record = hash_test_password(password)
    assert verify_password(password, record)
    assert not verify_password(SecretStr("wrong!"), record)
    ordinary_record = SecretStr(record.get_secret_value().replace("scrypt-test$", "scrypt$", 1))
    assert not verify_password(password, ordinary_record)
    with pytest.raises(AuthenticationInputInvalid):
        hash_test_password(SecretStr("small"))


def test_normal_password_record_policy_is_unchanged() -> None:
    password = SecretStr(secrets.token_urlsafe(24))
    record = hash_password(password)
    assert record.get_secret_value().startswith("scrypt$")
    assert verify_password(password, record)


def test_policy_length_rejection_still_performs_bounded_password_work(monkeypatch) -> None:
    from infra.authentication import passwords

    calls: list[bytes] = []

    def derive(data: bytes, salt: bytes) -> bytes:
        calls.append(data)
        return bytes(32)

    monkeypatch.setattr(passwords, "_derive", derive)
    suffix = "131072$8$1$" + "00" * 16 + "$" + "00" * 32
    assert not verify_password(SecretStr("sample"), SecretStr("scrypt$" + suffix))
    assert verify_password(SecretStr("sample"), SecretStr("scrypt-test$" + suffix))
    assert not verify_password(SecretStr("tiny"), SecretStr("scrypt-test$" + suffix))
    assert calls == [b"sample", b"sample", b"tiny"]
