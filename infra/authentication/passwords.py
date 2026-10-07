"""固定参数 scrypt 与进程级受限工作线程；所有材料保持 SecretStr。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from pydantic import SecretStr

from shared.authentication import AuthenticationInputInvalid

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="authentication")
_SLOTS = threading.BoundedSemaphore(2)
_RECORD = re.compile(r"scrypt\$131072\$8\$1\$([0-9a-f]{32})\$([0-9a-f]{64})", re.ASCII)


def _password_bytes(password: SecretStr) -> bytes:
    value = password.get_secret_value()
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise AuthenticationInputInvalid() from None
    if not 10 <= len(value) <= 128 or len(encoded) > 512:
        raise AuthenticationInputInvalid()
    return encoded


def _derive(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password, salt=salt, n=131072, r=8, p=1, dklen=32, maxmem=268435456
    )


def hash_password(password: SecretStr) -> SecretStr:
    """严格验证密码并以随机 16 字节 salt 编码；调用方必须使用受限线程。"""
    data = _password_bytes(password)
    salt = secrets.token_bytes(16)
    digest = _derive(data, salt)
    return SecretStr(f"scrypt$131072$8$1${salt.hex()}${digest.hex()}")


def verify_password(password: SecretStr, record: SecretStr) -> bool:
    """只接受唯一固定格式；非法记录在分配 scrypt 内存前拒绝。"""
    match = _RECORD.fullmatch(record.get_secret_value())
    if match is None:
        return False
    try:
        data = _password_bytes(password)
    except AuthenticationInputInvalid:
        return False
    actual = _derive(data, bytes.fromhex(match[1]))
    return hmac.compare_digest(actual, bytes.fromhex(match[2]))


async def password_work[T](operation: Callable[..., T], *args: object) -> T:
    """进程最多两个哈希工作；取消协程也不会提前释放仍在运行的工作槽。"""
    while not _SLOTS.acquire(blocking=False):
        await asyncio.sleep(0.01)

    def run() -> T:
        try:
            return operation(*args)
        finally:
            _SLOTS.release()

    try:
        future = asyncio.get_running_loop().run_in_executor(_EXECUTOR, run)
    except BaseException:
        _SLOTS.release()
        raise
    return await asyncio.shield(future)


def token_digest(token: SecretStr) -> str:
    """仅固定格式 32 字节 URL-safe token 能进入认证查询。"""
    raw = token.get_secret_value()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw, re.ASCII):
        raise AuthenticationInputInvalid()
    decoded = base64.urlsafe_b64decode(raw + "=")
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != raw:
        raise AuthenticationInputInvalid()
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def csrf_for(token: SecretStr) -> SecretStr:
    """按 ADR0067 从会话随机材料派生独立用途 CSRF，支持只读刷新恢复。"""
    token_digest(token)
    key = base64.urlsafe_b64decode(token.get_secret_value() + "=")
    digest = hmac.new(key, b"tradeos:csrf:v1", hashlib.sha256).digest()
    return SecretStr(base64.urlsafe_b64encode(digest).decode().rstrip("="))
