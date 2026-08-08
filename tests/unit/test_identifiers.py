"""shared.schemas.identifiers 的 new_id / _encode_ulid 契约单测（行为断言）。

覆盖（拦截的变异）：
1. ULID 已知向量：ts=0/random 全 0 → 26 个 0；ts=2**48-1/random 全 FF → 7+25 个 Z
2. 输出 26 字符且只用 Crockford 字符集（字符集/长度错）
3. 同 random 下 t1<t2 字符串有序（时间戳不占高位）
4. 同毫秒不同 random 输出不同，且随机 80 位未丢失（解码后 16 字符还原）
5. 解码前 10 字符准确还原多个时间戳（0/1/常用值/max，编码错位）
6. _encode_ulid 边界：ts<0、ts>=2**48、rand 长 9/11 抛内置 ValueError（边界校验缺失）
7. new_id：opp_ + 26 合法字符、两次不同；空白 prefix 抛 shared ValidationError；
   两端空格 strip 后输出 opp_ 且不留空格
"""
from __future__ import annotations

from collections.abc import Callable

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import new_id

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_TS = 2**48 - 1


def _load_encode_ulid() -> Callable[[int, bytes], str]:
    # RED 前置：_encode_ulid 尚未定义时，把预期的 ImportError 转成行为失败
    # （pytest.fail）而非收集错误。
    try:
        from shared.schemas.identifiers import _encode_ulid
    except ImportError as exc:
        pytest.fail(f"RED：_encode_ulid 尚未定义（{exc}）")
    return _encode_ulid


def _decode_base32(s: str) -> int:
    val = 0
    for ch in s:
        val = (val << 5) | _CROCKFORD.index(ch)
    return val


# --- ULID 已知向量与格式 -------------------------------------------------


def test_ulid_zero_vector() -> None:
    assert _load_encode_ulid()(0, b"\x00" * 10) == "0" * 26


def test_ulid_max_vector() -> None:
    assert _load_encode_ulid()(_MAX_TS, b"\xff" * 10) == "7" + "Z" * 25


def test_ulid_length_and_crockford_charset() -> None:
    ulid = _load_encode_ulid()(123456789, bytes(range(10)))
    assert len(ulid) == 26
    assert all(ch in _CROCKFORD for ch in ulid)


# --- 时间有序与随机保留 ---------------------------------------------------


def test_ulid_ordered_by_timestamp_with_same_random() -> None:
    enc = _load_encode_ulid()
    rand = b"\x01" * 10
    assert enc(1000, rand) < enc(2000, rand)


def test_ulid_same_ms_different_random_differs_and_keeps_random() -> None:
    enc = _load_encode_ulid()
    r1 = bytes(range(10))
    r2 = bytes(reversed(range(10)))
    ulid1 = enc(123456789, r1)
    ulid2 = enc(123456789, r2)
    assert ulid1 != ulid2
    # 随机 80 位未丢失：解码后 16 字符还原原随机值
    assert _decode_base32(ulid1[-16:]) == int.from_bytes(r1, "big")
    assert _decode_base32(ulid2[-16:]) == int.from_bytes(r2, "big")


@pytest.mark.parametrize("ts", [0, 1, 1234567890123, _MAX_TS])
def test_ulid_first_ten_chars_restore_timestamp(ts: int) -> None:
    ulid = _load_encode_ulid()(ts, b"\x00" * 10)
    assert _decode_base32(ulid[:10]) == ts


# --- _encode_ulid 边界：内置 ValueError（私有编程错误） --------------------


@pytest.mark.parametrize("bad_ts", [-1, 2**48])
def test_ulid_rejects_bad_timestamp(bad_ts: int) -> None:
    with pytest.raises(ValueError):
        _load_encode_ulid()(bad_ts, b"\x00" * 10)


@pytest.mark.parametrize("bad_len", [9, 11])
def test_ulid_rejects_wrong_random_length(bad_len: int) -> None:
    with pytest.raises(ValueError):
        _load_encode_ulid()(1000, b"\x00" * bad_len)


# --- new_id ----------------------------------------------------------------


def test_new_id_prefix_and_format() -> None:
    result = new_id("opp")
    assert result.startswith("opp_")
    assert len(result) == 4 + 26
    assert all(ch in _CROCKFORD for ch in result[4:])


def test_new_id_calls_differ() -> None:
    assert new_id("opp") != new_id("opp")


@pytest.mark.parametrize("blank", ["", "   "])
def test_new_id_blank_prefix_raises(blank: str) -> None:
    with pytest.raises(ValidationError):
        new_id(blank)


def test_new_id_strips_prefix_whitespace() -> None:
    result = new_id("  opp  ")
    assert result.startswith("opp_")
    assert " " not in result
