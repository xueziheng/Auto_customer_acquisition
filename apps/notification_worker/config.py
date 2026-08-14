"""notification worker 的显式、严格和脱敏配置。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

from pydantic import SecretStr

from infra.secrets import (
    EnvironmentSecretResolver,
    validate_environment_secret_reference,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId

from .recipients import ConfiguredNotificationRecipientDirectory

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_ID = re.compile(rf"tn_{_ULID}\Z")
_IDENTITY_ID = re.compile(rf"sid_{_ULID}\Z")
_LEASE_OWNER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")
_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password", "authorization")
_GMAIL_BASE_URL = "https://gmail.googleapis.com"


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


def _identity(value: str) -> SendingIdentityId:
    value = _text(value)
    if _IDENTITY_ID.fullmatch(value) is None:
        raise ValueError("invalid identity")
    return SendingIdentityId(value)


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("invalid bool")


def _version(value: str) -> str:
    value = _text(value)
    if _VERSION.fullmatch(value) is None or any(
        marker in value.casefold() for marker in _CREDENTIAL_MARKERS
    ):
        raise ValueError("invalid version")
    return value


def _secret_reference(value: str) -> str:
    return validate_environment_secret_reference(_text(value))


def _gmail_base_url(value: str, *, dev_mode: bool) -> str:
    value = _text(value)
    if value == _GMAIL_BASE_URL:
        return value
    parsed = urlsplit(value)
    try:
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("invalid Gmail base URL") from None
    canonical_host = f"[{hostname}]" if hostname == "::1" else hostname
    canonical = (
        f"http://{canonical_host}:{port}"
        if canonical_host is not None and port is not None
        else None
    )
    if (
        not dev_mode
        or parsed.scheme != "http"
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or not 1024 <= port <= 65535
        or value != canonical
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Gmail base URL")
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
    email: NotificationEmailSettings | None = field(
        default=None, repr=False, compare=False
    )

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
        if self.email is not None and not isinstance(
            self.email, NotificationEmailSettings
        ):
            raise ValidationError("通知 worker 邮件配置无效")
        object.__setattr__(self, "tenant_id", tenant)
        object.__setattr__(self, "lease_owner", owner)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        tenant_id = _read(environ, "TRADEOS_TENANT_ID", _tenant)
        email = _email_settings(environ, tenant_id)
        return cls(
            _read(environ, "DATABASE_URL", _database_url),
            tenant_id,
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
            email,
        )


@dataclass(frozen=True, repr=False)
class NotificationEmailSettings:
    gmail_base_url: str
    sending_identity_id: SendingIdentityId
    recipients: ConfiguredNotificationRecipientDirectory = field(repr=False)
    gmail_oauth_token_ref: str = field(repr=False)
    fingerprint_key_ref: str = field(repr=False)
    fingerprint_key_version: str
    tool_lease_seconds: int
    secrets: EnvironmentSecretResolver = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.gmail_base_url, str)
            or not _valid_identity(self.sending_identity_id)
            or not isinstance(
                self.recipients, ConfiguredNotificationRecipientDirectory
            )
            or not isinstance(self.gmail_oauth_token_ref, str)
            or not isinstance(self.fingerprint_key_ref, str)
            or not isinstance(self.fingerprint_key_version, str)
            or type(self.tool_lease_seconds) is not int
            or not 1 <= self.tool_lease_seconds <= 86_400
            or not isinstance(self.secrets, EnvironmentSecretResolver)
        ):
            raise ValidationError("通知 worker 邮件配置无效")


def _email_settings(
    environ: Mapping[str, str], tenant_id: TenantId
) -> NotificationEmailSettings:
    dev_mode = _read(environ, "TRADEOS_DEV_MODE", _boolean)
    raw_recipients = _read(
        environ,
        "TRADEOS_NOTIFICATION_RECIPIENTS_JSON",
        _recipient_value,
    )
    if any(
        not isinstance(item, dict) or item.get("tenant_id") != tenant_id
        for item in raw_recipients
    ):
        raise NotificationWorkerConfigurationError(
            "TRADEOS_NOTIFICATION_RECIPIENTS_JSON"
        )
    try:
        recipients = ConfiguredNotificationRecipientDirectory.from_value(
            raw_recipients
        )
    except ValidationError:
        raise NotificationWorkerConfigurationError(
            "TRADEOS_NOTIFICATION_RECIPIENTS_JSON"
        ) from None
    return NotificationEmailSettings(
        _read(
            environ,
            "TRADEOS_NOTIFICATION_GMAIL_BASE_URL",
            lambda value: _gmail_base_url(value, dev_mode=dev_mode),
        ),
        _read(
            environ,
            "TRADEOS_NOTIFICATION_SENDING_IDENTITY_ID",
            _identity,
        ),
        recipients,
        _read(environ, "GMAIL_OAUTH_TOKEN_REF", _secret_reference),
        _read(
            environ,
            "TOOL_CALL_FINGERPRINT_KEY_REF",
            _secret_reference,
        ),
        _read(
            environ,
            "TOOL_CALL_FINGERPRINT_KEY_VERSION",
            _version,
        ),
        _read(
            environ,
            "TRADEOS_TOOL_LEASE_SECONDS",
            lambda value: _integer(value, minimum=1, maximum=86_400),
        ),
        EnvironmentSecretResolver(dict(environ)),
    )


def _recipient_value(value: str) -> list[dict[str, object]]:
    value = _text(value)
    if len(value.encode("utf-8")) > 131_072:
        raise ValueError("recipient config too large")
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise ValueError("invalid recipient config") from None
    if not isinstance(decoded, list):
        raise TypeError("invalid recipient config")
    return decoded


def _valid_identity(value: object) -> bool:
    return isinstance(value, str) and _IDENTITY_ID.fullmatch(value) is not None
