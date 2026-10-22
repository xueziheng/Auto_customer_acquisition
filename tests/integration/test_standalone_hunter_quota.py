"""真实 PG + 原 Gateway：跨实例额度、缓存及未知执行不会误放行。"""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.composition_support.hunter_contacts import (
    HunterContactComposition,
    build_hunter_contact_tools,
)
from connectors.hunter.transport import HunterHttpResponse, HunterNetworkError
from domains.prospecting.schemas import (
    ContactPointKind,
    ContactPointView,
    VerificationStatus,
)
from infra.db.tables import ToolCallEventRow, ToolCallRow
from shared.schemas.identifiers import (
    ContactId,
    ContactPointId,
    ProspectAccountId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def module():
    try:
        return importlib.import_module("apps.api.composition.standalone_contacts")
    except ModuleNotFoundError:
        pytest.fail("Hunter 持久客户验证预算尚未实现")


class Secrets:
    def resolve(self, reference):
        return "fixture-only-hunter-key"


class Sources:
    def __init__(self, tenant):
        self.tenant = tenant
        self.cached = False
        self.suppressed = False

    async def get_contact_point(self, tenant, point_id):
        assert tenant == self.tenant
        return ContactPointView(
            tenant_id=tenant,
            account_id=ProspectAccountId(new_id("acc")),
            contact_id=ContactId(new_id("con")),
            contact_point_id=point_id,
            kind=ContactPointKind.EMAIL,
            value="fixture@example.test",
            verification=VerificationStatus.VERIFIED
            if self.cached
            else VerificationStatus.UNVERIFIED,
            verified_at=NOW if self.cached else None,
            verification_provider="hunter" if self.cached else None,
            verification_checked_at=NOW if self.cached else None,
            verification_cost_note="charged" if self.cached else None,
            created_at=NOW,
        )

    async def is_suppressed(self, *args, **kwargs):
        return object() if self.suppressed else None

    async def preflight(self, *args):
        raise AssertionError("本入口不能补全联系人")

    async def decision(self, *args):
        raise AssertionError("验证不得新增国家动作")

    async def require_current(self, *args):
        return None


class Provider:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.unknown = False

    async def get(self, path, params, *, api_key):
        assert path == "/email-verifier"
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.unknown:
            raise HunterNetworkError(may_have_reached_provider=True)
        return HunterHttpResponse(200, {"data": {"status": "valid"}})


def tools(factory, tenant, provider, sources, *, maximum=1, now=NOW):
    config = ProviderConfiguration.hunter_contacts("fixture-v1", "fixture-key-v1")
    return build_hunter_contact_tools(
        factory=factory,
        tenant_id=tenant,
        tool_user=UserId(new_id("usr")),
        fingerprints=HmacFingerprintProvider(
            "fixture-v1", b"fixture-only-fingerprint-key-32bytes"
        ),
        outreach=sources,
        prospecting=sources,
        country_policy=sources,
        composition=HunterContactComposition(
            sources,
            module().PostgresHunterCustomerQuota(
                factory, tenant, maximum_daily_verifications=maximum, now=lambda: now
            ),
        ),
        secret_resolver=Secrets(),
        secret_ref="HUNTER_CUSTOMER_KEY",
        transport=provider,
        snapshot=ProviderReadinessSnapshot(
            tenant,
            ProviderId.HUNTER,
            HUNTER_CONTACT_CAPABILITIES,
            config,
            ProviderReadinessState.READY,
            None,
            (),
        ),
        readiness_guard=sources,
        lease_duration=timedelta(seconds=60),
        now=lambda: now,
    )


async def verify(instance, tenant):
    return await instance.verifier.verify(tenant, ContactPointId(new_id("cp")))


async def test_real_gateway_cross_instance_concurrent_provider_never_exceeds_tenant_day(
    integration_engine,
):
    factory = async_sessionmaker(integration_engine)
    tenant, provider = TenantId(new_id("tn")), Provider()
    sources = Sources(tenant)
    first = tools(factory, tenant, provider, sources)
    provider.release.clear()
    pending = asyncio.create_task(verify(first, tenant))
    await asyncio.wait_for(provider.entered.wait(), 3)
    second = tools(factory, tenant, provider, sources)
    with pytest.raises(ToolGatewayError) as denied:
        await verify(second, tenant)
    assert denied.value.category is ToolErrorCategory.RATE_LIMITED
    assert provider.calls == 1
    provider.release.set()
    await pending
    with pytest.raises(ToolGatewayError) as denied:
        await verify(tools(factory, tenant, provider, sources), tenant)
    assert denied.value.category is ToolErrorCategory.RATE_LIMITED
    assert provider.calls == 1


async def test_cache_hit_does_not_consume_budget_and_next_utc_day_is_independent(
    integration_engine,
):
    factory = async_sessionmaker(integration_engine)
    tenant, provider = TenantId(new_id("tn")), Provider()
    sources = Sources(tenant)
    sources.cached = True
    await verify(tools(factory, tenant, provider, sources), tenant)
    assert provider.calls == 0
    sources.cached = False
    await verify(tools(factory, tenant, provider, sources), tenant)
    assert provider.calls == 1
    await verify(
        tools(factory, tenant, provider, sources, now=NOW + timedelta(days=1)), tenant
    )
    assert provider.calls == 2
    async with factory() as session:
        events = (
            await session.scalars(
                select(ToolCallEventRow).where(
                    ToolCallEventRow.tenant_id == tenant,
                    ToolCallEventRow.rule == "hunter-customer-budget-v1",
                )
            )
        ).all()
        assert sorted(event.outcome for event in events) == [
            "cache_hit",
            "reservation",
            "reservation",
        ]
        assert all(
            event.cost_note in {"provider_budget_reserved", "provider_cache_hit"}
            for event in events
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_unknown_or_cancelled_provider_reservation_survives_new_instance(
    integration_engine, cancel
):
    factory = async_sessionmaker(integration_engine)
    tenant, provider = TenantId(new_id("tn")), Provider()
    sources = Sources(tenant)
    if cancel:
        provider.release.clear()
    else:
        provider.unknown = True
    pending = asyncio.create_task(
        verify(tools(factory, tenant, provider, sources), tenant)
    )
    await asyncio.wait_for(provider.entered.wait(), 3)
    if cancel:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        with pytest.raises(ToolGatewayError) as unknown:
            await pending
        assert unknown.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    with pytest.raises(ToolGatewayError) as denied:
        await verify(tools(factory, tenant, provider, sources), tenant)
    assert denied.value.category is ToolErrorCategory.RATE_LIMITED
    assert provider.calls == 1
    async with factory() as session:
        calls = (
            await session.scalars(
                select(ToolCallRow).where(ToolCallRow.tenant_id == tenant)
            )
        ).all()
        assert len(calls) == 2
        assert not any("fixture@example.test" in str(call.__dict__) for call in calls)


async def test_builder_startup_uses_prior_validation_without_key_or_account_io(
    integration_engine, tmp_path
):
    from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
    from infra.standalone.hunter_settings import HunterCustomerSettings
    from shared.schemas.identifiers import IdempotencyKey
    from tool_gateway.provider_readiness import (
        ProviderReadinessActor,
        ProviderReadinessPermission,
        ProviderReadinessServiceImpl,
    )

    factory, tenant = async_sessionmaker(integration_engine), TenantId(new_id("tn"))
    actor = ProviderReadinessActor(
        "system:fixture", tenant, frozenset(ProviderReadinessPermission)
    )
    readiness = ProviderReadinessServiceImpl(
        lambda t: SqlAlchemyProviderReadinessUnitOfWork(factory, t),
        runtime_actor=actor,
        now=lambda: NOW,
    )
    config = ProviderConfiguration.hunter_contacts("fixture-v1", "fixture-key-v1")
    await readiness.declare_configuration(
        tenant, config, actor=actor, idempotency_key=IdempotencyKey("fixture-config")
    )
    await readiness.mark_validation_started(
        tenant,
        config.configuration_hash,
        actor=actor,
        validation_key=IdempotencyKey("fixture-validation"),
    )
    await readiness.mark_validation_passed(
        tenant,
        config.configuration_hash,
        actor=actor,
        validation_key=IdempotencyKey("fixture-validation"),
        evidence_ref="hunter-account:" + new_id("pre"),
    )
    sources = Sources(tenant)
    dependencies = SimpleNamespace(
        prospecting=sources,
        outreach=sources,
        demand_service=object(),
        compliance=object(),
    )
    settings = HunterCustomerSettings(
        enabled=True,
        business_tenant_id=tenant,
        configuration_version="fixture-v1",
        api_key_version="fixture-key-v1",
        secret_file=tmp_path / "absent-key",
        maximum_daily_verifications=1,
    )
    app = module().build_standalone_customer_verification(
        factory,
        dependencies,
        tenant_id=tenant,
        settings=settings,
        fingerprints=HmacFingerprintProvider("fixture-v1", b"f" * 32),
        now=lambda: NOW,
    )
    await app.startup()
    snapshot = await readiness.get_snapshot(
        tenant, HUNTER_CONTACT_CAPABILITIES, actor=actor
    )
    assert snapshot.state is ProviderReadinessState.READY
    assert not settings.secret_file.exists()
    async with factory() as session:
        assert not (
            await session.scalars(
                select(ToolCallRow).where(ToolCallRow.tenant_id == tenant)
            )
        ).all()


async def test_provider_second_limit_is_retained_with_larger_explicit_daily_budget(
    integration_engine,
):
    factory, tenant, provider = (
        async_sessionmaker(integration_engine),
        TenantId(new_id("tn")),
        Provider(),
    )
    sources = Sources(tenant)
    for _ in range(10):
        await verify(tools(factory, tenant, provider, sources, maximum=1000), tenant)
    with pytest.raises(ToolGatewayError) as limited:
        await verify(tools(factory, tenant, provider, sources, maximum=1000), tenant)
    assert limited.value.category is ToolErrorCategory.RATE_LIMITED
    assert provider.calls == 10
    await verify(
        tools(
            factory,
            tenant,
            provider,
            sources,
            maximum=1000,
            now=NOW + timedelta(seconds=1),
        ),
        tenant,
    )
    assert provider.calls == 11


async def test_rejected_calls_do_not_spend_budget_and_other_tenant_is_independent(
    integration_engine,
):
    factory, tenant, provider = (
        async_sessionmaker(integration_engine),
        TenantId(new_id("tn")),
        Provider(),
    )
    sources = Sources(tenant)
    sources.suppressed = True
    with pytest.raises(ToolGatewayError) as denied:
        await verify(tools(factory, tenant, provider, sources), tenant)
    assert denied.value.category is ToolErrorCategory.SUPPRESSED
    sources.suppressed = False
    await verify(tools(factory, tenant, provider, sources), tenant)
    other = TenantId(new_id("tn"))
    await verify(tools(factory, other, provider, Sources(other)), other)
    assert provider.calls == 2
