"""旧真实已验证配置只在双工具完整组合后声明 runtime ready。"""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.checks.contact_provider import InMemoryHunterQuotaGuard
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
)

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
CONFIG = ProviderConfiguration.hunter_contacts("old-validated-v1", "old-key-v1")


def module():
    try:
        return importlib.import_module("apps.api.composition.customer_verification_runtime")
    except ModuleNotFoundError:
        pytest.fail("客户验证运行组合尚未实现")


def setup(state):
    readiness = SimpleNamespace(get_snapshot=AsyncMock(return_value=ProviderReadinessSnapshot(
        TENANT, ProviderId.HUNTER, HUNTER_CONTACT_CAPABILITIES, CONFIG, state, None, ())),
        mark_runtime_composed=AsyncMock(), require_current=AsyncMock())
    runtime = module().ConfiguredHunterCustomerVerificationRuntime(
        factory=None, tenant_id=TENANT, configuration=CONFIG, readiness=readiness,
        readiness_actor=ProviderReadinessActor("system:customer-verify", TENANT,
            frozenset({ProviderReadinessPermission.READ, ProviderReadinessPermission.COMPOSE})),
        prospecting=None, outreach=None, country_policy=None, discovery_policy=SimpleNamespace(preflight=AsyncMock()),
        quota=InMemoryHunterQuotaGuard(), secret_resolver=None, secret_ref="HUNTER_API_KEY_REF", transport=None,
        fingerprints=None,
    )
    return runtime, readiness


async def test_unvalidated_configuration_is_not_composed_or_promoted():
    runtime, readiness = setup(ProviderReadinessState.VALIDATION_NOT_RUN)
    assert await runtime.blocking_reasons(TENANT) == ("validation_not_run",)
    with pytest.raises(ValidationError):
        await runtime.compose()
    readiness.mark_runtime_composed.assert_not_awaited()


async def test_single_registered_capability_cannot_claim_full_runtime_ready(monkeypatch):
    runtime, readiness = setup(ProviderReadinessState.RUNTIME_NOT_COMPOSED)
    monkeypatch.setattr(module(), "build_hunter_contact_tools", lambda **kwargs: SimpleNamespace(
        manifest_ids=("contact.verify",), registered_configuration_hash=CONFIG.configuration_hash))
    with pytest.raises(ValidationError):
        await runtime.compose()
    readiness.mark_runtime_composed.assert_not_awaited()


async def test_current_configuration_mismatch_blocks_old_resolver():
    runtime, readiness = setup(ProviderReadinessState.READY)
    newer = ProviderConfiguration.hunter_contacts("new-v2", "new-key-v2")
    readiness.get_snapshot.return_value = ProviderReadinessSnapshot(TENANT, ProviderId.HUNTER, HUNTER_CONTACT_CAPABILITIES,
        newer, ProviderReadinessState.READY, None, ())
    assert await runtime.blocking_reasons(TENANT) == ("provider_configuration_changed",)
    with pytest.raises(ValidationError):
        await runtime.compose()
    readiness.mark_runtime_composed.assert_not_awaited()
