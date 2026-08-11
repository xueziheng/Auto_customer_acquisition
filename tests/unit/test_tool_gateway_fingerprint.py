"""Tool call HMAC 指纹的无歧义与密钥安全契约。"""

from __future__ import annotations

import re

import pytest

from shared.errors import ValidationError
from tool_gateway.fingerprint import HmacFingerprintProvider


def test_fingerprint_is_deterministic_lower_hex_and_returns_only_version() -> None:
    """随机盐或非 canonical 编码会让历史幂等记录无法比较。"""
    provider = HmacFingerprintProvider("fp-v1", b"k" * 32)
    first = provider.fingerprint((b"buyer@example.com", b"body"))
    second = provider.fingerprint((b"buyer@example.com", b"body"))
    assert first == second
    digest, version = first
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert version == "fp-v1"
    assert "buyer" not in digest


def test_length_prefix_prevents_concatenation_collision() -> None:
    """简单 concat 会让 (ab,c) 与 (a,bc) 得到同一外发指纹。"""
    provider = HmacFingerprintProvider("fp-v1", b"x" * 32)
    assert provider.fingerprint((b"ab", b"c")) != provider.fingerprint((b"a", b"bc"))


def test_domain_separator_and_key_version_change_the_digest() -> None:
    """换 key/version 后仍返回旧 digest 会破坏轮换审计与用途隔离。"""
    parts = (b"attempt", b"subject", b"body")
    first = HmacFingerprintProvider("fp-v1", b"a" * 32).fingerprint(parts)
    new_key = HmacFingerprintProvider("fp-v1", b"b" * 32).fingerprint(parts)
    new_version = HmacFingerprintProvider("fp-v2", b"a" * 32).fingerprint(parts)
    assert first[0] != new_key[0]
    assert first[0] != new_version[0]


def test_one_byte_change_changes_fingerprint() -> None:
    """漏掉正文尾部或规范化差异会把不同邮件错误视为同一动作。"""
    provider = HmacFingerprintProvider("fp-v1", b"k" * 32)
    original = provider.fingerprint((b"subject", b"body"))
    changed = provider.fingerprint((b"subject", b"body "))
    assert original != changed


@pytest.mark.parametrize(
    ("version", "key"),
    [
        ("", b"k" * 32),
        (" fp-v1", b"k" * 32),
        ("fp-v1 ", b"k" * 32),
        ("fp\nv1", b"k" * 32),
        ("secret-v1", b"k" * 32),
        ("token-v1", b"k" * 32),
        ("fp-v1", b"short"),
        ("fp-v1", b"k" * 65),
        ("fp-v1", bytearray(b"k" * 32)),
    ],
)
def test_provider_rejects_weak_or_secret_like_configuration(
    version: str, key: object
) -> None:
    """弱 key 或 secret-like version 会泄露密钥语义或降低枚举成本。"""
    with pytest.raises(ValidationError) as caught:
        HmacFingerprintProvider(version, key)  # type: ignore[arg-type]
    assert "short" not in str(caught.value)
    assert "secret" not in str(caught.value).lower()


@pytest.mark.parametrize(
    "parts",
    [
        (),
        [b"not-a-tuple"],
        ("text",),
        (bytearray(b"mutable"),),
        tuple(b"x" for _ in range(33)),
        (b"x" * (1_048_576 + 1),),
    ],
)
def test_fingerprint_rejects_malformed_or_unbounded_parts(parts: object) -> None:
    """错类型/无界输入会造成歧义、内存放大或调用方静默改写。"""
    provider = HmacFingerprintProvider("fp-v1", b"k" * 32)
    with pytest.raises(ValidationError):
        provider.fingerprint(parts)  # type: ignore[arg-type]


def test_key_never_appears_in_repr_or_validation_error() -> None:
    """repr 与异常进入日志，不能包含 HMAC key bytes。"""
    raw = b"z" * 32
    provider = HmacFingerprintProvider("fp-v1", raw)
    assert raw.decode() not in repr(provider)
    with pytest.raises(ValidationError) as caught:
        provider.fingerprint((b"x" * (1_048_576 + 1),))
    assert raw.decode() not in str(caught.value)
