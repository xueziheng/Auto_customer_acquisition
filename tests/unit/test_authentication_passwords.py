"""密码材料只在测试进程中生成；失败输出只含安全错误。"""

import secrets

import pytest
from pydantic import SecretStr


def test_password_roundtrip_and_strict_records():
    from infra.authentication.passwords import hash_password, verify_password

    password = SecretStr(secrets.token_urlsafe(24))
    record = hash_password(password)
    verified = verify_password(password, record)
    assert verified, "AUTH_PASSWORD_VERIFY_FAILED"
    wrong_rejected = not verify_password(SecretStr(secrets.token_urlsafe(24)), record)
    assert wrong_rejected, "AUTH_WRONG_PASSWORD_ACCEPTED"
    for malformed in (
        "",
        "scrypt$999999999$8$1$a$b",
        record.get_secret_value().replace("131072", "2"),
        record.get_secret_value() + "x",
    ):
        malformed_rejected = not verify_password(password, SecretStr(malformed))
        assert malformed_rejected, "AUTH_MALFORMED_RECORD_ACCEPTED"
    password_repr_safe = password.get_secret_value() not in repr(record)
    assert password_repr_safe, "AUTH_PASSWORD_REPR_EXPOSED"


@pytest.mark.parametrize("length", [0, 9, 129])
def test_password_length_rejected(length):
    from infra.authentication.passwords import hash_password
    from shared.authentication import AuthenticationInputInvalid

    with pytest.raises(AuthenticationInputInvalid):
        hash_password(SecretStr(secrets.token_hex(70)[:length]))


def test_unicode_and_spaces_are_not_trimmed():
    from infra.authentication.passwords import hash_password, verify_password

    password = SecretStr(" " + secrets.token_urlsafe(16) + " ")
    record = hash_password(password)
    verified = verify_password(password, record)
    assert verified, "AUTH_PASSWORD_VERIFY_FAILED"
    trimmed_rejected = not verify_password(
        SecretStr(password.get_secret_value().strip()), record
    )
    assert trimmed_rejected, "AUTH_PASSWORD_WAS_TRIMMED"
    unicode_password = SecretStr(chr(0x1F600) * 128)
    unicode_verified = verify_password(
        unicode_password, hash_password(unicode_password)
    )
    assert unicode_verified, "AUTH_UNICODE_PASSWORD_REJECTED"


def test_minimum_length_and_distinct_salts():
    from infra.authentication.passwords import hash_password, verify_password

    password = SecretStr(secrets.token_urlsafe(16)[:10])
    first, second = hash_password(password), hash_password(password)
    salts_differ = first != second
    first_verified = verify_password(password, first)
    second_verified = verify_password(password, second)
    assert salts_differ, "AUTH_PASSWORD_SALT_REUSED"
    assert first_verified, "AUTH_FIRST_RECORD_INVALID"
    assert second_verified, "AUTH_SECOND_RECORD_INVALID"


async def test_worker_slots_remain_bounded_after_cancellation():
    import asyncio
    import threading

    from infra.authentication.passwords import password_work

    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    active = peak = entered = 0

    def work():
        nonlocal active, peak, entered
        with lock:
            active += 1
            entered += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        release.wait(timeout=5)
        with lock:
            active -= 1

    tasks = [asyncio.create_task(password_work(work)) for _ in range(4)]
    try:
        for _ in range(100):
            if two_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert two_started.is_set()
        tasks[0].cancel()
        await asyncio.sleep(0.04)
        assert entered == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert peak == 2


def test_csrf_derivation_and_token_encoding_are_canonical():
    import base64
    import hashlib
    import hmac

    from infra.authentication.passwords import csrf_for, token_digest
    from shared.authentication import AuthenticationInputInvalid

    raw = secrets.token_bytes(32)
    token = SecretStr(base64.urlsafe_b64encode(raw).decode().rstrip("="))
    expected = (
        base64.urlsafe_b64encode(hmac.digest(raw, b"tradeos:csrf:v1", "sha256"))
        .decode()
        .rstrip("=")
    )
    csrf_matches = csrf_for(token).get_secret_value() == expected
    assert csrf_matches, "AUTH_CSRF_DERIVATION_MISMATCH"
    digest_matches = (
        token_digest(token)
        == hashlib.sha256(token.get_secret_value().encode()).hexdigest()
    )
    assert digest_matches, "AUTH_TOKEN_DIGEST_MISMATCH"
    for malformed in ("", "a" * 42, "!" * 43, "a" * 44, "a" * 42 + "B"):
        with pytest.raises(AuthenticationInputInvalid):
            token_digest(SecretStr(malformed))
