"""Phase 1 API runtime 的显式、严格且脱敏配置解析。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
from infra.secrets import validate_environment_secret_reference
from shared.errors import ValidationError
from shared.schemas.money import CurrencyCode, Money, WireDecimal
from workflows.email_feedback.unsubscribe import UnsubscribeKeyReference

_PUBLIC_ID = re.compile(r"[a-z0-9-]{1,32}")
_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password")


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


def _parse_key_id(value: str) -> str:
    value = _nonblank_exact(value)
    if _PUBLIC_ID.fullmatch(value) is None:
        raise ValueError("key id invalid")
    return value


def _parse_route_id(value: str) -> str:
    value = _parse_key_id(value)
    if any(marker in value for marker in _CREDENTIAL_MARKERS):
        raise ValueError("route id invalid")
    return value


def _parse_key_references(value: str) -> tuple[UnsubscribeKeyReference, ...]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key id")
            result[key] = item
        return result

    try:
        payload = json.loads(value, object_pairs_hook=pairs)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ValueError("key reference mapping invalid") from None
    if not isinstance(payload, dict) or not payload:
        raise ValueError("key reference mapping invalid")
    references: list[UnsubscribeKeyReference] = []
    for key_id, secret_ref in payload.items():
        if not isinstance(secret_ref, str):
            raise TypeError("key reference mapping invalid")
        references.append(
            UnsubscribeKeyReference(
                _parse_key_id(key_id),
                validate_environment_secret_reference(secret_ref),
            )
        )
    return tuple(references)


def _parse_unsubscribe_origin(value: str, *, dev_mode: bool) -> str:
    value = _parse_safe_reference(value)
    parsed = urlsplit(value)
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
        raise ValueError("unsubscribe origin invalid")
    if parsed.scheme == "http" and (
        not dev_mode or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("unsubscribe origin invalid")
    port = parsed.port
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    canonical = f"{parsed.scheme}://{host}"
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    if port is not None and not default_port:
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
    except (KeyError, TypeError, ValueError, PydanticValidationError, ValidationError):
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
    email_feedback_route_id: str
    unsubscribe_active_key_id: str
    unsubscribe_key_refs: tuple[UnsubscribeKeyReference, ...]
    tool_lease: timedelta
    openai_api_key_ref: str = "OPENAI_API_KEY"
    trade_manager_model: str = "gpt-5-mini"
    tavily_api_key_ref: str | None = field(default=None, repr=False)
    tavily_exclusive_account_confirmed: bool = False

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
            lambda value: _parse_unsubscribe_origin(value, dev_mode=dev_mode),
        )
        email_feedback_route_id = _read(
            environ,
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID",
            _parse_route_id,
        )
        active_key_id = _read(
            environ,
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID",
            _parse_key_id,
        )
        key_references = _read(
            environ,
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON",
            _parse_key_references,
        )
        if active_key_id not in {reference.key_id for reference in key_references}:
            raise RuntimeConfigurationError("TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID")
        tool_lease_seconds = _read(
            environ,
            "TRADEOS_TOOL_LEASE_SECONDS",
            _parse_positive_integer,
        )
        try:
            openai_api_key_ref = validate_environment_secret_reference(
                environ.get("OPENAI_API_KEY_REF", "OPENAI_API_KEY")
            )
        except ValidationError:
            raise RuntimeConfigurationError("OPENAI_API_KEY_REF") from None
        trade_manager_model = _parse_safe_reference(
            environ.get("TRADEOS_TRADE_MANAGER_MODEL", "gpt-5-mini")
        )
        tavily_ref = None
        if "TAVILY_API_KEY_REF" in environ:
            tavily_ref = _read(environ, "TAVILY_API_KEY_REF", validate_environment_secret_reference)
        exclusive = environ.get("TRADEOS_TAVILY_EXCLUSIVE_ACCOUNT_CONFIRMED", "false")
        if exclusive not in {"true", "false"}:
            raise RuntimeConfigurationError("TRADEOS_TAVILY_EXCLUSIVE_ACCOUNT_CONFIRMED")
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
            email_feedback_route_id=email_feedback_route_id,
            unsubscribe_active_key_id=active_key_id,
            unsubscribe_key_refs=key_references,
            tool_lease=timedelta(seconds=tool_lease_seconds),
            openai_api_key_ref=openai_api_key_ref,
            trade_manager_model=trade_manager_model,
            tavily_api_key_ref=tavily_ref,
            tavily_exclusive_account_confirmed=exclusive == "true",
        )
