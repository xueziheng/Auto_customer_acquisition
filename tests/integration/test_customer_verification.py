"""真实 PG 归属、原 contact.verify Gateway 与 typed 观察落库。"""

import importlib
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from apps.api.dependencies import get_request_identity
from connectors.email_verification.client import (
    EmailVerificationOutcome,
)
from domains.prospecting.schemas import (
    ContactPointKind,
    ContactType,
    DiscoveredContactRequest,
    LegalBasisInput,
    LegalBasisType,
    SubjectType,
)
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.customer_followup import PostgresCustomerContextReader
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from infra.db.tables import EmployeeRow, ToolCallRow
from shared.errors import PermissionDenied, PolicyViolation
from shared.schemas.identifiers import new_id
from tests.integration.test_customer_assignment_bridge import NOW
from tests.integration.test_customer_assignment_bridge import (
    bridge_inventory as bridge_inventory,  # noqa: PLC0414
)
from tests.integration.test_customer_followup_api import app_fixture
from tests.integration.test_customer_preparation import TestHasher
from tool_gateway.checks.contact_provider import InMemoryHunterQuotaGuard
from tool_gateway.fingerprint import HmacFingerprintProvider


def module():
    try:
        return importlib.import_module("apps.api.composition.customer_verification")
    except ModuleNotFoundError:
        pytest.fail("客户验证应用尚未实现")


class Policy:
    async def require_current(self, identity, account, point):
        assert account.account_id == point.contact_point.account_id
        assert point.assessment_ref == "reviewed-assessment-fixture"


class Suppression:
    blocked = False

    async def is_suppressed(self, tenant, target, *, actor):
        return object() if self.blocked else None


class Provider:
    def __init__(self, outcome=EmailVerificationOutcome.VERIFIED):
        self.calls = 0
        self.outcome = outcome
        self.privacy_claimed = False

    async def get(self, path, params, *, api_key):
        from connectors.hunter.transport import HunterHttpResponse
        self.calls += 1
        assert path == "/email-verifier" and params == (("email", "sales@company.test"),)
        assert api_key == "fixture-only-hunter-key"
        if self.privacy_claimed:
            from connectors.hunter.transport import (
                HunterErrorCode,
                HunterHttpStatusError,
            )
            raise HunterHttpStatusError(451, HunterErrorCode.CLAIMED_EMAIL)
        status = {EmailVerificationOutcome.VERIFIED: "valid", EmailVerificationOutcome.INVALID: "invalid",
                  EmailVerificationOutcome.RISKY: "accept_all", EmailVerificationOutcome.UNVERIFIED: "unknown"}[self.outcome]
        return HunterHttpResponse(200, {"data": {"status": status}})


async def setup(bridge_inventory, *, outcome=EmailVerificationOutcome.VERIFIED):
    app, allocation, _, _ = await app_fixture(bridge_inventory)
    identity = app.dependency_overrides[get_request_identity]()
    _, _, factory, tenant, _, _, _ = bridge_inventory
    prospecting = ProspectingServiceImpl(lambda t: SqlAlchemyProspectingUnitOfWork(factory, t), TestHasher(), now=lambda: NOW)
    contact = await prospecting.record_discovered_contact(tenant, DiscoveredContactRequest(
        allocation.account_id, ContactPointKind.EMAIL, "sales@company.test",
        LegalBasisInput(LegalBasisType.LEGITIMATE_INTEREST, SubjectType.LEGAL_ENTITY, ContactType.ROLE_BASED,
                        "public-company-page", NOW, assessment_ref="reviewed-assessment-fixture"),
    ))
    provider, suppression = Provider(outcome), Suppression()

    from apps.api.composition.customer_verification_runtime import (
        ConfiguredHunterCustomerVerificationRuntime,
    )
    from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
    from shared.schemas.identifiers import IdempotencyKey
    from tool_gateway.provider_readiness import (
        ProviderConfiguration,
        ProviderReadinessActor,
        ProviderReadinessPermission,
        ProviderReadinessServiceImpl,
    )
    actor = ProviderReadinessActor("system:fixture", tenant, frozenset(ProviderReadinessPermission))
    readiness = ProviderReadinessServiceImpl(lambda t: SqlAlchemyProviderReadinessUnitOfWork(factory, t), runtime_actor=actor, now=lambda: NOW)
    config = ProviderConfiguration.hunter_contacts("fixture-v1", "fixture-key-v1")
    await readiness.declare_configuration(tenant, config, actor=actor, idempotency_key=IdempotencyKey("fixture-config"))
    await readiness.mark_validation_started(tenant, config.configuration_hash, actor=actor, validation_key=IdempotencyKey("fixture-validation"))
    await readiness.mark_validation_passed(tenant, config.configuration_hash, actor=actor,
        validation_key=IdempotencyKey("fixture-validation"), evidence_ref="hunter-account:" + new_id("pre"))

    class Secrets:
        def resolve(self, reference):
            assert reference == "HUNTER_API_KEY_REF"
            return "fixture-only-hunter-key"

    class MissingDiscoveryPolicy:
        async def preflight(self, *args):
            raise PolicyViolation("测试未配置补全政策")

    class MissingCountryPolicy:
        async def decision(self, *args):
            raise PolicyViolation("验证不得冒充国家补全政策")

    runtime = ConfiguredHunterCustomerVerificationRuntime(
        factory=factory, tenant_id=tenant, configuration=config, readiness=readiness, readiness_actor=actor,
        prospecting=prospecting, outreach=suppression, country_policy=MissingCountryPolicy(), discovery_policy=MissingDiscoveryPolicy(),
        quota=InMemoryHunterQuotaGuard(), secret_resolver=Secrets(), secret_ref="HUNTER_API_KEY_REF", transport=provider,
        fingerprints=HmacFingerprintProvider("test-v1", b"f" * 32), now=lambda: NOW + timedelta(seconds=2))
    await runtime.compose()

    application = module().CustomerVerificationApplication(
        PostgresCustomerContextReader(factory, tenant), prospecting, runtime, policy=module().RecordedContactBasisPolicy()
    )
    return application, identity, allocation, contact.contact_point_id, prospecting, provider, suppression


@pytest.mark.parametrize("outcome", list(EmailVerificationOutcome))
async def test_real_gateway_result_is_persisted_exactly_and_cached(bridge_inventory, outcome):
    application, identity, allocation, point_id, prospecting, provider, _ = await setup(bridge_inventory, outcome=outcome)
    command = module().CustomerVerificationCommand(contact_point_id=point_id)
    first = await application.verify(identity, allocation.id, command)
    second = await application.verify(identity, allocation.id, command)
    assert first.result.value == second.result.value == outcome.value
    assert second.cache_hit and provider.calls == 1
    point = await prospecting.get_contact_point(identity.tenant_id, point_id)
    assert point.verification.value == outcome.value
    assert point.verification_cost_note == "hunter.email_verifier.counted"
    async with bridge_inventory[2]() as session:
        rows = (await session.scalars(select(ToolCallRow).where(ToolCallRow.tenant_id == identity.tenant_id))).all()
        assert len(rows) == 2 and all(row.status == "succeeded" for row in rows)
        assert all("company.test" not in (row.provider_ref or "") for row in rows)


async def test_foreign_contact_never_reaches_provider(bridge_inventory):
    application, identity, allocation, _, _, provider, _ = await setup(bridge_inventory)
    with pytest.raises(PermissionDenied):
        await application.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=new_id("cp")))
    assert provider.calls == 0


async def test_inactive_current_employee_and_policy_block_before_provider(bridge_inventory):
    application, identity, allocation, point, _, provider, _ = await setup(bridge_inventory)
    application_without_policy = module().CustomerVerificationApplication(application._reader, application._prospecting, application._runtime)
    view = await application_without_policy.read(identity, allocation.id)
    assert not view.can_verify and "verification_policy_not_configured" in view.blocking_reasons
    with pytest.raises(PolicyViolation):
        await application_without_policy.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=point))
    async with bridge_inventory[2]() as session, session.begin():
        await session.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == identity.tenant_id, EmployeeRow.employee_id == identity.employee.employee_id).values(is_active=False))
    with pytest.raises(PermissionDenied):
        await application.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=point))
    assert provider.calls == 0


async def test_suppression_rejection_does_not_write_verified(bridge_inventory):
    from tool_gateway.errors import ToolGatewayError

    application, identity, allocation, point, prospecting, provider, suppression = await setup(bridge_inventory)
    suppression.blocked = True
    with pytest.raises(ToolGatewayError):
        await application.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=point))
    assert provider.calls == 0
    assert (await prospecting.get_contact_point(identity.tenant_id, point)).verification.value == "unverified"


@pytest.mark.parametrize("reason", ["unsubscribe", "manual_block"])
@pytest.mark.parametrize("cached", [False, True])
async def test_real_account_suppression_blocks_verification_and_cache(bridge_inventory, monkeypatch, reason, cached):
    from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
    from domains.outreach.schemas import (
        SuppressionReason,
        SuppressionRequest,
        SuppressionTarget,
    )
    from domains.prospecting.schemas import (
        VerificationRecordRequest,
        VerificationStatus,
    )
    from shared.schemas.identifiers import IdempotencyKey
    from tests.integration.test_outreach_enrollment_lifecycle import (
        MutableClock,
        _service,
    )
    from tool_gateway.errors import ToolErrorCategory, ToolGatewayError

    application, identity, allocation, point, prospecting, provider, _ = await setup(bridge_inventory)
    outreach = _service(bridge_inventory[2], identity.tenant_id, new_id("cmp"), new_id("apr"), (), {}, MutableClock(NOW))
    await outreach.add_suppression(identity.tenant_id, SuppressionRequest(
        SuppressionTarget(account_id=allocation.account_id), SuppressionReason(reason), NOW,
        "customer-refusal-fixture", IdempotencyKey("account-suppression-fixture")),
        actor=Actor("boss:fixture", OutreachScope(level=ScopeLevel.TENANT), "boss"))
    if cached:
        await prospecting.record_verification(identity.tenant_id, VerificationRecordRequest(
            point, VerificationStatus.VERIFIED, "hunter", NOW, "hunter.email_verifier.counted"))
    current_reader = outreach.is_suppressed
    targets = []

    async def scoped_reader(tenant_id, target, *, actor):
        assert tenant_id == identity.tenant_id
        assert actor.scope.allowed_suppression_targets == frozenset({target.canonical_id})
        targets.append(target)
        return await current_reader(tenant_id, target, actor=actor)

    monkeypatch.setattr(outreach, "is_suppressed", scoped_reader)
    application._runtime._outreach = outreach
    with pytest.raises(ToolGatewayError) as failure:
        await application.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=point))
    assert failure.value.category is ToolErrorCategory.SUPPRESSED
    assert targets == [SuppressionTarget(contact_point_id=point), SuppressionTarget(account_id=allocation.account_id)]
    assert provider.calls == 0


async def test_quota_rejection_never_calls_provider_or_changes_observation(bridge_inventory):
    from tool_gateway.errors import ToolGatewayError
    application, identity, allocation, point, prospecting, provider, _ = await setup(bridge_inventory)

    class DeniedQuota:
        async def reserve(self, tenant_id, capability, now):
            return 60

    application._runtime._quota = DeniedQuota()
    with pytest.raises(ToolGatewayError):
        await application.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=point))
    assert provider.calls == 0
    assert (await prospecting.get_contact_point(identity.tenant_id, point)).verification.value == "unverified"


async def test_privacy_erasure_retry_uses_recorded_refusal_without_provider_recall(bridge_inventory, monkeypatch):
    from shared.errors import TransientError

    application, identity, allocation, point, prospecting, provider, _ = await setup(bridge_inventory)
    provider.privacy_claimed = True
    command = module().CustomerVerificationCommand(contact_point_id=point)
    original_erasure = prospecting.handle_erasure_request

    async def unavailable(*args):
        raise TransientError("测试删除事务暂不可用")

    monkeypatch.setattr(prospecting, "handle_erasure_request", unavailable)
    with pytest.raises(TransientError):
        await application.verify(identity, allocation.id, command)
    assert (await prospecting.get_contact_point(identity.tenant_id, point)).verification_cost_note == "hunter.email_verifier.privacy_refused"
    monkeypatch.setattr(prospecting, "handle_erasure_request", original_erasure)
    recovered = await application.verify(identity, allocation.id, command)
    assert recovered.privacy_erased and not recovered.cache_hit and recovered.result.value == "unverified"
    assert provider.calls == 1
    assert (await prospecting.get_account_detail(identity.tenant_id, allocation.account_id)).contacts == ()


@pytest.mark.parametrize("runtime_state", ["disabled", "expired", "configuration_changed"])
async def test_recorded_privacy_refusal_is_erased_locally_even_without_ready_verification(bridge_inventory, runtime_state):
    from domains.prospecting.schemas import (
        VerificationRecordRequest,
        VerificationStatus,
    )
    from tool_gateway.provider_readiness import ProviderConfiguration

    application, identity, allocation, point, prospecting, provider, _ = await setup(bridge_inventory)
    await prospecting.record_verification(identity.tenant_id, VerificationRecordRequest(
        point, VerificationStatus.UNVERIFIED, "hunter", NOW, "hunter.email_verifier.privacy_refused"))
    if runtime_state == "disabled":
        application = module().CustomerVerificationApplication(application._reader, prospecting)
    elif runtime_state == "expired":
        application._runtime._now = lambda: NOW + timedelta(days=31)
    else:
        application._runtime._configuration = ProviderConfiguration.hunter_contacts("changed-v2", "changed-key-v2")
    view = await application.read(identity, allocation.id)
    assert view.contacts[0].can_verify
    result = await application.verify(identity, allocation.id, module().CustomerVerificationCommand(contact_point_id=point))
    assert result.privacy_erased and not result.cache_hit
    assert result.result.value == "unverified" and result.checked_at == NOW
    assert provider.calls == 0
    assert (await prospecting.get_account_detail(identity.tenant_id, allocation.account_id)).contacts == ()


async def test_http_contract_only_accepts_contact_id_and_returns_actual_state(bridge_inventory):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from apps.api.dependencies import get_api_dependencies
    from apps.api.routers.customer_verification import router

    application, identity, allocation, point, _, provider, _ = await setup(bridge_inventory)
    app = FastAPI()
    app.include_router(router, prefix="/prospects")
    app.dependency_overrides[get_request_identity] = lambda: identity
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(customer_verification=application)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/prospects/customers/{allocation.id}/verification"
        view = await client.get(path)
        assert view.status_code == 200 and view.json()["can_verify"] is True
        bad = await client.post(path, json={"contact_point_id": point, "verification": "verified"})
        assert bad.status_code == 422 and provider.calls == 0
        actual = await client.post(path, json={"contact_point_id": point})
        assert actual.status_code == 200 and actual.json()["result"] == "verified"
        assert provider.calls == 1
