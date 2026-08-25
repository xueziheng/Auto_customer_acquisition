"""Hunter Provider 显式验证的凭证、顺序与固定失败分类合同。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from connectors.hunter.client import (
    HunterAuthRequiredError,
    HunterConnector,
    HunterRateLimitedError,
    HunterResponseInvalidError,
    HunterTransientError,
    HunterUncertainError,
)
from connectors.hunter.secrets import BoundHunterSecretResolver
from connectors.hunter.transport import (
    HunterErrorCode,
    HunterHttpResponse,
    HunterHttpStatusError,
    HunterNetworkError,
)
from shared.errors import InvalidStateTransition, TransientError, ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.provider_validation import (
    PROVIDER_VALIDATION_MANIFEST,
    ProviderValidationHandler,
    ProviderValidationRateLimitCheck,
)
from tool_gateway.manifest import CostClass, IdempotencyRequirement, RiskLevel
from tool_gateway.pipeline import ToolCallContext, ToolInvocationState
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessActor,
    ProviderReadinessEvent,
    ProviderReadinessEventId,
    ProviderReadinessEventType,
    ProviderReadinessPermission,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
    ProviderValidationFailureCode,
)

TENANT = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
USER = UserId("usr_01K2C5R6J7ABCDEFGHJKMNPQRS")
NOW = datetime(2026, 8, 25, 10, tzinfo=UTC)
CONFIGURATION = ProviderConfiguration.hunter_contacts("deploy-v1", "key-v1")
VALIDATION_KEY = IdempotencyKey("hunter-validation-1")
API_KEY = "hunter-validation-api-key-canary"
DEPLOYMENT_REF = "HUNTER_API_KEY_PROD_CANARY"
RAW_CANARY = "raw-provider-payload-canary"
STARTED_EVENT_ID = "pre_01K2C5R6J7ABCDEFGHJKMNPQRS"


class _Resolver:
    def __init__(self, value: str | BaseException = API_KEY) -> None:
        self.value = value
        self.refs: list[str] = []

    def resolve(self, secret_ref: str) -> str:
        self.refs.append(secret_ref)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value

    def __repr__(self) -> str:
        return "_Resolver()"


class _Transport:
    def __init__(self, result: HunterHttpResponse | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[tuple[str, str], ...]]] = []

    async def get(self, path, params, *, api_key):
        assert api_key == API_KEY
        self.calls.append((path, params))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Readiness:
    def __init__(
        self,
        *,
        state: ProviderReadinessState = ProviderReadinessState.VALIDATION_NOT_RUN,
        order: list[str] | None = None,
        terminal_error: BaseException | None = None,
        start_error: BaseException | None = None,
        latest_validation_key: IdempotencyKey | None = None,
    ) -> None:
        self.order = order if order is not None else []
        self.terminal_error = terminal_error
        self.start_error = start_error
        self.events: list[
            tuple[ProviderReadinessEventType, ProviderValidationFailureCode | None]
        ] = []
        snapshot_events = ()
        if latest_validation_key is not None:
            snapshot_events = (
                ProviderReadinessEvent(
                    tenant_id=TENANT,
                    event_id=ProviderReadinessEventId(
                        "pre_01K2C5R6J7ABCDEFGHJKMNPQRW"
                    ),
                    provider=ProviderId.HUNTER,
                    capabilities=HUNTER_CONTACT_CAPABILITIES,
                    sequence=3,
                    event_type=ProviderReadinessEventType.VALIDATION_FAILED,
                    configuration=CONFIGURATION,
                    validation_key=latest_validation_key,
                    failure_code=ProviderValidationFailureCode.PROVIDER_TRANSIENT,
                    evidence_ref=None,
                    actor_id="employee:hunter-validator",
                    occurred_at=NOW,
                    idempotency_key=IdempotencyKey(
                        f"validation:failed:{latest_validation_key}"
                    ),
                ),
            )
        self.snapshot = ProviderReadinessSnapshot(
            TENANT,
            ProviderId.HUNTER,
            HUNTER_CONTACT_CAPABILITIES,
            CONFIGURATION,
            state,
            (
                ProviderValidationFailureCode.PROVIDER_TRANSIENT
                if state is ProviderReadinessState.VALIDATION_FAILED
                else None
            ),
            snapshot_events,
        )

    async def get_snapshot(self, tenant_id, capabilities, *, actor):
        assert tenant_id == TENANT
        assert capabilities == HUNTER_CONTACT_CAPABILITIES
        assert actor == ACTOR
        return self.snapshot

    async def mark_validation_started(
        self, tenant_id, configuration_hash, *, validation_key, actor
    ):
        assert tenant_id == TENANT
        assert configuration_hash == CONFIGURATION.configuration_hash
        assert validation_key == VALIDATION_KEY
        assert actor == ACTOR
        if self.start_error is not None:
            raise self.start_error
        self.order.append("validation_started")
        self.events.append((ProviderReadinessEventType.VALIDATION_STARTED, None))
        return SimpleNamespace(event_id=STARTED_EVENT_ID)

    async def mark_validation_passed(
        self,
        tenant_id,
        configuration_hash,
        *,
        validation_key,
        evidence_ref,
        actor,
    ):
        assert tenant_id == TENANT
        assert configuration_hash == CONFIGURATION.configuration_hash
        assert validation_key == VALIDATION_KEY
        assert evidence_ref == f"hunter-account:{STARTED_EVENT_ID}"
        assert actor == ACTOR
        if self.terminal_error is not None:
            raise self.terminal_error
        self.order.append("validation_passed")
        self.events.append((ProviderReadinessEventType.VALIDATION_PASSED, None))
        return SimpleNamespace(event_id="pre_01K2C5R6J7ABCDEFGHJKMNPQRT")

    async def mark_validation_failed(
        self,
        tenant_id,
        configuration_hash,
        *,
        validation_key,
        failure_code,
        actor,
    ):
        assert tenant_id == TENANT
        assert configuration_hash == CONFIGURATION.configuration_hash
        assert validation_key == VALIDATION_KEY
        assert actor == ACTOR
        if self.terminal_error is not None:
            raise self.terminal_error
        self.order.append("validation_failed")
        self.events.append(
            (ProviderReadinessEventType.VALIDATION_FAILED, failure_code)
        )
        return SimpleNamespace(event_id="pre_01K2C5R6J7ABCDEFGHJKMNPQRV")


ACTOR = ProviderReadinessActor(
    "employee:hunter-validator",
    TENANT,
    frozenset(
        {
            ProviderReadinessPermission.READ,
            ProviderReadinessPermission.VALIDATE,
        }
    ),
)


def _context(
    *,
    configuration_version: str = CONFIGURATION.configuration_version,
    validation_key: IdempotencyKey | None = VALIDATION_KEY,
) -> ToolCallContext:
    return ToolCallContext(
        TENANT,
        USER,
        PROVIDER_VALIDATION_MANIFEST.tool_id,
        {"configuration_version": configuration_version},
        idempotency_key=validation_key,
    )


def _handler(
    readiness: _Readiness,
    transport: _Transport,
    *,
    resolver: _Resolver | None = None,
) -> ProviderValidationHandler:
    return ProviderValidationHandler(
        readiness,
        lambda requested_tenant: (
            HunterConnector(transport)
            if requested_tenant == TENANT
            else (_ for _ in ()).throw(AssertionError("wrong tenant"))
        ),
        resolver or _Resolver(),
        DEPLOYMENT_REF,
        lambda ctx: ACTOR,
        HmacFingerprintProvider("provider-validation-v1", b"f" * 32),
    )


async def _prepared(handler: ProviderValidationHandler):
    return await handler.prepare(_context(), None)


def test_manifest_is_exact() -> None:
    manifest = PROVIDER_VALIDATION_MANIFEST
    assert manifest.tool_id == "provider.hunter.validate"
    assert manifest.version == "v1"
    assert manifest.description == "验证当前 Hunter Provider 安全配置"
    assert manifest.risk_level is RiskLevel.MEDIUM
    assert manifest.cost_class is CostClass.LOW
    assert manifest.requires_approval is False
    assert manifest.idempotency is IdempotencyRequirement.REQUIRED
    assert manifest.required_permissions == ("provider:validate",)
    assert manifest.checks == ("tenant", "permission", "idempotency", "rate_limit")
    assert dict(manifest.output_schema["properties"]) == {
        "provider_ref": {"type": "string"},
        "configuration_version": {"type": "string"},
        "status": {"type": "string", "enum": ("validation_passed",)},
    }


@pytest.mark.asyncio
async def test_validate_account_uses_fixed_non_pii_endpoint() -> None:
    transport = _Transport(HunterHttpResponse(200, {"data": {"requests": {}}}))
    connector = HunterConnector(transport)
    await connector.configure(_Resolver())
    await connector.validate_account()
    assert transport.calls == [("/account", ())]


@pytest.mark.asyncio
async def test_validate_account_requires_configuration_and_mapping_data() -> None:
    transport = _Transport(HunterHttpResponse(200, {"data": {}}))
    connector = HunterConnector(transport)
    with pytest.raises(HunterAuthRequiredError):
        await connector.validate_account()
    assert transport.calls == []

    malformed = _Transport(
        HunterHttpResponse(200, {"data": RAW_CANARY, "nested": RAW_CANARY})
    )
    connector = HunterConnector(malformed)
    await connector.configure(_Resolver())
    with pytest.raises(HunterResponseInvalidError) as captured:
        await connector.validate_account()
    assert RAW_CANARY not in str(captured.value)
    assert RAW_CANARY not in repr(captured.value)


@pytest.mark.parametrize(
    ("provider_error", "expected_type", "retry_after"),
    [
        (
            HunterHttpStatusError(401, HunterErrorCode.OTHER),
            HunterAuthRequiredError,
            None,
        ),
        (
            HunterHttpStatusError(403, HunterErrorCode.OTHER, 11),
            HunterRateLimitedError,
            11,
        ),
        (
            HunterHttpStatusError(429, HunterErrorCode.OTHER, 3600),
            HunterRateLimitedError,
            3600,
        ),
        (
            HunterHttpStatusError(500, HunterErrorCode.OTHER),
            HunterTransientError,
            None,
        ),
        (HunterNetworkError(False), HunterTransientError, None),
        (HunterNetworkError(True), HunterUncertainError, None),
    ],
)
@pytest.mark.asyncio
async def test_validate_account_classifies_transport_failures(
    provider_error: BaseException,
    expected_type: type[BaseException],
    retry_after: int | None,
) -> None:
    connector = HunterConnector(_Transport(provider_error))
    await connector.configure(_Resolver())
    with pytest.raises(expected_type) as captured:
        await connector.validate_account()
    assert getattr(captured.value, "retry_after_seconds", None) == retry_after
    assert API_KEY not in repr(captured.value)


def test_bound_resolver_maps_only_fixed_logical_reference_without_disclosure() -> None:
    resolver = _Resolver()
    bound = BoundHunterSecretResolver(resolver, DEPLOYMENT_REF)
    assert bound.resolve("HUNTER_API_KEY_REF") == API_KEY
    assert resolver.refs == [DEPLOYMENT_REF]
    assert DEPLOYMENT_REF not in repr(bound)
    assert API_KEY not in repr(bound)
    with pytest.raises(ValidationError) as captured:
        bound.resolve("HUNTER_OTHER_REF_CANARY")
    combined = f"{captured.value!s} {captured.value!r}"
    assert DEPLOYMENT_REF not in combined
    assert "HUNTER_OTHER_REF_CANARY" not in combined


@pytest.mark.asyncio
async def test_prepare_requires_current_matching_configuration_and_gateway_key() -> None:
    readiness = _Readiness()
    handler = _handler(
        readiness,
        _Transport(HunterHttpResponse(200, {"data": {}})),
    )
    prepared = await _prepared(handler)
    assert VALIDATION_KEY not in repr(prepared)
    assert API_KEY not in repr(prepared)
    assert dict(prepared.audit_projection) == {
        "tenant_id": TENANT,
        "provider": "hunter",
        "configuration_version": "deploy-v1",
        "configuration_hash": CONFIGURATION.configuration_hash,
    }
    expected, version = HmacFingerprintProvider(
        "provider-validation-v1", b"f" * 32
    ).fingerprint(
        (
            str(TENANT).encode(),
            b"hunter",
            CONFIGURATION.configuration_hash.encode(),
            b"deploy-v1",
        )
    )
    assert (prepared.request_fingerprint, prepared.fingerprint_version) == (
        expected,
        version,
    )

    for context in (
        _context(configuration_version="deploy-v2"),
        _context(validation_key=None),
        ToolCallContext(
            TENANT,
            USER,
            PROVIDER_VALIDATION_MANIFEST.tool_id,
            {"configuration_version": "deploy-v1", "extra": "x"},
            idempotency_key=VALIDATION_KEY,
        ),
    ):
        with pytest.raises(ValidationError):
            await handler.prepare(context, None)


@pytest.mark.asyncio
async def test_handler_commits_started_before_provider_io() -> None:
    order: list[str] = []
    readiness = _Readiness(order=order)

    class _OrderedTransport(_Transport):
        async def get(self, path, params, *, api_key):
            order.append("provider")
            return await super().get(path, params, api_key=api_key)

    handler = _handler(
        readiness,
        _OrderedTransport(HunterHttpResponse(200, {"data": {}})),
    )
    output = await handler.execute(TENANT, await _prepared(handler))
    assert order == ["validation_started", "provider", "validation_passed"]
    assert dict(output) == {
        "provider_ref": f"hunter-account:{STARTED_EVENT_ID}",
        "configuration_version": "deploy-v1",
        "status": "validation_passed",
    }


@pytest.mark.parametrize(
    ("error", "gateway_category", "failure_code"),
    [
        (
            HunterHttpStatusError(401, HunterErrorCode.OTHER),
            ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
            ProviderValidationFailureCode.AUTH_REQUIRED,
        ),
        (
            HunterHttpStatusError(403, HunterErrorCode.OTHER, 9),
            ToolErrorCategory.RATE_LIMITED,
            ProviderValidationFailureCode.RATE_LIMITED,
        ),
        (
            HunterHttpStatusError(429, HunterErrorCode.OTHER, 3600),
            ToolErrorCategory.RATE_LIMITED,
            ProviderValidationFailureCode.RATE_LIMITED,
        ),
        (
            HunterHttpStatusError(503, HunterErrorCode.OTHER),
            ToolErrorCategory.PROVIDER_TRANSIENT,
            ProviderValidationFailureCode.PROVIDER_TRANSIENT,
        ),
        (
            HunterHttpResponse(200, {"data": RAW_CANARY}),
            ToolErrorCategory.PROVIDER_PERMANENT,
            ProviderValidationFailureCode.RESPONSE_INVALID,
        ),
        (
            HunterNetworkError(False),
            ToolErrorCategory.PROVIDER_TRANSIENT,
            ProviderValidationFailureCode.PROVIDER_TRANSIENT,
        ),
    ],
)
@pytest.mark.asyncio
async def test_definite_failure_is_persisted_then_mapped(
    error: HunterHttpResponse | BaseException,
    gateway_category: ToolErrorCategory,
    failure_code: ProviderValidationFailureCode,
) -> None:
    readiness = _Readiness()
    handler = _handler(readiness, _Transport(error))
    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, await _prepared(handler))
    assert captured.value.category is gateway_category
    assert readiness.events == [
        (ProviderReadinessEventType.VALIDATION_STARTED, None),
        (ProviderReadinessEventType.VALIDATION_FAILED, failure_code),
    ]
    assert RAW_CANARY not in repr(readiness.events)


@pytest.mark.asyncio
async def test_resolver_failure_is_persisted_as_auth_without_disclosure(caplog) -> None:
    readiness = _Readiness()
    nested = RuntimeError(f"nested {API_KEY} {DEPLOYMENT_REF}")
    handler = _handler(
        readiness,
        _Transport(HunterHttpResponse(200, {"data": {}})),
        resolver=_Resolver(nested),
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, await _prepared(handler))
    assert captured.value.category is ToolErrorCategory.PROVIDER_AUTH_REQUIRED
    assert readiness.events[-1] == (
        ProviderReadinessEventType.VALIDATION_FAILED,
        ProviderValidationFailureCode.AUTH_REQUIRED,
    )
    exposed = repr((captured.value, readiness.events, caplog.records))
    assert API_KEY not in exposed
    assert DEPLOYMENT_REF not in exposed


@pytest.mark.asyncio
async def test_uncertain_transport_leaves_only_started() -> None:
    readiness = _Readiness()
    handler = _handler(readiness, _Transport(HunterNetworkError(True)))
    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, await _prepared(handler))
    assert captured.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert readiness.events == [
        (ProviderReadinessEventType.VALIDATION_STARTED, None)
    ]


@pytest.mark.asyncio
async def test_terminal_readiness_commit_failure_is_inconclusive() -> None:
    readiness = _Readiness(terminal_error=TransientError(f"storage {RAW_CANARY}"))
    handler = _handler(
        readiness,
        _Transport(HunterHttpResponse(200, {"data": {"requests": {}}})),
    )
    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, await _prepared(handler))
    assert captured.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert readiness.events == [
        (ProviderReadinessEventType.VALIDATION_STARTED, None)
    ]
    assert RAW_CANARY not in repr(captured.value)


@pytest.mark.parametrize(
    ("start_error", "category"),
    [
        (
            InvalidStateTransition("concurrent validation marker"),
            ToolErrorCategory.RATE_LIMITED,
        ),
        (
            TransientError("storage uncertainty marker"),
            ToolErrorCategory.RECONCILIATION_REQUIRED,
        ),
    ],
)
@pytest.mark.asyncio
async def test_start_failure_is_mapped_before_secret_resolution_and_provider_io(
    start_error: BaseException,
    category: ToolErrorCategory,
) -> None:
    readiness = _Readiness(start_error=start_error)
    resolver = _Resolver()
    transport = _Transport(HunterHttpResponse(200, {"data": {}}))
    handler = _handler(readiness, transport, resolver=resolver)

    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, await _prepared(handler))

    assert captured.value.category is category
    assert transport.calls == []
    assert resolver.refs == []
    assert readiness.events == []


@pytest.mark.parametrize(
    ("state", "allowed"),
    [
        (ProviderReadinessState.VALIDATION_NOT_RUN, True),
        (ProviderReadinessState.VALIDATION_FAILED, True),
        (ProviderReadinessState.VALIDATION_INCONCLUSIVE, False),
        (ProviderReadinessState.RUNTIME_NOT_COMPOSED, False),
        (ProviderReadinessState.READY, False),
    ],
)
@pytest.mark.asyncio
async def test_nonnumeric_rate_limit_allows_only_manual_validation_states(
    state: ProviderReadinessState, allowed: bool
) -> None:
    latest = (
        IdempotencyKey("older-validation")
        if state is ProviderReadinessState.VALIDATION_FAILED
        else None
    )
    readiness = _Readiness(state=state, latest_validation_key=latest)
    handler = _handler(
        readiness, _Transport(HunterHttpResponse(200, {"data": {}}))
    )
    prepared = await _prepared(handler)
    check = ProviderValidationRateLimitCheck(readiness, lambda ctx: ACTOR)
    invocation = ToolInvocationState(
        PROVIDER_VALIDATION_MANIFEST,
        "tcl_01K2C5R6J7ABCDEFGHJKMNPQRS",
        prepared,
    )
    if allowed:
        rejection = await check.check(_context(), invocation)
        assert rejection is None
    else:
        with pytest.raises(ToolGatewayError) as captured:
            await check.check(_context(), invocation)
        assert captured.value.category is ToolErrorCategory.RATE_LIMITED


@pytest.mark.asyncio
async def test_failed_validation_requires_a_new_gateway_idempotency_key() -> None:
    readiness = _Readiness(
        state=ProviderReadinessState.VALIDATION_FAILED,
        latest_validation_key=VALIDATION_KEY,
    )
    handler = _handler(
        readiness, _Transport(HunterHttpResponse(200, {"data": {}}))
    )
    check = ProviderValidationRateLimitCheck(readiness, lambda ctx: ACTOR)
    invocation = ToolInvocationState(
        PROVIDER_VALIDATION_MANIFEST,
        "tcl_01K2C5R6J7ABCDEFGHJKMNPQRS",
        await _prepared(handler),
    )
    with pytest.raises(ToolGatewayError) as captured:
        await check.check(_context(), invocation)
    assert captured.value.category is ToolErrorCategory.RATE_LIMITED
