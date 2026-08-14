"""notification worker 的显式、严格和脱敏配置。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Self

from pydantic import SecretStr

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_ID = re.compile(rf"tn_{_ULID}\Z")
_LEASE_OWNER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password", "authorization")


class NotificationWorkerConfigurationError(RuntimeError):
    """只暴露固定错误与安全字段名。"""

    def __init__(self, field_name: str) -> None:
        super().__init__("通知 worker 配置无效")
        self.field_name = field_name


def _text(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError("invalid text")
    return value


def _database_url(value: str) -> SecretStr:
    return SecretStr(_text(value))


def _tenant(value: str) -> TenantId:
    value = _text(value)
    if _TENANT_ID.fullmatch(value) is None:
        raise ValueError("invalid tenant")
    return TenantId(value)


def _integer(value: str, *, minimum: int, maximum: int) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("invalid integer")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("integer out of range")
    return parsed


def _lease_owner(value: str) -> str:
    value = _text(value)
    if _LEASE_OWNER.fullmatch(value) is None or any(
        marker in value.casefold() for marker in _CREDENTIAL_MARKERS
    ):
        raise ValueError("invalid lease owner")
    return value


def _read[T](
    environ: Mapping[str, str], name: str, parser: Callable[[str], T]
) -> T:
    try:
        return parser(environ[name])
    except (KeyError, TypeError, ValueError, ValidationError):
        raise NotificationWorkerConfigurationError(name) from None


@dataclass(frozen=True)
class NotificationWorkerConfig:
    database_url: SecretStr = field(repr=False)
    tenant_id: TenantId
    poll_interval_seconds: int
    batch_limit: int
    health_port: int
    lease_owner: str

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.database_url, SecretStr):
                raise TypeError("invalid database url")
            if not self.database_url.get_secret_value():
                raise ValueError("invalid database url")
            tenant = _tenant(self.tenant_id)
            owner = _lease_owner(self.lease_owner)
        except (TypeError, ValueError):
            raise ValidationError("通知 worker 配置无效") from None
        for value, minimum, maximum in (
            (self.poll_interval_seconds, 1, 3600),
            (self.batch_limit, 1, 100),
            (self.health_port, 1, 65535),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValidationError("通知 worker 数值配置无效")
        object.__setattr__(self, "tenant_id", tenant)
        object.__setattr__(self, "lease_owner", owner)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        return cls(
            _read(environ, "DATABASE_URL", _database_url),
            _read(environ, "TRADEOS_TENANT_ID", _tenant),
            _read(
                environ,
                "TRADEOS_NOTIFICATION_POLL_INTERVAL_SECONDS",
                lambda value: _integer(value, minimum=1, maximum=3600),
            ),
            _read(
                environ,
                "TRADEOS_NOTIFICATION_BATCH_LIMIT",
                lambda value: _integer(value, minimum=1, maximum=100),
            ),
            _read(
                environ,
                "TRADEOS_NOTIFICATION_HEALTH_PORT",
                lambda value: _integer(value, minimum=1, maximum=65535),
            ),
            _read(
                environ,
                "TRADEOS_NOTIFICATION_LEASE_OWNER",
                _lease_owner,
            ),
        )
