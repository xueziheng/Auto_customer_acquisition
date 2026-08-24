"""scheduler 只消费持久合规服务，且生产不注册联系人补全工具。"""

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

TENANT = TenantId("ten_01J00000000000000000000000")
USER = UserId("usr_01J00000000000000000000000")
HYPOTHESIS = NeedHypothesisId("hyp_01J00000000000000000000000")
ACCOUNT = ProspectAccountId("acc_01J00000000000000000000000")
NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


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
        del tenant_id, hypothesis_id, account_id
        raise AssertionError("unregistered 工具不得读取 preflight")


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


@pytest.mark.asyncio
async def test_production_contact_enrich_is_unregistered_before_provider() -> None:
    module = importlib.import_module("apps.scheduler_worker.hunter_contacts")
    secrets = _Secrets()
    transport = _Transport()
    composition = module.HunterContactComposition(
        discovery_policy=_DiscoveryPolicy(),
        secret_resolver=secrets,
        secret_ref="HUNTER_KEY_REF",
        transport=transport,
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
        lease_duration=timedelta(seconds=30),
        now=lambda: NOW,
    )

    with pytest.raises(ValidationError, match="tool_id 未注册"):
        await tools.enricher.find_contacts(TENANT, HYPOTHESIS, ACCOUNT, ("buyer",))

    assert secrets.calls == 0
    assert transport.calls == 0
