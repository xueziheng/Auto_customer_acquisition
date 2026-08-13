"""邮件反馈 worker 的严格、显式、脱敏配置。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import SecretStr

from infra.secrets import validate_environment_secret_reference
from shared.errors import ValidationError
from shared.schemas.identifiers import SendingIdentityId, TenantId

BOOTSTRAP_DAYS = 30
GMAIL_API_BASE_URL = "https://gmail.googleapis.com"
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_RE = re.compile(rf"tn_{_ULID}")
_IDENTITY_RE = re.compile(rf"sid_{_ULID}")
_ALIAS_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_PUBLIC_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password", "authorization")


class WorkerConfigurationError(RuntimeError):
    """固定、脱敏的单字段配置错误。"""

    def __init__(self, field_name: str) -> None:
        super().__init__("邮件反馈 worker 配置无效")
        self.field_name = field_name


def _safe_text(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError("invalid text")
    return value


def _database_url(value: str) -> SecretStr:
    return SecretStr(_safe_text(value))


def _tenant(value: str) -> TenantId:
    if _TENANT_RE.fullmatch(_safe_text(value)) is None:
        raise ValueError("invalid tenant")
    return TenantId(value)


def _identity(value: str) -> SendingIdentityId:
    if _IDENTITY_RE.fullmatch(_safe_text(value)) is None:
        raise ValueError("invalid identity")
    return SendingIdentityId(value)


def _public(value: str, pattern: re.Pattern[str]) -> str:
    value = _safe_text(value)
    if pattern.fullmatch(value) is None or any(
        marker in value.casefold() for marker in _CREDENTIAL_MARKERS
    ):
        raise ValueError("invalid public value")
    return value


def _secret_reference(value: str) -> str:
    return validate_environment_secret_reference(_safe_text(value))


def _boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("invalid bool")


def _gmail_base_url(value: str, *, dev_mode: bool) -> str:
    value = _safe_text(value)
    if value == GMAIL_API_BASE_URL:
        return value
    parsed = urlsplit(value)
    try:
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise ValueError("invalid Gmail base URL") from None
    canonical_host = f"[{hostname}]" if hostname == "::1" else hostname
    canonical_value = (
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
        or value != canonical_value
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Gmail base URL")
    return value


def _integer(value: str, *, minimum: int, maximum: int) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("invalid integer")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("integer out of range")
    return parsed


def _read[T](
    environ: Mapping[str, str], name: str, parser: Callable[[str], T]
) -> T:
    try:
        return parser(environ[name])
    except (KeyError, TypeError, ValueError, ValidationError):
        raise WorkerConfigurationError(name) from None


@dataclass(frozen=True)
class EmailFeedbackWorkerConfig:
    tenant_id: TenantId
    mailbox_alias: str
    sending_identity_id: SendingIdentityId
    feedback_route_id: str
    poll_interval_seconds: int = 30
    page_limit: int = 100
    enabled: bool = False
    health_port: int = 8092

    def __post_init__(self) -> None:
        try:
            tenant = _tenant(self.tenant_id)
            alias = _public(self.mailbox_alias, _ALIAS_RE)
            identity = _identity(self.sending_identity_id)
            route = _public(self.feedback_route_id, _PUBLIC_ID_RE)
        except (TypeError, ValueError):
            raise ValidationError("邮件反馈 worker 配置无效") from None
        for value, minimum, maximum in (
            (self.poll_interval_seconds, 5, 3600),
            (self.page_limit, 1, 100),
            (self.health_port, 1, 65535),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not minimum <= value <= maximum
            ):
                raise ValidationError("邮件反馈 worker 数值配置无效")
        if not isinstance(self.enabled, bool):
            raise ValidationError("邮件反馈 worker enabled 无效")
        object.__setattr__(self, "tenant_id", tenant)
        object.__setattr__(self, "mailbox_alias", alias)
        object.__setattr__(self, "sending_identity_id", identity)
        object.__setattr__(self, "feedback_route_id", route)


@dataclass(frozen=True, repr=False)
class EmailFeedbackWorkerSettings:
    database_url: SecretStr
    gmail_oauth_token_ref: str
    tool_call_fingerprint_key_ref: str
    tool_call_fingerprint_key_version: str
    dev_mode: bool
    gmail_base_url: str
    config: EmailFeedbackWorkerConfig

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str]
    ) -> EmailFeedbackWorkerSettings:
        database_url = _read(environ, "DATABASE_URL", _database_url)
        oauth_ref = _read(environ, "GMAIL_OAUTH_TOKEN_REF", _secret_reference)
        fingerprint_ref = _read(
            environ, "TOOL_CALL_FINGERPRINT_KEY_REF", _secret_reference
        )
        fingerprint_version = _read(
            environ,
            "TOOL_CALL_FINGERPRINT_KEY_VERSION",
            lambda value: _public(value, _VERSION_RE),
        )
        dev_mode = _read(environ, "TRADEOS_DEV_MODE", _boolean)
        gmail_base_url = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL",
            lambda value: _gmail_base_url(value, dev_mode=dev_mode),
        )
        tenant_id = _read(environ, "TRADEOS_TENANT_ID", _tenant)
        mailbox_alias = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS",
            lambda value: _public(value, _ALIAS_RE),
        )
        identity_id = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_SENDING_IDENTITY_ID",
            _identity,
        )
        route_id = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID",
            lambda value: _public(value, _PUBLIC_ID_RE),
        )
        enabled = _read(environ, "TRADEOS_EMAIL_FEEDBACK_ENABLED", _boolean)
        health_port = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT",
            lambda value: _integer(value, minimum=1, maximum=65535),
        )
        interval = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS",
            lambda value: _integer(value, minimum=5, maximum=3600),
        )
        page_limit = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT",
            lambda value: _integer(value, minimum=1, maximum=100),
        )
        return cls(
            database_url,
            oauth_ref,
            fingerprint_ref,
            fingerprint_version,
            dev_mode,
            gmail_base_url,
            EmailFeedbackWorkerConfig(
                tenant_id,
                mailbox_alias,
                identity_id,
                route_id,
                interval,
                page_limit,
                enabled,
                health_port,
            ),
        )
