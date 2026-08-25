"""Provider 运行就绪的纯契约、状态推导与 fail-closed 守卫。

本模块只记录可安全持久化的运行元数据；它不解析凭证、不访问网络，也不依赖
持久化实现。数据库事件流由下层实现，所有状态从其 append-only 事实推导。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import TracebackType
from typing import Any, NewType, Protocol, Self, runtime_checkable

from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import IdempotencyKey, TenantId, new_id

ProviderReadinessEventId = NewType("ProviderReadinessEventId", str)

_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}\Z")
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_EVENT_ID_RE = re.compile(r"pre_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_SAFE_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_SECRET_MARKERS = ("secret", "token", "password", "authorization", "bearer")
_HUNTER_CONNECTOR_PROFILE_VERSION = "hunter-contacts-v1"
_HUNTER_TRANSPORT_PROFILE = "hunter_api_v2_fixed_host"


class ProviderId(str, Enum):
    HUNTER = "hunter"


class ProviderCapability(str, Enum):
    CONTACT_ENRICHMENT = "contact.enrich"
    CONTACT_VERIFICATION = "contact.verify"


HUNTER_CONTACT_CAPABILITIES = (
    ProviderCapability.CONTACT_ENRICHMENT,
    ProviderCapability.CONTACT_VERIFICATION,
)


class ProviderReadinessEventType(str, Enum):
    CONFIGURED = "configured"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    RUNTIME_COMPOSED = "runtime_composed"


class ProviderReadinessState(str, Enum):
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    VALIDATION_NOT_RUN = "validation_not_run"
    VALIDATION_INCONCLUSIVE = "validation_inconclusive"
    VALIDATION_FAILED = "validation_failed"
    RUNTIME_NOT_COMPOSED = "runtime_not_composed"
    READY = "ready"


class ProviderReadinessPermission(str, Enum):
    CONFIGURE = "configure"
    VALIDATE = "validate"
    COMPOSE = "compose"
    READ = "read"


class ProviderValidationFailureCode(str, Enum):
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    RESPONSE_INVALID = "response_invalid"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ProviderReadinessUnavailableError(TransientError):
    """当前进程不能安全使用 Provider；不泄露配置或存储错误详情。"""

    def __init__(self) -> None:
        super().__init__("Provider 配置当前不可用")


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """编码确定性、安全元数据；调用方不得传入凭证或运行时请求内容。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def require_version(value: str) -> str:
    """仅接受无空白、无凭证标记的部署版本标签。"""
    if (
        not isinstance(value, str)
        or _VERSION_RE.fullmatch(value) is None
        or _contains_secret_marker(value)
    ):
        raise ValidationError("Provider 配置版本无效")
    return value


def _contains_secret_marker(value: str) -> bool:
    folded = value.casefold()
    return any(marker in folded for marker in _SECRET_MARKERS)


def _require_safe_reference(value: object, message: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_REFERENCE_RE.fullmatch(value) is None
        or _contains_secret_marker(value)
    ):
        raise ValidationError(message)
    return value


def _require_tenant_id(value: object) -> TenantId:
    return TenantId(_require_safe_reference(value, "Provider 租户无效"))


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValidationError("Provider 配置哈希无效")
    return value


def _require_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("Provider 事件时间必须是 UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class ProviderConfiguration:
    """可安全入库的精确 Provider 配置，不含凭证引用、值或其哈希。"""

    provider: ProviderId
    capabilities: tuple[ProviderCapability, ...]
    configuration_version: str
    connector_profile_version: str
    transport_profile: str
    api_key_version: str
    configuration_hash: str

    def __post_init__(self) -> None:
        if self.provider is not ProviderId.HUNTER:
            raise ValidationError("Provider 标识无效")
        _require_hunter_capabilities(self.capabilities)
        if (
            self.connector_profile_version != _HUNTER_CONNECTOR_PROFILE_VERSION
            or self.transport_profile != _HUNTER_TRANSPORT_PROFILE
        ):
            raise ValidationError("Provider profile 无效")
        require_version(self.configuration_version)
        require_version(self.connector_profile_version)
        require_version(self.transport_profile)
        require_version(self.api_key_version)
        _require_hash(self.configuration_hash)
        expected = hashlib.sha256(
            canonical_json(
                {
                    "provider": self.provider.value,
                    "capabilities": [capability.value for capability in self.capabilities],
                    "connector_profile_version": self.connector_profile_version,
                    "transport_profile": self.transport_profile,
                    "configuration_version": self.configuration_version,
                    "api_key_version": self.api_key_version,
                }
            )
        ).hexdigest()
        if self.configuration_hash != expected:
            raise ValidationError("Provider 配置哈希不匹配")

    @classmethod
    def hunter_contacts(cls, configuration_version: str, api_key_version: str) -> Self:
        safe_configuration_version = require_version(configuration_version)
        safe_api_key_version = require_version(api_key_version)
        payload = {
            "provider": "hunter",
            "capabilities": ["contact.enrich", "contact.verify"],
            "connector_profile_version": _HUNTER_CONNECTOR_PROFILE_VERSION,
            "transport_profile": _HUNTER_TRANSPORT_PROFILE,
            "configuration_version": safe_configuration_version,
            "api_key_version": safe_api_key_version,
        }
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        return cls(
            provider=ProviderId.HUNTER,
            capabilities=HUNTER_CONTACT_CAPABILITIES,
            configuration_version=safe_configuration_version,
            connector_profile_version=_HUNTER_CONNECTOR_PROFILE_VERSION,
            transport_profile=_HUNTER_TRANSPORT_PROFILE,
            api_key_version=safe_api_key_version,
            configuration_hash=digest,
        )


def _require_hunter_capabilities(
    capabilities: object,
) -> tuple[ProviderCapability, ...]:
    if capabilities != HUNTER_CONTACT_CAPABILITIES:
        raise ValidationError("Provider 能力集合无效")
    return HUNTER_CONTACT_CAPABILITIES


@dataclass(frozen=True)
class ProviderReadinessActor:
    """由受信 composition root 构造的 tenant-bound 操作身份。"""

    actor_id: str
    tenant_id: TenantId
    permissions: frozenset[ProviderReadinessPermission]

    def __post_init__(self) -> None:
        _require_safe_reference(self.actor_id, "Provider 操作身份无效")
        _require_tenant_id(self.tenant_id)
        if not isinstance(self.permissions, frozenset) or not all(
            isinstance(permission, ProviderReadinessPermission)
            for permission in self.permissions
        ):
            raise ValidationError("Provider 操作权限无效")


@dataclass(frozen=True)
class ProviderReadinessEvent:
    """单个 append-only readiness 事实；所有字段都可安全审计。"""

    tenant_id: TenantId
    event_id: ProviderReadinessEventId
    provider: ProviderId
    capabilities: tuple[ProviderCapability, ...]
    sequence: int | None
    event_type: ProviderReadinessEventType
    configuration: ProviderConfiguration
    validation_key: IdempotencyKey | None
    failure_code: ProviderValidationFailureCode | None
    evidence_ref: str | None
    actor_id: str
    occurred_at: datetime
    idempotency_key: IdempotencyKey

    def __post_init__(self) -> None:
        _require_tenant_id(self.tenant_id)
        if not isinstance(self.event_id, str) or _EVENT_ID_RE.fullmatch(self.event_id) is None:
            raise ValidationError("Provider 事件标识无效")
        if self.provider is not ProviderId.HUNTER:
            raise ValidationError("Provider 事件来源无效")
        _require_hunter_capabilities(self.capabilities)
        if self.sequence is not None and (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise ValidationError("Provider 事件序号无效")
        if not isinstance(self.event_type, ProviderReadinessEventType):
            raise ValidationError("Provider 事件类型无效")
        if (
            not isinstance(self.configuration, ProviderConfiguration)
            or self.configuration.provider is not self.provider
            or self.configuration.capabilities != self.capabilities
        ):
            raise ValidationError("Provider 事件配置无效")
        _require_safe_reference(self.actor_id, "Provider 事件操作身份无效")
        _require_utc(self.occurred_at)
        _require_safe_reference(self.idempotency_key, "Provider 事件幂等键无效")
        if self.validation_key is not None:
            _require_safe_reference(self.validation_key, "Provider 验证幂等键无效")
        if self.evidence_ref is not None:
            _require_safe_reference(self.evidence_ref, "Provider 验证证据引用无效")
        if self.failure_code is not None and not isinstance(
            self.failure_code, ProviderValidationFailureCode
        ):
            raise ValidationError("Provider 验证失败分类无效")
        self._validate_event_specific_fields()

    @property
    def outcome_code(self) -> str | None:
        """供持久层映射固定结果分类，绝不保存异常文本。"""
        return self.failure_code.value if self.failure_code is not None else None

    def _validate_event_specific_fields(self) -> None:
        if self.event_type is ProviderReadinessEventType.CONFIGURED:
            valid = (
                self.validation_key is None
                and self.failure_code is None
                and self.evidence_ref is None
            )
        elif self.event_type is ProviderReadinessEventType.VALIDATION_STARTED:
            valid = (
                self.validation_key is not None
                and self.failure_code is None
                and self.evidence_ref is None
            )
        elif self.event_type is ProviderReadinessEventType.VALIDATION_PASSED:
            valid = (
                self.validation_key is not None
                and self.failure_code is None
                and self.evidence_ref is not None
            )
        elif self.event_type is ProviderReadinessEventType.VALIDATION_FAILED:
            valid = (
                self.validation_key is not None
                and self.failure_code is not None
                and self.evidence_ref is None
            )
        else:
            valid = (
                self.validation_key is None
                and self.failure_code is None
                and self.evidence_ref is None
            )
        if not valid:
            raise ValidationError("Provider 事件字段不匹配")


@dataclass(frozen=True)
class ProviderReadinessSnapshot:
    """某 tenant 的当前精确配置及由事件流确定性推导的状态。"""

    tenant_id: TenantId
    provider: ProviderId
    capabilities: tuple[ProviderCapability, ...]
    configuration: ProviderConfiguration | None
    state: ProviderReadinessState
    failure_code: ProviderValidationFailureCode | None
    events: tuple[ProviderReadinessEvent, ...]

    def __post_init__(self) -> None:
        _require_tenant_id(self.tenant_id)
        if self.provider is not ProviderId.HUNTER:
            raise ValidationError("Provider 快照来源无效")
        _require_hunter_capabilities(self.capabilities)
        if self.configuration is not None and (
            self.configuration.provider is not self.provider
            or self.configuration.capabilities != self.capabilities
        ):
            raise ValidationError("Provider 快照配置无效")
        if not isinstance(self.state, ProviderReadinessState):
            raise ValidationError("Provider 快照状态无效")
        if self.failure_code is not None and not isinstance(
            self.failure_code, ProviderValidationFailureCode
        ):
            raise ValidationError("Provider 快照失败分类无效")
        if not isinstance(self.events, tuple) or not all(
            isinstance(event, ProviderReadinessEvent) for event in self.events
        ):
            raise ValidationError("Provider 快照事件无效")


@runtime_checkable
class ProviderReadinessRepository(Protocol):
    """tenant-scoped append-only readiness 事件流仓储。"""

    async def list_events(
        self,
        tenant_id: TenantId,
        provider: ProviderId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> list[ProviderReadinessEvent]: ...

    async def append(
        self, tenant_id: TenantId, event: ProviderReadinessEvent
    ) -> ProviderReadinessEvent: ...


@runtime_checkable
class ProviderReadinessUnitOfWork(Protocol):
    """单次 readiness 读写使用的 tenant-bound 事务边界。"""

    readiness: ProviderReadinessRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class ProviderReadinessUnitOfWorkFactory(Protocol):
    """按 tenant 构造隔离的 readiness 事务。"""

    def __call__(self, tenant_id: TenantId) -> ProviderReadinessUnitOfWork: ...


@runtime_checkable
class ProviderReadinessService(Protocol):
    async def get_snapshot(
        self,
        tenant_id: TenantId,
        capabilities: tuple[ProviderCapability, ...],
        *,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessSnapshot: ...

    async def declare_configuration(
        self,
        tenant_id: TenantId,
        configuration: ProviderConfiguration,
        *,
        actor: ProviderReadinessActor,
        idempotency_key: IdempotencyKey,
    ) -> ProviderReadinessSnapshot: ...

    async def mark_validation_started(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        validation_key: IdempotencyKey,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessEvent: ...

    async def mark_validation_passed(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        validation_key: IdempotencyKey,
        evidence_ref: str,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessEvent: ...

    async def mark_validation_failed(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        validation_key: IdempotencyKey,
        failure_code: ProviderValidationFailureCode,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessEvent: ...

    async def mark_runtime_composed(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        actor: ProviderReadinessActor,
        idempotency_key: IdempotencyKey,
    ) -> ProviderReadinessEvent: ...


@runtime_checkable
class ProviderRuntimeGuard(Protocol):
    async def require_current(
        self, tenant_id: TenantId, configuration_hash: str
    ) -> None: ...


class ProviderReadinessServiceImpl:
    """组合授权、事件校验与状态推导；不含任何 Provider IO 或凭证依赖。"""

    def __init__(
        self,
        uow_factory: ProviderReadinessUnitOfWorkFactory,
        *,
        runtime_actor: ProviderReadinessActor,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("Provider readiness 事务依赖无效")
        if not isinstance(runtime_actor, ProviderReadinessActor):
            raise ValidationError("Provider runtime 操作身份无效")
        self._uow_factory = uow_factory
        self._runtime_actor = runtime_actor
        self._now = now or (lambda: datetime.now(UTC))

    async def get_snapshot(
        self,
        tenant_id: TenantId,
        capabilities: tuple[ProviderCapability, ...],
        *,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessSnapshot:
        self._require_actor(tenant_id, actor, ProviderReadinessPermission.READ)
        _require_hunter_capabilities(capabilities)
        events = await self._load_events(tenant_id, capabilities)
        return _snapshot(tenant_id, capabilities, events)

    async def declare_configuration(
        self,
        tenant_id: TenantId,
        configuration: ProviderConfiguration,
        *,
        actor: ProviderReadinessActor,
        idempotency_key: IdempotencyKey,
    ) -> ProviderReadinessSnapshot:
        self._require_actor(
            tenant_id, actor, ProviderReadinessPermission.CONFIGURE
        )
        if not isinstance(configuration, ProviderConfiguration):
            raise ValidationError("Provider 配置无效")
        events = await self._load_events(tenant_id, configuration.capabilities)
        event = self._new_event(
            tenant_id,
            configuration,
            ProviderReadinessEventType.CONFIGURED,
            actor,
            idempotency_key,
        )
        persisted = await self._append(events, event)
        return _snapshot(
            tenant_id,
            configuration.capabilities,
            _with_appended(events, persisted),
        )

    async def mark_validation_started(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        validation_key: IdempotencyKey,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessEvent:
        self._require_actor(tenant_id, actor, ProviderReadinessPermission.VALIDATE)
        events, configuration = await self._current_events(tenant_id, configuration_hash)
        event = self._new_event(
            tenant_id,
            configuration,
            ProviderReadinessEventType.VALIDATION_STARTED,
            actor,
            _validation_event_idempotency("started", validation_key),
            validation_key=validation_key,
        )
        existing = _idempotency_event(events, event)
        if existing is not None:
            return existing
        if not _can_start_validation(events):
            raise InvalidStateTransition("Provider 当前配置不可开始验证")
        return await self._append(events, event)

    async def mark_validation_passed(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        validation_key: IdempotencyKey,
        evidence_ref: str,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessEvent:
        self._require_actor(tenant_id, actor, ProviderReadinessPermission.VALIDATE)
        events, configuration = await self._current_events(tenant_id, configuration_hash)
        event = self._new_event(
            tenant_id,
            configuration,
            ProviderReadinessEventType.VALIDATION_PASSED,
            actor,
            _validation_event_idempotency("passed", validation_key),
            validation_key=validation_key,
            evidence_ref=evidence_ref,
        )
        return await self._append_validation_terminal(events, event)

    async def mark_validation_failed(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        validation_key: IdempotencyKey,
        failure_code: ProviderValidationFailureCode,
        actor: ProviderReadinessActor,
    ) -> ProviderReadinessEvent:
        self._require_actor(tenant_id, actor, ProviderReadinessPermission.VALIDATE)
        events, configuration = await self._current_events(tenant_id, configuration_hash)
        event = self._new_event(
            tenant_id,
            configuration,
            ProviderReadinessEventType.VALIDATION_FAILED,
            actor,
            _validation_event_idempotency("failed", validation_key),
            validation_key=validation_key,
            failure_code=failure_code,
        )
        return await self._append_validation_terminal(events, event)

    async def mark_runtime_composed(
        self,
        tenant_id: TenantId,
        configuration_hash: str,
        *,
        actor: ProviderReadinessActor,
        idempotency_key: IdempotencyKey,
    ) -> ProviderReadinessEvent:
        self._require_actor(tenant_id, actor, ProviderReadinessPermission.COMPOSE)
        events, configuration = await self._current_events(tenant_id, configuration_hash)
        event = self._new_event(
            tenant_id,
            configuration,
            ProviderReadinessEventType.RUNTIME_COMPOSED,
            actor,
            idempotency_key,
        )
        existing = _idempotency_event(events, event)
        if existing is not None:
            return existing
        if _snapshot(tenant_id, configuration.capabilities, events).state is not ProviderReadinessState.RUNTIME_NOT_COMPOSED:
            raise InvalidStateTransition("Provider runtime 缺少当前验证通过事实")
        return await self._append(events, event)

    async def require_current(
        self, tenant_id: TenantId, configuration_hash: str
    ) -> None:
        """仅在精确当前配置已验证且已组合时允许创建 Provider connector。"""
        try:
            _require_hash(configuration_hash)
            snapshot = await self.get_snapshot(
                tenant_id,
                HUNTER_CONTACT_CAPABILITIES,
                actor=self._runtime_actor,
            )
            if (
                snapshot.state is not ProviderReadinessState.READY
                or snapshot.configuration is None
                or snapshot.configuration.configuration_hash != configuration_hash
            ):
                raise ProviderReadinessUnavailableError()
        except ProviderReadinessUnavailableError:
            raise
        except Exception:  # noqa: BLE001 - guard must sanitize every storage failure.
            raise ProviderReadinessUnavailableError() from None

    def _require_actor(
        self,
        tenant_id: TenantId,
        actor: ProviderReadinessActor,
        permission: ProviderReadinessPermission,
    ) -> None:
        _require_tenant_id(tenant_id)
        if not isinstance(actor, ProviderReadinessActor):
            raise PermissionDenied("Provider readiness 授权拒绝")
        if actor.tenant_id != tenant_id:
            raise TenantIsolationViolation("Provider readiness 租户不匹配")
        if permission not in actor.permissions:
            raise PermissionDenied("Provider readiness 授权拒绝")

    async def _current_events(
        self, tenant_id: TenantId, configuration_hash: str
    ) -> tuple[list[ProviderReadinessEvent], ProviderConfiguration]:
        _require_hash(configuration_hash)
        events = await self._load_events(tenant_id, HUNTER_CONTACT_CAPABILITIES)
        configuration = _current_configuration(events)
        if (
            configuration is None
            or configuration.configuration_hash != configuration_hash
        ):
            raise InvalidStateTransition("Provider 配置不是当前配置")
        return events, configuration

    async def _load_events(
        self,
        tenant_id: TenantId,
        capabilities: tuple[ProviderCapability, ...],
    ) -> list[ProviderReadinessEvent]:
        try:
            async with self._uow_factory(tenant_id) as uow:
                events = await uow.readiness.list_events(
                    tenant_id, ProviderId.HUNTER, capabilities
                )
        except (TenantIsolationViolation, ValidationError):
            raise
        except Exception:  # noqa: BLE001 - storage errors must not expose implementation details.
            raise TransientError("Provider readiness 存储不可用") from None
        _validate_loaded_events(tenant_id, capabilities, events)
        return events

    async def _append(
        self,
        events: list[ProviderReadinessEvent],
        event: ProviderReadinessEvent,
    ) -> ProviderReadinessEvent:
        existing = _idempotency_event(events, event)
        if existing is not None:
            return existing
        try:
            async with self._uow_factory(event.tenant_id) as uow:
                persisted = await uow.readiness.append(event.tenant_id, event)
        except (IdempotencyConflict, InvalidStateTransition, TenantIsolationViolation, ValidationError):
            raise
        except Exception:  # noqa: BLE001 - storage errors must not expose implementation details.
            raise TransientError("Provider readiness 存储不可用") from None
        _validate_persisted_event(event, persisted)
        return persisted

    async def _append_validation_terminal(
        self,
        events: list[ProviderReadinessEvent],
        event: ProviderReadinessEvent,
    ) -> ProviderReadinessEvent:
        existing = _idempotency_event(events, event)
        if existing is not None:
            return existing
        if not _has_unterminated_start(events, event.validation_key):
            raise InvalidStateTransition("Provider 验证缺少匹配的已开始事实")
        return await self._append(events, event)

    def _new_event(
        self,
        tenant_id: TenantId,
        configuration: ProviderConfiguration,
        event_type: ProviderReadinessEventType,
        actor: ProviderReadinessActor,
        idempotency_key: IdempotencyKey,
        *,
        validation_key: IdempotencyKey | None = None,
        failure_code: ProviderValidationFailureCode | None = None,
        evidence_ref: str | None = None,
    ) -> ProviderReadinessEvent:
        return ProviderReadinessEvent(
            tenant_id=tenant_id,
            event_id=ProviderReadinessEventId(new_id("pre")),
            provider=configuration.provider,
            capabilities=configuration.capabilities,
            sequence=None,
            event_type=event_type,
            configuration=configuration,
            validation_key=validation_key,
            failure_code=failure_code,
            evidence_ref=evidence_ref,
            actor_id=actor.actor_id,
            occurred_at=_require_utc(self._now()),
            idempotency_key=idempotency_key,
        )


def _validate_loaded_events(
    tenant_id: TenantId,
    capabilities: tuple[ProviderCapability, ...],
    events: object,
) -> None:
    if not isinstance(events, list):
        raise TransientError("Provider readiness 存储返回无效")
    last_sequence = 0
    for event in events:
        if (
            not isinstance(event, ProviderReadinessEvent)
            or event.tenant_id != tenant_id
            or event.provider is not ProviderId.HUNTER
            or event.capabilities != capabilities
            or event.sequence is None
            or event.sequence <= last_sequence
        ):
            raise TransientError("Provider readiness 存储返回无效")
        last_sequence = event.sequence
    _validate_loaded_stream(events)


def _validate_loaded_stream(events: list[ProviderReadinessEvent]) -> None:
    """验证持久化流的因果关系，避免损坏事实被状态推导误认成 ready。"""
    current_configuration: ProviderConfiguration | None = None
    open_validations: set[IdempotencyKey] = set()
    latest_validation: ProviderReadinessEventType | None = None
    for event in events:
        if event.event_type is ProviderReadinessEventType.CONFIGURED:
            current_configuration = event.configuration
            open_validations.clear()
            latest_validation = None
            continue
        if current_configuration is None or event.configuration != current_configuration:
            raise TransientError("Provider readiness 存储返回无效")
        if event.event_type is ProviderReadinessEventType.VALIDATION_STARTED:
            if (
                event.validation_key is None
                or open_validations
                or latest_validation
                not in {None, ProviderReadinessEventType.VALIDATION_FAILED}
            ):
                raise TransientError("Provider readiness 存储返回无效")
            open_validations.add(event.validation_key)
            latest_validation = event.event_type
            continue
        if event.event_type in {
            ProviderReadinessEventType.VALIDATION_PASSED,
            ProviderReadinessEventType.VALIDATION_FAILED,
        }:
            if (
                event.validation_key is None
                or event.validation_key not in open_validations
            ):
                raise TransientError("Provider readiness 存储返回无效")
            open_validations.remove(event.validation_key)
            latest_validation = event.event_type
            continue
        if latest_validation is not ProviderReadinessEventType.VALIDATION_PASSED:
            raise TransientError("Provider readiness 存储返回无效")


def _validate_persisted_event(
    requested: ProviderReadinessEvent, persisted: object
) -> None:
    if (
        not isinstance(persisted, ProviderReadinessEvent)
        or persisted.sequence is None
        or not _same_operation(persisted, requested)
    ):
        raise TransientError("Provider readiness 存储写入无效")


def _idempotency_event(
    events: list[ProviderReadinessEvent], event: ProviderReadinessEvent
) -> ProviderReadinessEvent | None:
    for existing in events:
        if existing.idempotency_key == event.idempotency_key:
            if _same_operation(existing, event):
                return existing
            raise IdempotencyConflict("Provider readiness 幂等键冲突")
    return None


def _same_operation(
    first: ProviderReadinessEvent, second: ProviderReadinessEvent
) -> bool:
    return (
        first.tenant_id == second.tenant_id
        and first.provider is second.provider
        and first.capabilities == second.capabilities
        and first.event_type is second.event_type
        and first.configuration == second.configuration
        and first.validation_key == second.validation_key
        and first.failure_code is second.failure_code
        and first.evidence_ref == second.evidence_ref
        and first.idempotency_key == second.idempotency_key
    )


def _validation_event_idempotency(
    phase: str, validation_key: IdempotencyKey
) -> IdempotencyKey:
    """把同一 Gateway validation key 分成 append-only 的开始与终态事实。"""
    _require_safe_reference(validation_key, "Provider 验证幂等键无效")
    return IdempotencyKey(f"validation:{phase}:{validation_key}")


def _current_configuration(
    events: list[ProviderReadinessEvent],
) -> ProviderConfiguration | None:
    for event in reversed(events):
        if event.event_type is ProviderReadinessEventType.CONFIGURED:
            return event.configuration
    return None


def _current_configuration_events(
    events: list[ProviderReadinessEvent],
) -> tuple[ProviderReadinessEvent, ...]:
    for index in range(len(events) - 1, -1, -1):
        if events[index].event_type is ProviderReadinessEventType.CONFIGURED:
            return tuple(events[index:])
    return ()


def _has_unterminated_start(
    events: list[ProviderReadinessEvent], validation_key: IdempotencyKey | None
) -> bool:
    if validation_key is None:
        return False
    matching = [
        event
        for event in _current_configuration_events(events)
        if event.validation_key == validation_key
    ]
    return bool(matching) and matching[-1].event_type is ProviderReadinessEventType.VALIDATION_STARTED


def _can_start_validation(events: list[ProviderReadinessEvent]) -> bool:
    validation_events = [
        event
        for event in _current_configuration_events(events)
        if event.event_type
        in {
            ProviderReadinessEventType.VALIDATION_STARTED,
            ProviderReadinessEventType.VALIDATION_PASSED,
            ProviderReadinessEventType.VALIDATION_FAILED,
        }
    ]
    return not validation_events or (
        validation_events[-1].event_type
        is ProviderReadinessEventType.VALIDATION_FAILED
    )


def _snapshot(
    tenant_id: TenantId,
    capabilities: tuple[ProviderCapability, ...],
    events: list[ProviderReadinessEvent],
) -> ProviderReadinessSnapshot:
    current_events = _current_configuration_events(events)
    if not current_events:
        return ProviderReadinessSnapshot(
            tenant_id=tenant_id,
            provider=ProviderId.HUNTER,
            capabilities=capabilities,
            configuration=None,
            state=ProviderReadinessState.PROVIDER_NOT_CONFIGURED,
            failure_code=None,
            events=(),
        )
    configuration = current_events[0].configuration
    validation_events = tuple(
        event
        for event in current_events[1:]
        if event.event_type
        in {
            ProviderReadinessEventType.VALIDATION_STARTED,
            ProviderReadinessEventType.VALIDATION_PASSED,
            ProviderReadinessEventType.VALIDATION_FAILED,
        }
    )
    if not validation_events:
        state = ProviderReadinessState.VALIDATION_NOT_RUN
        failure_code = None
    else:
        latest_validation = validation_events[-1]
        if latest_validation.event_type is ProviderReadinessEventType.VALIDATION_STARTED:
            state = ProviderReadinessState.VALIDATION_INCONCLUSIVE
            failure_code = None
        elif latest_validation.event_type is ProviderReadinessEventType.VALIDATION_FAILED:
            state = ProviderReadinessState.VALIDATION_FAILED
            failure_code = latest_validation.failure_code
        else:
            latest_index = current_events.index(latest_validation)
            has_matching_runtime = any(
                event.event_type is ProviderReadinessEventType.RUNTIME_COMPOSED
                for event in current_events[latest_index + 1 :]
            )
            state = (
                ProviderReadinessState.READY
                if has_matching_runtime
                else ProviderReadinessState.RUNTIME_NOT_COMPOSED
            )
            failure_code = None
    return ProviderReadinessSnapshot(
        tenant_id=tenant_id,
        provider=ProviderId.HUNTER,
        capabilities=capabilities,
        configuration=configuration,
        state=state,
        failure_code=failure_code,
        events=current_events,
    )


def _with_appended(
    events: list[ProviderReadinessEvent], event: ProviderReadinessEvent
) -> list[ProviderReadinessEvent]:
    return events if event in events else [*events, event]


__all__ = (
    "HUNTER_CONTACT_CAPABILITIES",
    "ProviderCapability",
    "ProviderConfiguration",
    "ProviderId",
    "ProviderReadinessActor",
    "ProviderReadinessEvent",
    "ProviderReadinessEventId",
    "ProviderReadinessEventType",
    "ProviderReadinessPermission",
    "ProviderReadinessRepository",
    "ProviderReadinessService",
    "ProviderReadinessServiceImpl",
    "ProviderReadinessSnapshot",
    "ProviderReadinessState",
    "ProviderReadinessUnavailableError",
    "ProviderReadinessUnitOfWork",
    "ProviderReadinessUnitOfWorkFactory",
    "ProviderRuntimeGuard",
    "ProviderValidationFailureCode",
)
