"""scheduler worker 的严格、脱敏生产配置。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import urlsplit

from infra.secrets import validate_environment_secret_reference
from shared.errors import ValidationError
from shared.schemas.dns_auth import DnsAuthenticationRequest
from shared.schemas.identifiers import TenantId
from tool_gateway.provider_readiness import ProviderConfiguration
from workflows.email_feedback.unsubscribe import UnsubscribeKeyReference

_TENANT = re.compile(r"tn_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_REFERENCE = re.compile(r"[A-Z][A-Z0-9_]{2,99}")
_ROUTE_ID = re.compile(r"[a-z0-9-]{1,32}")
_KEY_ID = re.compile(r"[a-z0-9-]{1,32}")
_CREDENTIAL_MARKERS = ("bearer", "token", "secret", "password")
_REQUIRED = (
    "DATABASE_URL",
    "TRADEOS_TENANT_ID",
    "TRADEOS_SCHEDULER_INTERVAL_SECONDS",
    "TRADEOS_SCHEDULER_BATCH_LIMIT",
    "TRADEOS_SCHEDULER_LOCK_KEY",
    "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS",
    "TRADEOS_HANDOFF_T1_SECONDS",
    "TRADEOS_HANDOFF_T2_SECONDS",
    "TRADEOS_DKIM_SELECTOR",
    "TRADEOS_SCHEDULER_HEALTH_PORT",
    "TRADEOS_TOOL_LEASE_SECONDS",
    "TOOL_CALL_FINGERPRINT_KEY_REF",
    "TOOL_CALL_FINGERPRINT_KEY_VERSION",
    "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS",
    "GMAIL_OAUTH_TOKEN_REF",
    "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID",
    "TRADEOS_UNSUBSCRIBE_BASE_URL",
    "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID",
    "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON",
    "TRADEOS_HUNTER_CONTACTS_ENABLED",
)
_HUNTER_COMPANION_METADATA = (
    "TRADEOS_HUNTER_CONFIGURATION_VERSION",
    "TRADEOS_HUNTER_API_KEY_SECRET_REF",
    "TRADEOS_HUNTER_API_KEY_VERSION",
)


def _nonblank(value: str) -> str:
    if not value or value != value.strip():
        raise ValidationError("scheduler worker 配置无效")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError("scheduler worker 配置无效")
    return value


def _unsubscribe_origin(value: str) -> str:
    value = _nonblank(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValidationError("scheduler worker 配置无效") from None
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not hostname.isascii()
        or hostname != hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValidationError("scheduler worker 配置无效")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValidationError("scheduler worker 配置无效")
    host = f"[{hostname}]" if ":" in hostname else hostname
    canonical = f"{parsed.scheme}://{host}"
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    if port is not None and not default_port:
        canonical = f"{canonical}:{port}"
    if value != canonical:
        raise ValidationError("scheduler worker 配置无效")
    return value


def _unsubscribe_key_refs(value: str) -> tuple[UnsubscribeKeyReference, ...]:
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
        raise ValidationError("scheduler worker 配置无效") from None
    if not isinstance(payload, dict) or not payload:
        raise ValidationError("scheduler worker 配置无效")
    references: list[UnsubscribeKeyReference] = []
    for key_id, secret_ref in payload.items():
        if _KEY_ID.fullmatch(key_id) is None or not isinstance(secret_ref, str):
            raise ValidationError("scheduler worker 配置无效")
        references.append(
            UnsubscribeKeyReference(
                key_id, validate_environment_secret_reference(secret_ref)
            )
        )
    return tuple(references)


def _integer(environ: Mapping[str, str], name: str, *, minimum: int) -> int:
    try:
        value = int(environ[name])
    except (KeyError, TypeError, ValueError):
        raise ValidationError("scheduler worker 配置无效") from None
    if value < minimum:
        raise ValidationError("scheduler worker 配置无效")
    return value


@dataclass(frozen=True)
class HunterContactsSettings:
    """仅保存 Hunter 部署元数据，禁止在此解析或接触密钥值。"""

    enabled: bool
    configuration: ProviderConfiguration | None
    secret_ref: str | None = field(repr=False)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        """严格解析显式开关与部署元数据，缺失或不安全输入均失败关闭。"""
        try:
            enabled = environ["TRADEOS_HUNTER_CONTACTS_ENABLED"]
        except (KeyError, TypeError):
            raise ValidationError("scheduler worker 配置无效") from None
        if enabled == "false":
            for name in _HUNTER_COMPANION_METADATA:
                value = environ.get(name, "")
                if not isinstance(value, str) or value:
                    raise ValidationError("scheduler worker 配置无效")
            return cls(False, None, None)
        if enabled != "true":
            raise ValidationError("scheduler worker 配置无效")
        try:
            configuration_version = environ[
                "TRADEOS_HUNTER_CONFIGURATION_VERSION"
            ]
            secret_ref = environ["TRADEOS_HUNTER_API_KEY_SECRET_REF"]
            api_key_version = environ["TRADEOS_HUNTER_API_KEY_VERSION"]
            safe_secret_ref = validate_environment_secret_reference(secret_ref)
            configuration = ProviderConfiguration.hunter_contacts(
                configuration_version, api_key_version
            )
        except (KeyError, TypeError, ValidationError):
            raise ValidationError("scheduler worker 配置无效") from None
        return cls(True, configuration, safe_secret_ref)


@dataclass(frozen=True)
class SchedulerWorkerConfig:
    database_url: str = field(repr=False)
    tenant_id: TenantId
    interval_seconds: int
    batch_limit: int
    lock_key: int
    outbox_max_attempts: int
    handoff_t1_seconds: int
    handoff_t2_seconds: int
    dkim_selector: str
    health_port: int
    tool_lease_seconds: int
    fingerprint_key_ref: str = field(repr=False)
    fingerprint_key_version: str = field(repr=False)
    campaign_retry_interval_seconds: int
    gmail_oauth_token_ref: str = field(repr=False)
    email_feedback_route_id: str
    unsubscribe_base_url: str
    unsubscribe_active_key_id: str
    unsubscribe_key_refs: tuple[UnsubscribeKeyReference, ...]
    hunter_contacts: HunterContactsSettings

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> SchedulerWorkerConfig:
        if not isinstance(environ, Mapping) or any(
            name not in environ for name in _REQUIRED
        ):
            raise ValidationError("scheduler worker 配置无效")
        hunter_contacts = HunterContactsSettings.from_environ(environ)
        database_url = environ["DATABASE_URL"]
        tenant = environ["TRADEOS_TENANT_ID"]
        selector = environ["TRADEOS_DKIM_SELECTOR"]
        key_ref = environ["TOOL_CALL_FINGERPRINT_KEY_REF"]
        key_version = environ["TOOL_CALL_FINGERPRINT_KEY_VERSION"]
        gmail_ref = environ["GMAIL_OAUTH_TOKEN_REF"]
        route_id = environ["TRADEOS_EMAIL_FEEDBACK_ROUTE_ID"]
        unsubscribe_url = _unsubscribe_origin(environ["TRADEOS_UNSUBSCRIBE_BASE_URL"])
        active_key_id = environ["TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID"]
        key_references = _unsubscribe_key_refs(
            environ["TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON"]
        )
        if (
            not isinstance(database_url, str)
            or not database_url.startswith("postgresql+asyncpg://")
            or any(character.isspace() for character in database_url)
            or not isinstance(tenant, str)
            or _TENANT.fullmatch(tenant) is None
            or not isinstance(key_ref, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", key_ref) is None
            or not isinstance(key_version, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", key_version) is None
            or not isinstance(gmail_ref, str)
            or _REFERENCE.fullmatch(gmail_ref) is None
            or not isinstance(route_id, str)
            or _ROUTE_ID.fullmatch(route_id) is None
            or any(marker in route_id for marker in _CREDENTIAL_MARKERS)
            or not isinstance(active_key_id, str)
            or _KEY_ID.fullmatch(active_key_id) is None
        ):
            raise ValidationError("scheduler worker 配置无效")
        if active_key_id not in {reference.key_id for reference in key_references}:
            raise ValidationError("scheduler worker 配置无效")
        DnsAuthenticationRequest("validation.example", selector)
        lock_key = _integer(environ, "TRADEOS_SCHEDULER_LOCK_KEY", minimum=-(2**63))
        health_port = _integer(
            environ, "TRADEOS_SCHEDULER_HEALTH_PORT", minimum=1
        )
        tool_lease_seconds = _integer(
            environ, "TRADEOS_TOOL_LEASE_SECONDS", minimum=1
        )
        campaign_retry_interval_seconds = _integer(
            environ, "TRADEOS_CAMPAIGN_RETRY_INTERVAL_SECONDS", minimum=1
        )
        if (
            lock_key > 2**63 - 1
            or health_port > 65_535
            or tool_lease_seconds > 120
            or campaign_retry_interval_seconds > 86_400
        ):
            raise ValidationError("scheduler worker 配置无效")
        return cls(
            database_url,
            TenantId(tenant),
            _integer(environ, "TRADEOS_SCHEDULER_INTERVAL_SECONDS", minimum=1),
            _integer(environ, "TRADEOS_SCHEDULER_BATCH_LIMIT", minimum=1),
            lock_key,
            _integer(environ, "TRADEOS_SCHEDULER_OUTBOX_MAX_ATTEMPTS", minimum=1),
            _integer(environ, "TRADEOS_HANDOFF_T1_SECONDS", minimum=1),
            _integer(environ, "TRADEOS_HANDOFF_T2_SECONDS", minimum=1),
            selector,
            health_port,
            tool_lease_seconds,
            key_ref,
            key_version,
            campaign_retry_interval_seconds,
            gmail_ref,
            route_id,
            unsubscribe_url,
            active_key_id,
            key_references,
            hunter_contacts,
        )
