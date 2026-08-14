"""dns.auth.check 通过通用 Tool Gateway 的完整持久调用合同。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from shared.errors import TransientError
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    IdempotencyKey,
    RunId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolRegistry,
)
from tool_gateway.pipeline import ToolCallContext, ToolGateway
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory

_NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
_TENANT = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")
_USER = UserId("usr_01K2C5R6J7ABCDEFGHJKMNPQRS")
_RUN = RunId("run_01K2C5R6J7ABCDEFGHJKMNPQRS")
_REQUEST = AuthenticationCheckRequestId("acr_01K2C5R6J7ABCDEFGHJKMNPQRS")
_IDENTITY = SendingIdentityId("sid_01K2C5R6J7ABCDEFGHJKMNPQRS")


@pytest_asyncio.fixture
async def dns_gateway_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """函数级 engine 与 pytest loop 同生共死，避免 asyncpg 跨 loop。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _handler_module():
    try:
        return importlib.import_module("tool_gateway.handlers.dns_auth")
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：dns.auth.check handler 尚未创建（{exc.name}）")


class _Allow:
    def __init__(self, name: str) -> None:
        self.name = name

    async def check(self, ctx, state):
        del ctx, state


class _Connector:
    def __init__(self, facts: object) -> None:
        self.facts = facts
        self.calls = 0

    async def check(self, request):
        self.calls += 1
        return self.facts


def test_manifest_is_exact_read_only_external_tool() -> None:
    """错误 risk/stages/幂等会绕过既定 Gateway 证据链。"""
    manifest = _handler_module().MANIFEST
    assert manifest.tool_id == "dns.auth.check"
    assert manifest.risk_level is RiskLevel.LOW
    assert manifest.cost_class is CostClass.FREE
    assert manifest.requires_approval is False
    assert manifest.idempotency is IdempotencyRequirement.REQUIRED
    assert manifest.required_permissions == ("sending_identity:auth_check",)
    assert manifest.checks == (
        "tenant",
        "permission",
        "idempotency",
        "rate_limit",
    )
    assert manifest.high_risk_stage_profile is None


def test_facts_capsule_preserves_exact_utc_microseconds() -> None:
    """Gateway capsule 不得把同秒 DNS 检查时间截断到 identity 创建之前。"""
    contracts = importlib.import_module("shared.schemas.dns_auth")
    module = _handler_module()
    checked_at = _NOW.replace(microsecond=950_000)
    facts = contracts.DnsAuthenticationFacts(
        checked_at, True, True, True, (), "f" * 64
    )
    assert module._decode_facts(module._encode_facts(facts)).checked_at == checked_at


async def test_gateway_duplicate_executes_one_dns_check_and_persists_no_raw_dns(
    dns_gateway_engine: AsyncEngine,
) -> None:
    """同一幂等键只查一次；ledger 只能保存安全 capsule/ID。"""
    contracts = importlib.import_module("shared.schemas.dns_auth")
    handler_module = _handler_module()
    facts = contracts.DnsAuthenticationFacts(
        _NOW,
        True,
        True,
        True,
        (),
        "c" * 64,
    )
    connector = _Connector(facts)
    handler = handler_module.DnsAuthenticationCheckHandler(
        connector,
        HmacFingerprintProvider("v1", b"task4-test-fingerprint-key-012345"),
    )
    registry = ToolRegistry()
    registry.register(handler_module.MANIFEST, handler)
    factory = async_sessionmaker(bind=dns_gateway_engine, expire_on_commit=False)
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork

    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        {
            "tenant": _Allow("tenant"),
            "permission": _Allow("permission"),
            "idempotency": IdempotencyCheck(),
            "rate_limit": _Allow("rate_limit"),
        },
        cast(
            ToolGatewayUnitOfWorkFactory,
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
                factory, tenant, now=lambda: _NOW
            ),
        ),
        lease_duration=timedelta(minutes=1),
        lease_owner="dns-auth-test",
        now=lambda: _NOW,
        id_factory=new_id,
    )
    checker = handler_module.ToolGatewayDnsAuthenticationChecker(gateway, _USER)
    kwargs = {
        "tenant_id": _TENANT,
        "run_id": _RUN,
        "request_id": _REQUEST,
        "sending_identity_id": _IDENTITY,
        "domain": "example.co.uk",
        "dkim_selector": "s1",
    }
    first = await checker.check(**kwargs)
    second = await checker.check(**kwargs)
    assert first == second == facts
    assert connector.calls == 1

    async with dns_gateway_engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT tool_id,provider_ref,error_category FROM tool_calls "
                    "WHERE tenant_id=:tenant AND tool_id='dns.auth.check'"
                ),
                {"tenant": str(_TENANT)},
            )
        ).all()
    assert len(rows) == 2
    assert {row.tool_id for row in rows} == {"dns.auth.check"}
    assert all(row.error_category is None for row in rows)
    assert all("v=spf1" not in (row.provider_ref or "") for row in rows)
    assert all("secret" not in (row.provider_ref or "") for row in rows)


async def test_handler_rejects_extra_or_noncanonical_params_before_connector() -> None:
    """自由参数、坏 ID 和 credential-shaped 值不得到达 Connector。"""
    contracts = importlib.import_module("shared.schemas.dns_auth")
    module = _handler_module()
    connector = _Connector(
        contracts.DnsAuthenticationFacts(_NOW, True, True, True, (), "d" * 64)
    )
    handler = module.DnsAuthenticationCheckHandler(
        connector,
        HmacFingerprintProvider("v1", b"task4-test-fingerprint-key-012345"),
    )
    base = {
        "request_id": str(_REQUEST),
        "sending_identity_id": str(_IDENTITY),
        "domain": "example.co.uk",
        "dkim_selector": "s1",
    }
    for params in (
        {**base, "raw_record": "v=spf1 -all"},
        {**base, "request_id": "acr-bad"},
        {**base, "dkim_selector": "Bearer-secret"},
    ):
        with pytest.raises(Exception) as caught:
            await handler.prepare(
                ToolCallContext(
                    _TENANT,
                    _USER,
                    "dns.auth.check",
                    params,
                    run_id=_RUN,
                    idempotency_key=IdempotencyKey(f"auth:{_REQUEST}"),
                ),
                None,
            )
        assert "v=spf1" not in str(caught.value)
        assert "Bearer-secret" not in str(caught.value)
    assert connector.calls == 0


async def test_read_only_dns_reconciles_after_ledger_completion_failure(
    dns_gateway_engine: AsyncEngine,
) -> None:
    """DNS 读取可安全重做；reconciliation claim 不得永久卡死。"""
    from infra.db.tables import ToolCallRow
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork

    contracts = importlib.import_module("shared.schemas.dns_auth")
    module = _handler_module()
    facts = contracts.DnsAuthenticationFacts(_NOW, True, True, True, (), "e" * 64)
    connector = _Connector(facts)
    handler = module.DnsAuthenticationCheckHandler(
        connector,
        HmacFingerprintProvider("v1", b"task4-reconcile-key-0123456789abcd"),
    )
    registry = ToolRegistry()
    registry.register(module.MANIFEST, handler)
    factory = async_sessionmaker(bind=dns_gateway_engine, expire_on_commit=False)
    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        {
            "tenant": _Allow("tenant"),
            "permission": _Allow("permission"),
            "idempotency": IdempotencyCheck(),
            "rate_limit": _Allow("rate_limit"),
        },
        cast(
            ToolGatewayUnitOfWorkFactory,
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
                factory, tenant, now=lambda: _NOW
            ),
        ),
        lease_duration=timedelta(minutes=1),
        lease_owner="dns-auth-reconcile",
        now=lambda: _NOW,
        id_factory=new_id,
    )
    checker = module.ToolGatewayDnsAuthenticationChecker(gateway, _USER)
    kwargs = {
        "tenant_id": _TENANT,
        "run_id": _RUN,
        "request_id": AuthenticationCheckRequestId(
            "acr_01K2C5R6J7ABCDEFGHJKMNPQRT"
        ),
        "sending_identity_id": _IDENTITY,
        "domain": "example.co.uk",
        "dkim_selector": "s1",
    }
    assert await checker.check(**kwargs) == facts
    async with factory() as session:
        await session.execute(
            update(ToolCallRow)
            .where(
                ToolCallRow.tenant_id == _TENANT,
                ToolCallRow.tool_id == "dns.auth.check",
                ToolCallRow.idempotency_key
                == f"auth:{kwargs['request_id']}",
            )
            .values(
                status="failed_transient",
                provider_ref=None,
                error_category="reconciliation_required",
                lease_owner="crashed-worker",
                lease_expires_at=_NOW - timedelta(seconds=1),
                retry_after_at=None,
                completed_at=None,
            )
        )
        await session.commit()
    assert await checker.check(**kwargs) == facts
    assert connector.calls == 2


async def test_post_claim_dns_rate_limit_finishes_canonical_as_retryable(
    dns_gateway_engine: AsyncEngine,
) -> None:
    """超限发生在 claim 后，必须完成 canonical transient 而非留下 CLAIMED。"""
    from apps.scheduler_worker.runtime import _DnsReadRateLimitCheck
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork

    contracts = importlib.import_module("shared.schemas.dns_auth")
    module = _handler_module()
    connector = _Connector(
        contracts.DnsAuthenticationFacts(_NOW, True, True, True, (), "7" * 64)
    )
    registry = ToolRegistry()
    registry.register(
        module.MANIFEST,
        module.DnsAuthenticationCheckHandler(
            connector,
            HmacFingerprintProvider("v1", b"r" * 32),
        ),
    )
    factory = async_sessionmaker(bind=dns_gateway_engine, expire_on_commit=False)
    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        {
            "tenant": _Allow("tenant"),
            "permission": _Allow("permission"),
            "idempotency": IdempotencyCheck(),
            "rate_limit": _DnsReadRateLimitCheck(
                max_calls=1, window_seconds=60, now=lambda: _NOW
            ),
        },
        cast(
            ToolGatewayUnitOfWorkFactory,
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
                factory, tenant, now=lambda: _NOW
            ),
        ),
        lease_duration=timedelta(seconds=30),
        lease_owner="dns-auth-rate",
        now=lambda: _NOW,
        id_factory=new_id,
    )
    checker = module.ToolGatewayDnsAuthenticationChecker(gateway, _USER)
    common = {
        "tenant_id": _TENANT,
        "run_id": _RUN,
        "sending_identity_id": _IDENTITY,
        "domain": "example.co.uk",
        "dkim_selector": "s1",
    }
    first_request = AuthenticationCheckRequestId(new_id("acr"))
    assert await checker.check(request_id=first_request, **common)
    second_request = AuthenticationCheckRequestId(new_id("acr"))
    with pytest.raises(TransientError):
        await checker.check(request_id=second_request, **common)

    async with dns_gateway_engine.connect() as connection:
        status, category = (
            await connection.execute(
                text(
                    "SELECT status,error_category FROM tool_calls "
                    "WHERE tenant_id=:tenant AND tool_id='dns.auth.check' "
                    "AND idempotency_key=:key"
                ),
                {"tenant": str(_TENANT), "key": f"auth:{second_request}"},
            )
        ).one()
    assert (status, category) == ("failed_transient", "rate_limited")
    assert connector.calls == 1
