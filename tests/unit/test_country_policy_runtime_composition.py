"""scheduler 只为已验证的当前 Hunter 配置注册联系人工具。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import CountryPolicyAction, CountryPolicyDecision
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CountryPolicyVersionId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    UserId,
)
from tool_gateway.checks.contact_provider import (
    ContactCountryPolicyCheck,
    ContactDiscoveryPreflight,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.pipeline import ToolCallContext, ToolInvocationState
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderConfiguration,
    ProviderId,
    ProviderReadinessSnapshot,
    ProviderReadinessState,
    ProviderReadinessUnavailableError,
)

TENANT = TenantId("ten_01J00000000000000000000000")
USER = UserId("usr_01J00000000000000000000000")
HYPOTHESIS = NeedHypothesisId("hyp_01J00000000000000000000000")
ACCOUNT = ProspectAccountId("acc_01J00000000000000000000000")
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
CONFIG = ProviderConfiguration.hunter_contacts("config-v1", "key-v1")


class _ComplianceService:
    def __init__(self, result: CountryPolicyDecision) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def get_country_policy_decision(
        self,
        tenant_id: TenantId,
        country: str,
        action: CountryPolicyAction,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyDecision:
        self.calls.append((tenant_id, country, action, actor))
        return self.result


@pytest.mark.asyncio
async def test_worker_policy_adapter_uses_real_compliance_service_and_system_actor() -> (
    None
):
    module = importlib.import_module("apps.scheduler_worker.account_discovery")
    adapter_type = getattr(module, "ComplianceCountryPolicyDecisionReader", None)
    if adapter_type is None:
        pytest.fail("RED：scheduler 尚无持久合规政策 adapter")
    actor = ComplianceActor(
        actor_id="system:scheduler-country-policy",
        tenant_id=TENANT,
        scope=ComplianceScope.SYSTEM,
        role="system",
    )
    expected = CountryPolicyDecision(
        country_key="synthetic market",
        action=CountryPolicyAction.CONTACT_ENRICHMENT,
        configured=True,
        allowed=True,
        active_version_id=CountryPolicyVersionId("cpp_01J00000000000000000000000"),
        content_hash="c" * 64,
        requirements=("human_review",),
    )
    compliance = _ComplianceService(expected)
    adapter = adapter_type(compliance, actor)

    state = ToolInvocationState(
        manifest=SimpleNamespace(tool_id="contact.enrich"),
        tool_call_id="tcl_01J00000000000000000000000",
    )
    state.preflight = ContactDiscoveryPreflight(
        TENANT,
        HYPOTHESIS,
        ACCOUNT,
        "synthetic-category",
        "Synthetic Market",
        "synthetic.example",
    )
    rejection = await ContactCountryPolicyCheck(adapter).check(
        ToolCallContext(TENANT, USER, "contact.enrich", {}),
        state,
    )

    assert rejection is None
    assert compliance.calls == [
        (
            TENANT,
            "synthetic market",
            CountryPolicyAction.CONTACT_ENRICHMENT,
            actor,
        )
    ]


class _DiscoveryPolicy:
    async def preflight(self, tenant_id, hypothesis_id, account_id):
        return ContactDiscoveryPreflight(
            tenant_id,
            hypothesis_id,
            account_id,
            "synthetic-category",
            "synthetic market",
            "synthetic.example",
        )


class _Secrets:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, secret_ref: str) -> str:
        del secret_ref
        self.calls += 1
        raise AssertionError("unregistered 工具不得解析凭证")


class _Transport:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, path, params, *, api_key):
        del path, params, api_key
        self.calls += 1
        raise AssertionError("unregistered 工具不得调用 Provider")


class _Outreach:
    async def is_suppressed(self, tenant_id, target, *, actor):
        del tenant_id, target, actor
        raise AssertionError("unregistered 工具不得查抑制")


class _Prospecting:
    async def get_contact_point(self, tenant_id, contact_point_id):
        del tenant_id, contact_point_id
        raise AssertionError("unregistered 工具不得读联系人")


class _DecisionReader:
    async def decision(self, tenant_id, country, action):
        del tenant_id, country
        return CountryPolicyDecision(
            country_key="synthetic market",
            action=action,
            configured=False,
            allowed=False,
            active_version_id=None,
            content_hash=None,
            requirements=(),
        )


class _Guard:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[tuple[TenantId, str]] = []

    async def require_current(
        self, tenant_id: TenantId, configuration_hash: str
    ) -> None:
        self.calls.append((tenant_id, configuration_hash))
        if self.error is not None:
            raise self.error


def _snapshot(
    state: ProviderReadinessState,
    *,
    tenant_id: TenantId = TENANT,
) -> ProviderReadinessSnapshot:
    configuration = (
        None if state is ProviderReadinessState.PROVIDER_NOT_CONFIGURED else CONFIG
    )
    return ProviderReadinessSnapshot(
        tenant_id=tenant_id,
        provider=ProviderId.HUNTER,
        capabilities=HUNTER_CONTACT_CAPABILITIES,
        configuration=configuration,
        state=state,
        failure_code=None,
        events=(),
    )


def _build_tools(
    snapshot: ProviderReadinessSnapshot | None,
    *,
    guard: _Guard | None = None,
):
    module = importlib.import_module("apps.scheduler_worker.hunter_contacts")
    secrets = _Secrets()
    transport = _Transport()
    composition = module.HunterContactComposition(
        discovery_policy=_DiscoveryPolicy(),
    )
    tools = module.build_hunter_contact_tools(
        factory=async_sessionmaker[AsyncSession](),
        tenant_id=TENANT,
        tool_user=USER,
        fingerprints=HmacFingerprintProvider("v1", b"x" * 32),
        outreach=_Outreach(),
        prospecting=_Prospecting(),
        country_policy=_DecisionReader(),
        composition=composition,
        secret_resolver=secrets,
        secret_ref="HUNTER_KEY_REF",
        transport=transport,
        snapshot=snapshot,
        readiness_guard=guard or _Guard(),
        lease_duration=timedelta(seconds=30),
        now=lambda: NOW,
    )
    return tools, secrets, transport


@pytest.mark.parametrize(
    "state",
    [
        ProviderReadinessState.PROVIDER_NOT_CONFIGURED,
        ProviderReadinessState.VALIDATION_NOT_RUN,
        ProviderReadinessState.VALIDATION_FAILED,
        ProviderReadinessState.VALIDATION_INCONCLUSIVE,
    ],
)
@pytest.mark.asyncio
async def test_nonvalidated_state_registers_neither_contact_tool(
    state: ProviderReadinessState,
) -> None:
    tools, secrets, transport = _build_tools(_snapshot(state))

    assert tools.registered_configuration_hash is None
    assert tools.manifest_ids == ()
    with pytest.raises(ValidationError, match="tool_id 未注册"):
        await tools.enricher.find_contacts(TENANT, HYPOTHESIS, ACCOUNT, ("buyer",))
    assert secrets.calls == 0
    assert transport.calls == 0


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        _snapshot(
            ProviderReadinessState.RUNTIME_NOT_COMPOSED,
            tenant_id=TenantId("ten_01J00000000000000000000001"),
        ),
    ],
)
def test_missing_or_stale_snapshot_registers_neither_contact_tool(
    snapshot: ProviderReadinessSnapshot | None,
) -> None:
    tools, secrets, transport = _build_tools(snapshot)

    assert tools.registered_configuration_hash is None
    assert tools.manifest_ids == ()
    assert secrets.calls == 0
    assert transport.calls == 0


def test_passed_current_configuration_registers_exactly_both_tools() -> None:
    tools, secrets, transport = _build_tools(
        _snapshot(ProviderReadinessState.RUNTIME_NOT_COMPOSED)
    )

    assert tools.registered_configuration_hash == CONFIG.configuration_hash
    assert tools.manifest_ids == ("contact.enrich", "contact.verify")
    assert secrets.calls == 0
    assert transport.calls == 0


def test_already_composed_current_configuration_registers_exactly_both_tools() -> None:
    tools, secrets, transport = _build_tools(_snapshot(ProviderReadinessState.READY))

    assert tools.registered_configuration_hash == CONFIG.configuration_hash
    assert tools.manifest_ids == ("contact.enrich", "contact.verify")
    assert secrets.calls == 0
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_unregistered_tools_stay_closed_when_live_guard_is_unavailable() -> None:
    guard = _Guard(ProviderReadinessUnavailableError())
    tools, secrets, transport = _build_tools(
        _snapshot(ProviderReadinessState.VALIDATION_FAILED),
        guard=guard,
    )

    with pytest.raises(ValidationError, match="tool_id 未注册"):
        await tools.enricher.find_contacts(TENANT, HYPOTHESIS, ACCOUNT, ("buyer",))

    assert guard.calls == []
    assert secrets.calls == 0
    assert transport.calls == 0
