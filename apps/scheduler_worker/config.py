"""scheduler worker 的严格、脱敏生产配置。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from shared.errors import ValidationError
from shared.schemas.dns_auth import DnsAuthenticationRequest
from shared.schemas.identifiers import TenantId

_TENANT = re.compile(r"tn_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
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
)


def _integer(environ: Mapping[str, str], name: str, *, minimum: int) -> int:
    try:
        value = int(environ[name])
    except (KeyError, TypeError, ValueError):
        raise ValidationError("scheduler worker 配置无效") from None
    if value < minimum:
        raise ValidationError("scheduler worker 配置无效")
    return value


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

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> SchedulerWorkerConfig:
        if not isinstance(environ, Mapping) or any(
            name not in environ for name in _REQUIRED
        ):
            raise ValidationError("scheduler worker 配置无效")
        database_url = environ["DATABASE_URL"]
        tenant = environ["TRADEOS_TENANT_ID"]
        selector = environ["TRADEOS_DKIM_SELECTOR"]
        key_ref = environ["TOOL_CALL_FINGERPRINT_KEY_REF"]
        key_version = environ["TOOL_CALL_FINGERPRINT_KEY_VERSION"]
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
        ):
            raise ValidationError("scheduler worker 配置无效")
        DnsAuthenticationRequest("validation.example", selector)
        lock_key = _integer(environ, "TRADEOS_SCHEDULER_LOCK_KEY", minimum=-(2**63))
        health_port = _integer(
            environ, "TRADEOS_SCHEDULER_HEALTH_PORT", minimum=1
        )
        tool_lease_seconds = _integer(
            environ, "TRADEOS_TOOL_LEASE_SECONDS", minimum=1
        )
        if (
            lock_key > 2**63 - 1
            or health_port > 65_535
            or tool_lease_seconds > 120
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
        )
