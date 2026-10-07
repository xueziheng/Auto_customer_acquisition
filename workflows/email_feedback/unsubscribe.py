"""One-click unsubscribe capability token 的签发与一次性消费。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlsplit

from domains.outreach.schemas import MessageSendPreflight
from domains.outreach.service import (
    Actor,
    SuppressionReason,
    SuppressionRequest,
    SuppressionTarget,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    IdempotencyKey,
    MessageAttemptId,
    TenantId,
)
from workflows.email_feedback.repository import (
    FeedbackPageUnitOfWork,
    UnsubscribeTokenRecord,
)

__all__ = [
    "UnsubscribeKeyReference",
    "UnsubscribeKeyRing",
    "UnsubscribeLink",
    "UnsubscribeMetrics",
    "UnsubscribeService",
    "UnsubscribeServiceImpl",
]

_KEY_ID_RE = re.compile(r"[a-z0-9-]{1,32}")
_TOKEN_PART_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_TOKEN_RE = re.compile(r"([a-z0-9-]{1,32})\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})")
_DOMAIN = b"tradeos-unsubscribe-v1\0"
_POLICY_VERSION = "unsubscribe-v1"
_NONCE_ATTEMPTS = 3
_logger = logging.getLogger("workflows.email_feedback.unsubscribe")


@dataclass(frozen=True)
class UnsubscribeKeyReference:
    key_id: str
    secret_ref: str = field(repr=False)

    def __post_init__(self) -> None:
        if _KEY_ID_RE.fullmatch(self.key_id) is None:
            raise ValidationError("退订 key id 无效")
        if (
            not isinstance(self.secret_ref, str)
            or not self.secret_ref
            or self.secret_ref != self.secret_ref.strip()
        ):
            raise ValidationError("退订 secret ref 无效")


@dataclass(frozen=True, repr=False)
class UnsubscribeKeyRing:
    active_key_id: str
    keys: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.active_key_id, str)
            or _KEY_ID_RE.fullmatch(self.active_key_id) is None
            or not isinstance(self.keys, Mapping)
            or self.active_key_id not in self.keys
            or not self.keys
        ):
            raise ValidationError("退订 key ring 无效")
        copied: dict[str, bytes] = {}
        for key_id, key in self.keys.items():
            if (
                not isinstance(key_id, str)
                or _KEY_ID_RE.fullmatch(key_id) is None
                or not isinstance(key, bytes)
                or len(key) < 32
            ):
                raise ValidationError("退订 key ring 无效")
            copied[key_id] = bytes(key)
        object.__setattr__(self, "keys", MappingProxyType(copied))

    def key(self, key_id: str) -> bytes | None:
        return self.keys.get(key_id)


@dataclass(frozen=True)
class UnsubscribeLink:
    tenant_id: TenantId = field(repr=False)
    attempt_id: MessageAttemptId = field(repr=False)
    contact_point_id: ContactPointId = field(repr=False)
    url: str = field(repr=False)
    active_key_id: str
    policy_version: str


@runtime_checkable
class UnsubscribeMetrics(Protocol):
    def increment(self, result: Literal["valid", "expired", "invalid"]) -> None: ...


@runtime_checkable
class UnsubscribeService(Protocol):
    async def issue(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink: ...

    async def consume(self, opaque_token: str) -> bool: ...


class _NoopMetrics:
    def increment(self, result: Literal["valid", "expired", "invalid"]) -> None:
        del result


class FeedbackPageUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> FeedbackPageUnitOfWork: ...


class OutreachActorFactory(Protocol):
    def __call__(self, contact_point_id: ContactPointId) -> Actor: ...


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes | None:
    if _TOKEN_PART_RE.fullmatch(value) is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, TypeError):
        return None
    if len(decoded) != 32 or _b64encode(decoded) != value:
        return None
    return decoded


def _is_canonical_origin(value: object) -> bool:
    if not isinstance(value, str) or not value.isascii():
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or not hostname.isascii()
        or hostname != hostname.lower()
        or hostname.endswith(".")
        or ".." in hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    host = f"[{hostname}]" if ":" in hostname else hostname
    canonical = f"{parsed.scheme}://{host}"
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    if port is not None and not default_port:
        canonical = f"{canonical}:{port}"
    return value == canonical


def _require_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("退订时钟无效")
    return value


class UnsubscribeServiceImpl:
    """签发只持久化 nonce 摘要；消费与 Outreach suppression 同事务。"""

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        uow_factory: FeedbackPageUnitOfWorkFactory,
        key_ring: UnsubscribeKeyRing,
        base_url: str,
        actor_factory: OutreachActorFactory,
        now: Callable[[], datetime],
        nonce_factory: Callable[[], bytes] | None = None,
        metrics: UnsubscribeMetrics | None = None,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValidationError("退订 tenant 无效")
        if (
            not isinstance(key_ring, UnsubscribeKeyRing)
            or not _is_canonical_origin(base_url)
        ):
            raise ValidationError("退订服务配置无效")
        self._tenant_id = tenant_id
        self._uow_factory = uow_factory
        self._key_ring = key_ring
        self._base_url = base_url
        self._actor_factory = actor_factory
        self._now = now
        self._nonce_factory = nonce_factory or (lambda: secrets.token_bytes(32))
        self._metrics = metrics or _NoopMetrics()

    def _metric(self, result: Literal["valid", "expired", "invalid"]) -> None:
        try:
            self._metrics.increment(result)
        except BaseException:  # noqa: BLE001 指标失败不改变 capability 结果
            _logger.error("退订指标写入失败")

    def _signature(self, key_id: str, nonce: bytes) -> bytes:
        key = self._key_ring.key(key_id)
        if key is None:
            raise ValidationError("退订 token 无效")
        material = (
            _DOMAIN
            + str(self._tenant_id).encode("ascii")
            + b"\0"
            + key_id.encode("ascii")
            + b"\0"
            + nonce
        )
        return hmac.new(key, material, hashlib.sha256).digest()

    async def issue(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> UnsubscribeLink:
        if (
            tenant_id != self._tenant_id
            or not isinstance(preflight, MessageSendPreflight)
            or preflight.tenant_id != tenant_id
        ):
            raise ValidationError("退订签发绑定无效")
        created_at = _require_utc(self._now())
        key_id = self._key_ring.active_key_id
        async with self._uow_factory(tenant_id) as uow:
            for _attempt in range(_NONCE_ATTEMPTS):
                nonce = self._nonce_factory()
                if not isinstance(nonce, bytes) or len(nonce) != 32:
                    raise ValidationError("退订 nonce 无效")
                digest = hashlib.sha256(nonce).digest()
                record = UnsubscribeTokenRecord(
                    tenant_id=tenant_id,
                    nonce_sha256=digest,
                    contact_point_id=preflight.contact_point_id,
                    message_attempt_id=preflight.attempt_id,
                    key_id=key_id,
                    expires_at=created_at + timedelta(days=90),
                    consumed_at=None,
                    created_at=created_at,
                )
                if await uow.tokens.add_if_absent(record):
                    signature = self._signature(key_id, nonce)
                    token = f"{key_id}.{_b64encode(nonce)}.{_b64encode(signature)}"
                    return UnsubscribeLink(
                        tenant_id,
                        preflight.attempt_id,
                        preflight.contact_point_id,
                        f"{self._base_url}/unsubscribe/{token}",
                        key_id,
                        _POLICY_VERSION,
                    )
        raise ValidationError("退订 token 生成失败")

    def _parse(self, opaque_token: object) -> tuple[str, bytes] | None:
        if not isinstance(opaque_token, str) or len(opaque_token) > 160:
            return None
        matched = _TOKEN_RE.fullmatch(opaque_token)
        if matched is None:
            return None
        key_id, nonce_text, signature_text = matched.groups()
        nonce = _b64decode(nonce_text)
        signature = _b64decode(signature_text)
        key = self._key_ring.key(key_id)
        if nonce is None or signature is None or key is None:
            return None
        expected = self._signature(key_id, nonce)
        if not hmac.compare_digest(signature, expected):
            return None
        return key_id, nonce

    async def consume(self, opaque_token: str) -> bool:
        parsed = self._parse(opaque_token)
        if parsed is None:
            self._metric("invalid")
            return False
        key_id, nonce = parsed
        digest = hashlib.sha256(nonce).digest()
        now = _require_utc(self._now())
        async with self._uow_factory(self._tenant_id) as uow:
            record = await uow.tokens.get_for_update(self._tenant_id, digest)
            if record is None or record.key_id != key_id or record.consumed_at is not None:
                self._metric("invalid")
                return False
            if now >= record.expires_at:
                self._metric("expired")
                return False
            safe_ref = digest.hex()
            await uow.outreach.add_suppression(
                self._tenant_id,
                SuppressionRequest(
                    target=SuppressionTarget(contact_point_id=record.contact_point_id),
                    reason=SuppressionReason.UNSUBSCRIBE,
                    occurred_at=now,
                    source_ref=safe_ref,
                    idempotency_key=IdempotencyKey(safe_ref),
                ),
                actor=self._actor_factory(record.contact_point_id),
            )
            if not await uow.tokens.mark_consumed(record, now):
                raise TransientError("退订 token 消费冲突")
        self._metric("valid")
        return True
