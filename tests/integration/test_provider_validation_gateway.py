"""Hunter `/account` 验证经 PostgreSQL Gateway 的顺序与脱敏验收。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.hunter.client import HunterConnector
from connectors.hunter.transport import HunterHttpResponse, HunterNetworkError
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.db.tables import (
    ProviderReadinessEventRow,
    ToolCallEventRow,
    ToolCallRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from scripts.configure_hunter_provider import main as configure_main
from scripts.validate_hunter_provider import main as validate_main
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.provider_validation import (
    PROVIDER_VALIDATION_MANIFEST,
    ProviderValidationHandler,
    ProviderValidationRateLimitCheck,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolGateway
from tool_gateway.provider_readiness import (
    ProviderConfiguration,
    ProviderReadinessActor,
    ProviderReadinessEventType,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
)

NOW = datetime(2026, 8, 25, 11, tzinfo=UTC)
API_KEY_CANARY = "hunter-gateway-api-key-canary"
RAW_CANARY = "hunter-account-raw-response-canary"
NESTED_CANARY = "hunter-nested-exception-canary"
DEPLOYMENT_REF = "HUNTER_API_KEY_GATEWAY_CANARY"
FINGERPRINT_REF = "TOOL_FINGERPRINT_GATEWAY_CANARY"


class _Resolver:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == DEPLOYMENT_REF
        return API_KEY_CANARY


class _TenantCheck:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    async def check(self, ctx, state):
        del state
        if ctx.tenant_id != self._tenant_id:
            return CheckRejection(
                self.name,
                "provider_validation:tenant",
                "Provider 验证租户绑定无效",
            )
        return None


class _BarrierRateCheck:
    name = "rate_limit"

    def __init__(self, delegate, ready: asyncio.Queue[None], release: asyncio.Event):
        self._delegate = delegate
        self._ready = ready
        self._release = release

    async def check(self, ctx, state):
        result = await self._delegate.check(ctx, state)
        self._ready.put_nowait(None)
        await self._release.wait()
        return result


class _AssertingTransport:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tenant_id: TenantId,
        result: HunterHttpResponse | BaseException,
    ) -> None:
        self._factory = factory
        self._tenant_id = tenant_id
        self._result = result
        self.calls = 0

    async def get(self, path, params, *, api_key):
        assert path == "/account"
        assert params == ()
        assert api_key == API_KEY_CANARY
        async with self._factory() as session:
            tool_statuses = list(
                (
                    await session.execute(
                        select(ToolCallRow.status).where(
                            ToolCallRow.tenant_id == self._tenant_id,
                            ToolCallRow.tool_id
                            == PROVIDER_VALIDATION_MANIFEST.tool_id,
                        )
                    )
                ).scalars()
            )
            readiness_types = list(
                (
                    await session.execute(
                        select(ProviderReadinessEventRow.event_type).where(
                            ProviderReadinessEventRow.tenant_id == self._tenant_id
                        )
                    )
                ).scalars()
            )
        assert ToolCallStatus.EXECUTING.value in tool_statuses
        assert ProviderReadinessEventType.VALIDATION_STARTED.value in readiness_types
        self.calls += 1
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _actor(tenant_id: TenantId) -> ProviderReadinessActor:
    return ProviderReadinessActor(
        "employee:hunter-validator",
        tenant_id,
        frozenset(
            {
                ProviderReadinessPermission.CONFIGURE,
                ProviderReadinessPermission.READ,
                ProviderReadinessPermission.VALIDATE,
            }
        ),
    )


async def _composition(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: TenantId,
    user_id: UserId,
    transport: _AssertingTransport,
    *,
    validation_key: str = "hunter-gateway-validation-1",
    configuration_version: str = "deploy-v1",
    rate_barrier: tuple[asyncio.Queue[None], asyncio.Event] | None = None,
) -> tuple[ToolGateway, ToolCallContext]:
    actor = _actor(tenant_id)
    readiness = ProviderReadinessServiceImpl(
        lambda requested: SqlAlchemyProviderReadinessUnitOfWork(
            factory, requested, now=lambda: NOW
        ),
        runtime_actor=actor,
        now=lambda: NOW,
    )
    configuration = ProviderConfiguration.hunter_contacts(
        configuration_version, "key-v1"
    )
    await readiness.declare_configuration(
        tenant_id,
        configuration,
        actor=actor,
        idempotency_key=IdempotencyKey("hunter-configure-gateway"),
    )
    actor_provider = lambda ctx: actor
    handler = ProviderValidationHandler(
        readiness,
        lambda requested: HunterConnector(transport)
        if requested == tenant_id
        else (_ for _ in ()).throw(AssertionError("wrong tenant")),
        _Resolver(),
        DEPLOYMENT_REF,
        actor_provider,
        HmacFingerprintProvider("provider-validation-v1", b"f" * 32),
    )
    registry = ToolRegistry()
    registry.register(PROVIDER_VALIDATION_MANIFEST, handler)

    async def authorize(ctx, state):
        del state
        return (
            ctx.tenant_id == tenant_id
            and ctx.user_id == user_id
            and ctx.tool_id == PROVIDER_VALIDATION_MANIFEST.tool_id
        )

    rate_limit = ProviderValidationRateLimitCheck(readiness, actor_provider)
    rate_stage = (
        rate_limit
        if rate_barrier is None
        else _BarrierRateCheck(rate_limit, rate_barrier[0], rate_barrier[1])
    )
    gateway = ToolGateway(
        registry,
        {
            "tenant": _TenantCheck(tenant_id),
            "permission": PermissionCheck(authorize),
            "idempotency": IdempotencyCheck(),
            "rate_limit": rate_stage,
        },
        lambda requested: SqlAlchemyToolGatewayUnitOfWork(
            factory, requested, now=lambda: NOW
        ),
        lease_duration=timedelta(minutes=1),
        lease_owner="provider_validation",
        now=lambda: NOW,
        id_factory=new_id,
    )
    context = ToolCallContext(
        tenant_id,
        user_id,
        PROVIDER_VALIDATION_MANIFEST.tool_id,
        {"configuration_version": configuration_version},
        idempotency_key=IdempotencyKey(validation_key),
    )
    return gateway, context


@pytest.mark.parametrize("configuration_version", ["url", "dsn", "cookie"])
@pytest.mark.asyncio
async def test_canonical_marker_shaped_versions_complete_gateway_and_readiness(
    integration_engine,
    configuration_version: str,
) -> None:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    user_id = UserId(new_id("usr"))
    transport = _AssertingTransport(
        factory,
        tenant_id,
        HunterHttpResponse(200, {"data": {"requests": {}}}),
    )
    gateway, context = await _composition(
        factory,
        tenant_id,
        user_id,
        transport,
        validation_key=f"hunter-version-{configuration_version}",
        configuration_version=configuration_version,
    )

    result = await gateway.invoke(context)

    assert result.status is ToolCallStatus.SUCCEEDED
    assert dict(result.output or {}) == {
        "provider_ref": next(
            value
            for key, value in dict(result.output or {}).items()
            if key == "provider_ref"
        ),
        "configuration_version": configuration_version,
        "status": "validation_passed",
    }
    assert transport.calls == 1
    async with factory() as session:
        tool_statuses = list(
            (
                await session.execute(
                    select(ToolCallRow.status).where(
                        ToolCallRow.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
        readiness_types = list(
            (
                await session.execute(
                    select(ProviderReadinessEventRow.event_type)
                    .where(ProviderReadinessEventRow.tenant_id == tenant_id)
                    .order_by(ProviderReadinessEventRow.sequence)
                )
            ).scalars()
        )
    assert tool_statuses == [ToolCallStatus.SUCCEEDED.value]
    assert readiness_types == [
        ProviderReadinessEventType.CONFIGURED.value,
        ProviderReadinessEventType.VALIDATION_STARTED.value,
        ProviderReadinessEventType.VALIDATION_PASSED.value,
    ]


@pytest.mark.asyncio
async def test_gateway_commits_executing_then_started_and_persists_no_secrets(
    integration_engine, caplog
) -> None:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    user_id = UserId(new_id("usr"))
    transport = _AssertingTransport(
        factory,
        tenant_id,
        HunterHttpResponse(
            200,
            {
                "data": {"requests": {}},
                "raw": RAW_CANARY,
                "nested": {"private": RAW_CANARY},
            },
        ),
    )
    gateway, context = await _composition(
        factory, tenant_id, user_id, transport
    )

    first = await gateway.invoke(context)
    duplicate = await gateway.invoke(context)

    assert first.status is ToolCallStatus.SUCCEEDED
    assert dict(first.output or {}) == {
        "provider_ref": next(
            value
            for key, value in dict(first.output or {}).items()
            if key == "provider_ref"
        ),
        "configuration_version": "deploy-v1",
        "status": "validation_passed",
    }
    assert duplicate.status is ToolCallStatus.DUPLICATE
    assert duplicate.tool_call_id == first.tool_call_id
    assert transport.calls == 1

    async with factory() as session:
        tool_rows = list(
            (
                await session.execute(
                    select(ToolCallRow).where(ToolCallRow.tenant_id == tenant_id)
                )
            ).scalars()
        )
        tool_events = list(
            (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
        readiness_events = list(
            (
                await session.execute(
                    select(ProviderReadinessEventRow).where(
                        ProviderReadinessEventRow.tenant_id == tenant_id
                    ).order_by(ProviderReadinessEventRow.sequence)
                )
            ).scalars()
        )

    assert [row.event_type for row in readiness_events] == [
        ProviderReadinessEventType.CONFIGURED.value,
        ProviderReadinessEventType.VALIDATION_STARTED.value,
        ProviderReadinessEventType.VALIDATION_PASSED.value,
    ]
    canonical = next(row for row in tool_rows if row.provider_ref is not None)
    assert canonical.provider_ref.startswith("hunter-account:pre_")
    persisted = repr(
        (
            [
                {
                    column.name: getattr(row, column.name)
                    for column in ToolCallRow.__table__.columns
                }
                for row in tool_rows
            ],
            [
                {
                    column.name: getattr(row, column.name)
                    for column in ToolCallEventRow.__table__.columns
                }
                for row in tool_events
            ],
            [
                {
                    column.name: getattr(row, column.name)
                    for column in ProviderReadinessEventRow.__table__.columns
                }
                for row in readiness_events
            ],
            caplog.records,
        )
    )
    for forbidden in (
        API_KEY_CANARY,
        DEPLOYMENT_REF,
        RAW_CANARY,
        NESTED_CANARY,
        "requests",
        "https://api.hunter.io",
        "X-API-KEY",
    ):
        assert forbidden not in persisted


@pytest.mark.asyncio
async def test_uncertain_provider_result_stays_started_and_is_not_reissued(
    integration_engine,
) -> None:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    user_id = UserId(new_id("usr"))
    transport = _AssertingTransport(
        factory, tenant_id, HunterNetworkError(True)
    )
    gateway, context = await _composition(
        factory,
        tenant_id,
        user_id,
        transport,
        validation_key="hunter-gateway-uncertain-1",
    )

    first = await gateway.invoke(context)
    second = await gateway.invoke(context)

    assert first.status is ToolCallStatus.FAILED_TRANSIENT
    assert first.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert second.status is ToolCallStatus.FAILED_TRANSIENT
    assert second.error_category in {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RECONCILIATION_REQUIRED,
    }
    assert transport.calls == 1
    async with factory() as session:
        readiness_types = list(
            (
                await session.execute(
                    select(ProviderReadinessEventRow.event_type).where(
                        ProviderReadinessEventRow.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
    assert readiness_types == [
        ProviderReadinessEventType.CONFIGURED.value,
        ProviderReadinessEventType.VALIDATION_STARTED.value,
    ]


@pytest.mark.asyncio
async def test_post_claim_readiness_denial_is_fixed_rate_limited_with_zero_provider_io(
    integration_engine,
) -> None:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    user_id = UserId(new_id("usr"))
    transport = _AssertingTransport(
        factory,
        tenant_id,
        HunterHttpResponse(200, {"data": {"requests": {}}}),
    )
    gateway, first_context = await _composition(
        factory, tenant_id, user_id, transport
    )
    first = await gateway.invoke(first_context)
    assert first.status is ToolCallStatus.SUCCEEDED
    assert transport.calls == 1

    blocked = await gateway.invoke(
        ToolCallContext(
            tenant_id,
            user_id,
            PROVIDER_VALIDATION_MANIFEST.tool_id,
            {"configuration_version": "deploy-v1"},
            idempotency_key=IdempotencyKey("hunter-gateway-validation-2"),
        )
    )

    assert blocked.status is ToolCallStatus.FAILED_TRANSIENT
    assert blocked.error_category is ToolErrorCategory.RATE_LIMITED
    assert blocked.output is None
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_concurrent_different_keys_atomically_allow_one_provider_call(
    integration_engine,
) -> None:
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant_id = TenantId(new_id("tn"))
    user_id = UserId(new_id("usr"))
    transport = _AssertingTransport(
        factory,
        tenant_id,
        HunterHttpResponse(200, {"data": {"requests": {}}}),
    )
    ready: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()
    gateway, first_context = await _composition(
        factory,
        tenant_id,
        user_id,
        transport,
        rate_barrier=(ready, release),
    )
    second_context = ToolCallContext(
        tenant_id,
        user_id,
        PROVIDER_VALIDATION_MANIFEST.tool_id,
        {"configuration_version": "deploy-v1"},
        idempotency_key=IdempotencyKey("hunter-gateway-concurrent-2"),
    )
    tasks = [
        asyncio.create_task(gateway.invoke(first_context)),
        asyncio.create_task(gateway.invoke(second_context)),
    ]
    await ready.get()
    await ready.get()
    release.set()
    outcomes = await asyncio.gather(*tasks)

    succeeded = [
        result for result in outcomes if result.status is ToolCallStatus.SUCCEEDED
    ]
    denied = [
        result
        for result in outcomes
        if result.error_category is ToolErrorCategory.RATE_LIMITED
    ]
    assert len(succeeded) == 1
    assert len(denied) == 1
    assert denied[0].status is ToolCallStatus.FAILED_TRANSIENT
    assert transport.calls == 1

    denied_context = (
        first_context
        if denied[0].tool_call_id == outcomes[0].tool_call_id
        else second_context
    )
    replay = await gateway.invoke(denied_context)
    assert replay.status is ToolCallStatus.FAILED_TRANSIENT
    assert replay.error_category in {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RATE_LIMITED,
    }
    assert transport.calls == 1

    async with factory() as session:
        readiness_types = list(
            (
                await session.execute(
                    select(ProviderReadinessEventRow.event_type)
                    .where(ProviderReadinessEventRow.tenant_id == tenant_id)
                    .order_by(ProviderReadinessEventRow.sequence)
                )
            ).scalars()
        )
    assert readiness_types == [
        ProviderReadinessEventType.CONFIGURED.value,
        ProviderReadinessEventType.VALIDATION_STARTED.value,
        ProviderReadinessEventType.VALIDATION_PASSED.value,
    ]

def _cli_environ(database_url: str, tenant_id: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "TRADEOS_TENANT_ID": tenant_id,
        "TRADEOS_HUNTER_CONTACTS_ENABLED": "true",
        "TRADEOS_HUNTER_CONFIGURATION_VERSION": "deploy-v1",
        "TRADEOS_HUNTER_API_KEY_SECRET_REF": DEPLOYMENT_REF,
        "TRADEOS_HUNTER_API_KEY_VERSION": "key-v1",
        DEPLOYMENT_REF: API_KEY_CANARY,
        "TOOL_CALL_FINGERPRINT_KEY_REF": FINGERPRINT_REF,
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "provider-validation-v1",
        FINGERPRINT_REF: "f" * 32,
        "TRADEOS_TOOL_LEASE_SECONDS": "60",
    }


def test_cli_normalizes_success_and_duplicate_without_real_transport(
    db_url: str, capsys
) -> None:
    tenant_id = "tn_01K2C5R6J7ABCDEFGHJKMNPQRT"
    environ = _cli_environ(str(db_url), tenant_id)
    assert configure_main(
        environ, ["--actor-id", "employee:hunter-validator"]
    ) == 0
    capsys.readouterr()

    class _ControlledTransport:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, path, params, *, api_key):
            assert (path, params, api_key) == ("/account", (), API_KEY_CANARY)
            self.calls += 1
            return HunterHttpResponse(200, {"data": {"requests": {}}})

    transport = _ControlledTransport()
    argv = [
        "--actor-id",
        "employee:hunter-validator",
        "--idempotency-key",
        "hunter-cli-validation-1",
    ]
    first_code = validate_main(
        environ, argv, transport_factory=lambda: transport
    )
    first = json.loads(capsys.readouterr().out)
    duplicate_code = validate_main(
        environ, argv, transport_factory=lambda: transport
    )
    duplicate = json.loads(capsys.readouterr().out)

    assert first_code == duplicate_code == 0
    assert first == duplicate
    assert set(first) == {
        "tool_id",
        "status",
        "tool_call_id",
        "configuration_version",
    }
    assert first["tool_id"] == PROVIDER_VALIDATION_MANIFEST.tool_id
    assert first["status"] == "validation_passed"
    assert first["configuration_version"] == "deploy-v1"
    assert first["tool_call_id"].startswith("tcl_")
    assert transport.calls == 1
    exposed = repr((first, duplicate, capsys.readouterr()))
    assert API_KEY_CANARY not in exposed
    assert DEPLOYMENT_REF not in exposed


def test_cli_reconciliation_is_nonzero_and_prints_fixed_category_only(
    db_url: str, capsys
) -> None:
    tenant_id = "tn_01K2C5R6J7ABCDEFGHJKMNPQRV"
    environ = _cli_environ(str(db_url), tenant_id)
    assert configure_main(
        environ, ["--actor-id", "employee:hunter-validator"]
    ) == 0
    capsys.readouterr()

    class _UncertainTransport:
        async def get(self, path, params, *, api_key):
            del path, params, api_key
            raise HunterNetworkError(True) from RuntimeError(
                f"{NESTED_CANARY} {API_KEY_CANARY}"
            )

    status = validate_main(
        environ,
        [
            "--actor-id",
            "employee:hunter-validator",
            "--idempotency-key",
            "hunter-cli-uncertain-1",
        ],
        transport_factory=_UncertainTransport,
    )
    output = capsys.readouterr()
    payload = json.loads(output.err)
    assert status != 0
    assert set(payload) == {
        "tool_id",
        "category",
        "tool_call_id",
        "configuration_version",
    }
    assert payload["category"] == "reconciliation_required"
    exposed = repr((payload, output))
    for forbidden in (API_KEY_CANARY, DEPLOYMENT_REF, NESTED_CANARY):
        assert forbidden not in exposed
