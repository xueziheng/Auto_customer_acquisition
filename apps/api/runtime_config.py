"""Phase 1 API runtime 的显式、严格且脱敏配置解析。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictInt,
    TypeAdapter,
)
from pydantic import ValidationError as PydanticValidationError

from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy
from shared.errors import ValidationError
from shared.schemas.money import CurrencyCode, Money, WireDecimal


def _nonblank_exact(value: str) -> str:
    if not value or not value.strip() or value != value.strip():
        raise ValueError("blank configuration")
    return value


def _parse_database_url(value: str) -> SecretStr:
    return SecretStr(_nonblank_exact(value))


def _parse_tenant_id(value: str) -> str:
    return _nonblank_exact(value)


def _parse_dev_mode(value: str) -> bool:
    if value != "true":
        raise ValueError("dev mode must be explicit")
    return True


def _parse_positive_integer(value: str) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise ValueError("positive integer required")
    return int(value)


def _parse_safe_reference(value: str) -> str:
    value = _nonblank_exact(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("control character forbidden")
    return value


def _parse_https_origin(value: str) -> str:
    value = _parse_safe_reference(value)
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or not parsed.hostname.isascii()
        or parsed.hostname != parsed.hostname.lower()
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HTTPS origin invalid")
    port = parsed.port
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    canonical = f"https://{host}"
    if port is not None and port != 443:
        canonical = f"{canonical}:{port}"
    if value != canonical:
        raise ValueError("HTTPS origin not canonical")
    return value


class RuntimeConfigurationError(RuntimeError):
    """固定、脱敏的单字段配置错误。"""

    def __init__(self, field_name: str) -> None:
        super().__init__("API runtime 配置无效")
        self.field_name = field_name


class _HandoffPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    sla_seconds: StrictInt = Field(gt=0)
    backlog_threshold: StrictInt = Field(gt=0)
    t1_seconds: StrictInt = Field(gt=0)
    t2_seconds: StrictInt = Field(gt=0)


class _ScoringPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    version: str
    currency: str
    value_band_boundaries: tuple[WireDecimal, ...]
    bucket_map: dict[
        Literal["1", "2", "3", "4", "5", "6", "7"],
        Literal["high", "mid", "low"],
    ]


_HANDOFF_ADAPTER = TypeAdapter(_HandoffPayload)
_SCORING_ADAPTER = TypeAdapter(_ScoringPayload)
_ORIGIN_ADAPTER = TypeAdapter(tuple[str, ...])


def _parse_origins(value: str) -> tuple[str, ...]:
    origins = _ORIGIN_ADAPTER.validate_json(value)
    if not origins or len(origins) != len(set(origins)):
        raise ValueError("origin list invalid")
    for origin in origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.hostname is None
            or not parsed.hostname.isascii()
            or parsed.hostname != parsed.hostname.lower()
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin invalid")
        port = parsed.port
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        default_port = (parsed.scheme == "http" and port == 80) or (
            parsed.scheme == "https" and port == 443
        )
        canonical = f"{parsed.scheme}://{host}"
        if port is not None and not default_port:
            canonical = f"{canonical}:{port}"
        if origin != canonical:
            raise ValueError("origin not canonical")
    return origins


def _parse_scoring_policy(value: str) -> ScoringPolicy:
    payload = _SCORING_ADAPTER.validate_json(value)
    version = _nonblank_exact(payload.version)
    currency = payload.currency
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise ValueError("currency invalid")
    return ScoringPolicy(
        version=version,
        value_band_boundaries=tuple(
            Money(amount, CurrencyCode(currency))
            for amount in payload.value_band_boundaries
        ),
        bucket_map={int(rank): bucket for rank, bucket in payload.bucket_map.items()},
    )


def _read[T](
    environ: Mapping[str, str],
    name: str,
    parser: Callable[[str], T],
) -> T:
    try:
        return parser(environ[name])
    except (KeyError, ValueError, PydanticValidationError, ValidationError):
        raise RuntimeConfigurationError(name) from None


@dataclass(frozen=True)
class Phase1RuntimeSettings:
    """生产 runtime 的完整不可变配置；数据库 URL 以 SecretStr 持有。"""

    database_url: SecretStr
    tenant_id: str
    dev_mode: bool
    cors_allowed_origins: tuple[str, ...]
    retry_after_seconds: int
    handoff_policy: HandoffPolicy
    t1: timedelta
    t2: timedelta
    scoring_policy: ScoringPolicy
    outbox_max_attempts: int
    gmail_oauth_token_ref: str
    tool_call_fingerprint_key_ref: str
    tool_call_fingerprint_key_version: str
    unsubscribe_base_url: str
    tool_lease: timedelta

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Phase1RuntimeSettings:
        """从显式 mapping 读取全部必填值；任何失败只暴露变量名。"""
        database_url = _read(environ, "DATABASE_URL", _parse_database_url)
        tenant_id = _read(environ, "TRADEOS_TENANT_ID", _parse_tenant_id)
        dev_mode = _read(environ, "TRADEOS_DEV_MODE", _parse_dev_mode)
        origins = _read(
            environ,
            "TRADEOS_CORS_ALLOWED_ORIGINS",
            _parse_origins,
        )
        retry_after = _read(
            environ,
            "TRADEOS_API_RETRY_AFTER_SECONDS",
            _parse_positive_integer,
        )
        handoff = _read(
            environ,
            "TRADEOS_HANDOFF_POLICY",
            _HANDOFF_ADAPTER.validate_json,
        )
        scoring_policy = _read(
            environ,
            "TRADEOS_SCORING_POLICY",
            _parse_scoring_policy,
        )
        max_attempts = _read(
            environ,
            "TRADEOS_OUTBOX_MAX_ATTEMPTS",
            _parse_positive_integer,
        )
        gmail_oauth_token_ref = _read(
            environ,
            "GMAIL_OAUTH_TOKEN_REF",
            _parse_safe_reference,
        )
        fingerprint_key_ref = _read(
            environ,
            "TOOL_CALL_FINGERPRINT_KEY_REF",
            _parse_safe_reference,
        )
        fingerprint_key_version = _read(
            environ,
            "TOOL_CALL_FINGERPRINT_KEY_VERSION",
            _parse_safe_reference,
        )
        unsubscribe_base_url = _read(
            environ,
            "TRADEOS_UNSUBSCRIBE_BASE_URL",
            _parse_https_origin,
        )
        tool_lease_seconds = _read(
            environ,
            "TRADEOS_TOOL_LEASE_SECONDS",
            _parse_positive_integer,
        )
        return cls(
            database_url=database_url,
            tenant_id=tenant_id,
            dev_mode=dev_mode,
            cors_allowed_origins=origins,
            retry_after_seconds=retry_after,
            handoff_policy=HandoffPolicy(
                sla_seconds=handoff.sla_seconds,
                backlog_threshold=handoff.backlog_threshold,
            ),
            t1=timedelta(seconds=handoff.t1_seconds),
            t2=timedelta(seconds=handoff.t2_seconds),
            scoring_policy=scoring_policy,
            outbox_max_attempts=max_attempts,
            gmail_oauth_token_ref=gmail_oauth_token_ref,
            tool_call_fingerprint_key_ref=fingerprint_key_ref,
            tool_call_fingerprint_key_version=fingerprint_key_version,
            unsubscribe_base_url=unsubscribe_base_url,
            tool_lease=timedelta(seconds=tool_lease_seconds),
        )
