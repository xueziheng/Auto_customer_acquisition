"""Hunter Provider 当前安全配置的显式 Gateway 验证插件。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from connectors.hunter.client import (
    HunterAuthRequiredError,
    HunterConnector,
    HunterConnectorError,
    HunterPermanentError,
    HunterRateLimitedError,
    HunterResponseInvalidError,
    HunterSecretResolver,
    HunterTransientError,
    HunterUncertainError,
)
from connectors.hunter.secrets import BoundHunterSecretResolver
from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    SafeScalar,
    ToolCallContext,
    ToolInvocationState,
)
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessActor,
    ProviderReadinessEventType,
    ProviderReadinessService,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
    ProviderValidationFailureCode,
)

PROVIDER_VALIDATION_MANIFEST = ToolManifest(
    tool_id="provider.hunter.validate",
    version="v1",
    description="验证当前 Hunter Provider 安全配置",
    risk_level=RiskLevel.MEDIUM,
    cost_class=CostClass.LOW,
    requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("provider:validate",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={
        "type": "object",
        "properties": {
            "configuration_version": {
                "type": "string",
                "pattern": "^[a-z0-9][a-z0-9._-]{0,31}$",
            },
        },
        "required": ("configuration_version",),
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {
            "provider_ref": {"type": "string"},
            "configuration_version": {"type": "string"},
            "status": {"type": "string", "enum": ("validation_passed",)},
        },
        "required": ("provider_ref", "configuration_version", "status"),
        "additionalProperties": False,
    },
    redact_fields=(),
)


@dataclass(frozen=True, repr=False)
class _ProviderValidationPayload:
    tenant_id: TenantId
    configuration: ProviderConfiguration
    validation_key: IdempotencyKey = field(repr=False)
    actor: ProviderReadinessActor = field(repr=False)


class ProviderValidationHandler:
    """在 Gateway 已提交 EXECUTING 后记录验证开始，再访问固定 `/account`。"""

    def __init__(
        self,
        readiness: ProviderReadinessService,
        connector_factory: Callable[[TenantId], HunterConnector],
        secret_resolver: HunterSecretResolver,
        configured_secret_ref: str,
        actor_provider: Callable[[ToolCallContext], ProviderReadinessActor],
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        required = (
            "get_snapshot",
            "mark_validation_started",
            "mark_validation_passed",
            "mark_validation_failed",
        )
        if (
            not all(callable(getattr(readiness, name, None)) for name in required)
            or not callable(connector_factory)
            or not isinstance(secret_resolver, HunterSecretResolver)
            or not callable(actor_provider)
            or not isinstance(fingerprints, HmacFingerprintProvider)
        ):
            raise ValidationError("Provider validation handler 依赖无效")
        self._readiness = readiness
        self._connector_factory = connector_factory
        self._secrets = BoundHunterSecretResolver(
            secret_resolver, configured_secret_ref
        )
        self._actor_provider = actor_provider
        self._fingerprints = fingerprints

    async def prepare(
        self,
        ctx: ToolCallContext,
        preflight: object | None,
    ) -> PreparedToolCall:
        if preflight is not None or set(ctx.params) != {"configuration_version"}:
            raise ValidationError("Provider validation params 无效")
        requested_version = ctx.params.get("configuration_version")
        if not isinstance(requested_version, str) or ctx.idempotency_key is None:
            raise ValidationError("Provider validation params 无效")
        actor = self._actor_provider(ctx)
        if not isinstance(actor, ProviderReadinessActor):
            raise ValidationError("Provider validation actor 无效")
        snapshot = await self._readiness.get_snapshot(
            ctx.tenant_id,
            HUNTER_CONTACT_CAPABILITIES,
            actor=actor,
        )
        configuration = _current_matching_configuration(
            snapshot,
            ctx.tenant_id,
            requested_version,
        )
        fingerprint, fingerprint_version = self._fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                configuration.provider.value.encode(),
                configuration.configuration_hash.encode(),
                configuration.configuration_version.encode(),
            )
        )
        return PreparedToolCall(
            fingerprint,
            fingerprint_version,
            {
                "tenant_id": str(ctx.tenant_id),
                "provider": configuration.provider.value,
                "configuration_version": configuration.configuration_version,
                "configuration_hash": configuration.configuration_hash,
            },
            _ProviderValidationPayload(
                ctx.tenant_id,
                configuration,
                ctx.idempotency_key,
                actor,
            ),
        )

    async def execute(
        self,
        tenant_id: TenantId,
        prepared: PreparedToolCall,
    ) -> Mapping[str, SafeScalar]:
        payload = _require_payload(tenant_id, prepared)
        try:
            started = await self._readiness.mark_validation_started(
                tenant_id,
                payload.configuration.configuration_hash,
                validation_key=payload.validation_key,
                actor=payload.actor,
            )
        except InvalidStateTransition:
            raise ToolGatewayError(ToolErrorCategory.RATE_LIMITED) from None
        except Exception:  # noqa: BLE001 - start 存储结果不确定时禁止 Provider IO。
            raise ToolGatewayError(
                ToolErrorCategory.RECONCILIATION_REQUIRED
            ) from None
        event_id = getattr(started, "event_id", None)
        if not isinstance(event_id, str):
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        evidence_ref = f"hunter-account:{event_id}"

        try:
            connector = self._connector_factory(tenant_id)
            if not isinstance(connector, HunterConnector):
                raise ValidationError("Provider validation connector 无效")
            await connector.configure(self._secrets)
            await connector.validate_account()
        except HunterUncertainError:
            raise ToolGatewayError(
                ToolErrorCategory.RECONCILIATION_REQUIRED
            ) from None
        except HunterConnectorError as error:
            failure_code, gateway_error = _map_validation_failure(error)
            try:
                await self._readiness.mark_validation_failed(
                    tenant_id,
                    payload.configuration.configuration_hash,
                    validation_key=payload.validation_key,
                    failure_code=failure_code,
                    actor=payload.actor,
                )
            except Exception:  # noqa: BLE001 - 已开始事实后终态不确定必须转人工。
                raise ToolGatewayError(
                    ToolErrorCategory.RECONCILIATION_REQUIRED
                ) from None
            raise gateway_error from None
        except Exception:  # noqa: BLE001 - started 后的未知本地结果不可自动重试。
            raise ToolGatewayError(
                ToolErrorCategory.RECONCILIATION_REQUIRED
            ) from None

        try:
            await self._readiness.mark_validation_passed(
                tenant_id,
                payload.configuration.configuration_hash,
                validation_key=payload.validation_key,
                evidence_ref=evidence_ref,
                actor=payload.actor,
            )
        except Exception:  # noqa: BLE001 - Provider 已成功但终态未确认。
            raise ToolGatewayError(
                ToolErrorCategory.RECONCILIATION_REQUIRED
            ) from None
        return {
            "provider_ref": evidence_ref,
            "configuration_version": payload.configuration.configuration_version,
            "status": "validation_passed",
        }


class ProviderValidationRateLimitCheck:
    """用 readiness 状态而非数值阈值限制当前配置的人工验证。"""

    name = "rate_limit"

    def __init__(
        self,
        readiness: ProviderReadinessService,
        actor_provider: Callable[[ToolCallContext], ProviderReadinessActor],
    ) -> None:
        if not callable(getattr(readiness, "get_snapshot", None)) or not callable(
            actor_provider
        ):
            raise ValidationError("Provider validation rate-limit 依赖无效")
        self._readiness = readiness
        self._actor_provider = actor_provider

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        try:
            payload = _require_payload(ctx.tenant_id, state.prepared)
            actor = self._actor_provider(ctx)
            if actor != payload.actor:
                raise ValidationError("Provider validation actor 绑定无效")
            snapshot = await self._readiness.get_snapshot(
                ctx.tenant_id,
                HUNTER_CONTACT_CAPABILITIES,
                actor=actor,
            )
            _current_matching_configuration(
                snapshot,
                ctx.tenant_id,
                payload.configuration.configuration_version,
                expected_hash=payload.configuration.configuration_hash,
            )
        except ToolGatewayError:
            raise
        except Exception:  # noqa: BLE001 - check reader/storage 必须固定关闭。
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT) from None

        if snapshot.state is ProviderReadinessState.VALIDATION_NOT_RUN:
            return None
        if snapshot.state is ProviderReadinessState.VALIDATION_FAILED:
            if _latest_validation_key(snapshot) != payload.validation_key:
                return None
            raise ToolGatewayError(ToolErrorCategory.RATE_LIMITED)
        raise ToolGatewayError(ToolErrorCategory.RATE_LIMITED)


def _require_payload(
    tenant_id: TenantId,
    prepared: PreparedToolCall | None,
) -> _ProviderValidationPayload:
    payload = None if prepared is None else prepared.payload
    if (
        not isinstance(payload, _ProviderValidationPayload)
        or payload.tenant_id != tenant_id
    ):
        raise ValidationError("Provider validation payload 无效")
    return payload


def _current_matching_configuration(
    snapshot: object,
    tenant_id: TenantId,
    requested_version: str,
    *,
    expected_hash: str | None = None,
) -> ProviderConfiguration:
    if (
        not isinstance(snapshot, ProviderReadinessSnapshot)
        or snapshot.tenant_id != tenant_id
        or snapshot.provider is not ProviderId.HUNTER
        or snapshot.capabilities != HUNTER_CONTACT_CAPABILITIES
        or not isinstance(snapshot.configuration, ProviderConfiguration)
        or snapshot.configuration.configuration_version != requested_version
        or (
            expected_hash is not None
            and snapshot.configuration.configuration_hash != expected_hash
        )
    ):
        raise ValidationError("Provider 当前配置不匹配")
    return snapshot.configuration


def _latest_validation_key(
    snapshot: ProviderReadinessSnapshot,
) -> IdempotencyKey | None:
    for event in reversed(snapshot.events):
        if event.event_type in {
            ProviderReadinessEventType.VALIDATION_PASSED,
            ProviderReadinessEventType.VALIDATION_FAILED,
        }:
            return event.validation_key
    return None


def _map_validation_failure(
    error: HunterConnectorError,
) -> tuple[ProviderValidationFailureCode, ToolGatewayError]:
    if isinstance(error, HunterAuthRequiredError):
        return (
            ProviderValidationFailureCode.AUTH_REQUIRED,
            ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED),
        )
    if isinstance(error, HunterRateLimitedError):
        return (
            ProviderValidationFailureCode.RATE_LIMITED,
            ToolGatewayError(
                ToolErrorCategory.RATE_LIMITED,
                retry_after_seconds=error.retry_after_seconds,
            ),
        )
    if isinstance(error, HunterResponseInvalidError):
        return (
            ProviderValidationFailureCode.RESPONSE_INVALID,
            ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT),
        )
    if isinstance(error, HunterTransientError):
        return (
            ProviderValidationFailureCode.PROVIDER_TRANSIENT,
            ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT),
        )
    if isinstance(error, HunterPermanentError):
        return (
            ProviderValidationFailureCode.PROVIDER_PERMANENT,
            ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT),
        )
    return (
        ProviderValidationFailureCode.PROVIDER_PERMANENT,
        ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT),
    )


__all__ = (
    "PROVIDER_VALIDATION_MANIFEST",
    "ProviderValidationHandler",
    "ProviderValidationRateLimitCheck",
)
