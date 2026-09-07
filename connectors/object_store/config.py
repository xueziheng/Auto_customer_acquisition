"""对象存储 connector 的严格、显式、脱敏配置。"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from infra.secrets import validate_environment_secret_reference
from shared.errors import PolicyViolation, ValidationError

_POSITIVE_INT = re.compile(r"[1-9][0-9]*")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_REGION = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[1-9][0-9]*")
_MAX_SIGNED_INT64 = 2**63 - 1


def _invalid() -> PolicyViolation:
    return PolicyViolation("Artifact Store 配置无效")


def _read[T](environ: Mapping[str, str], name: str, parser: Callable[[str], T]) -> T:
    try:
        value = environ[name]
        if not isinstance(value, str):
            raise TypeError("configuration value is not text")
        return parser(value)
    except (KeyError, TypeError, ValueError, ValidationError):
        raise _invalid() from None


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("invalid boolean")


def _positive_int(value: str) -> int:
    if _POSITIVE_INT.fullmatch(value) is None:
        raise ValueError("invalid integer")
    parsed = int(value)
    if parsed > _MAX_SIGNED_INT64:
        raise ValueError("integer out of range")
    return parsed


def _bucket(value: str) -> str:
    if (
        _BUCKET.fullmatch(value) is None
        or ".." in value
        or ".-" in value
        or "-." in value
    ):
        raise ValueError("invalid bucket")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("bucket must not be an IP address")


def _region(value: str) -> str:
    if _REGION.fullmatch(value) is None:
        raise ValueError("invalid region")
    return value


def _secret_ref(value: str) -> str:
    return validate_environment_secret_reference(value)


def _endpoint(value: str, *, dev_mode: bool) -> str:
    if not value or value != value.strip():
        raise ValueError("invalid endpoint")
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("invalid endpoint") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid endpoint")
    if (
        hostname is None
        or not hostname.isascii()
        or hostname != hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid endpoint")
    host = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.scheme == "https":
        canonical = f"https://{host}"
        if port is not None and port != 443:
            canonical = f"{canonical}:{port}"
    elif (
        dev_mode
        and parsed.scheme == "http"
        and hostname in {"127.0.0.1", "localhost", "::1"}
        and port is not None
    ):
        canonical = f"http://{host}:{port}"
    else:
        raise ValueError("invalid endpoint")
    if value != canonical:
        raise ValueError("noncanonical endpoint")
    return value


@dataclass(frozen=True, repr=False)
class S3ObjectStoreSettings:
    """不含凭证原值的 bucket-bound connector 配置。"""

    dev_mode: bool
    endpoint: str
    bucket: str
    access_key_ref: str
    secret_key_ref: str
    region: str
    raw_max_bytes: int
    generated_max_bytes: int

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> S3ObjectStoreSettings:
        """仅读取八个必填变量；任何错误固定脱敏。"""
        return cls._parse(environ, pilot=False)

    @classmethod
    def from_pilot_environ(cls, environ: Mapping[str, str]) -> S3ObjectStoreSettings:
        """显式真实本机内测：认证非 dev，仅开放精确 IPv4 loopback HTTP。"""
        if environ.get("TRADEOS_DEV_MODE") != "false":
            raise _invalid()
        return cls._parse(environ, pilot=True)

    @classmethod
    def _parse(
        cls, environ: Mapping[str, str], *, pilot: bool
    ) -> S3ObjectStoreSettings:
        dev_mode = _read(environ, "TRADEOS_DEV_MODE", _boolean)
        endpoint = _read(
            environ,
            "S3_ENDPOINT",
            lambda value: _endpoint(value, dev_mode=dev_mode or pilot),
        )
        if pilot and re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]*", endpoint) is None:
            raise _invalid()
        bucket = _read(environ, "S3_BUCKET_ARTIFACTS", _bucket)
        access_ref = _read(environ, "S3_ACCESS_KEY_REF", _secret_ref)
        secret_ref = _read(environ, "S3_SECRET_KEY_REF", _secret_ref)
        region = _read(environ, "S3_REGION", _region)
        raw_max = _read(environ, "RAW_ARTIFACT_MAX_BYTES", _positive_int)
        generated_max = _read(environ, "GENERATED_ARTIFACT_MAX_BYTES", _positive_int)
        return cls(
            dev_mode,
            endpoint,
            bucket,
            access_ref,
            secret_ref,
            region,
            raw_max,
            generated_max,
        )

    def __repr__(self) -> str:
        return f"S3ObjectStoreSettings(dev_mode={self.dev_mode!r})"
