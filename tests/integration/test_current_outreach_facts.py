"""当前公开事实读取：真实 PostgreSQL 与公开写入口。"""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.conversations.schemas import ReplyCategory
from domains.conversations.service_impl import ConversationServiceImpl
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service_impl import DemandServiceImpl
from domains.prospecting.errors import ContactPointNotFoundError
from domains.prospecting.schemas import (
    AccountResolveRequest,
    ContactCreateRequest,
    ContactPointCreateRequest,
    ContactPointKind,
    ContactType,
    LegalBasisInput,
    LegalBasisType,
    SubjectType,
)
from domains.prospecting.service_impl import ProspectingServiceImpl
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
from shared.errors import PermissionDenied
from shared.schemas.identifiers import ProspectAccountId, TenantId, new_id

NOW = datetime(2026, 9, 5, tzinfo=UTC)


class Hasher:
    def fingerprint(self, canonical_value: str) -> str:
        return hashlib.sha256(canonical_value.encode()).hexdigest()


async def test_contact_fact_is_exact_and_address_free(integration_engine):
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    service = ProspectingServiceImpl(
        lambda t: SqlAlchemyProspectingUnitOfWork(sessions, t, now=lambda: NOW),
        Hasher(),
        now=lambda: NOW,
    )
    tenant = TenantId(new_id("tn"))
    account = await service.resolve_account(
        tenant, AccountResolveRequest("Acme", "DE", entity_type="manufacturer")
    )
    contact = await service.create_contact(tenant, ContactCreateRequest(account))
    point = await service.add_contact_point(
        tenant,
        ContactPointCreateRequest(
            contact,
            ContactPointKind.EMAIL,
            "buyer@example.test",
            LegalBasisInput(
                LegalBasisType.LEGITIMATE_INTEREST,
                SubjectType.LEGAL_ENTITY,
                ContactType.PERSONAL_BUSINESS,
                "company_website",
                NOW,
                assessment_ref="lia-1",
            ),
        ),
    )
    fact = await service.get_outreach_contact_facts(tenant, account, point)
    assert fact.account_id == account and fact.contact_point_id == point
    assert fact.legal_basis_ref == "lia-1"
    assert "buyer@example.test" not in repr(fact)
    with pytest.raises(ContactPointNotFoundError):
        await service.get_outreach_contact_facts(
            tenant, ProspectAccountId(new_id("acc")), point
        )
    with pytest.raises(ContactPointNotFoundError):
        await service.get_outreach_contact_facts(TenantId(new_id("tn")), account, point)


async def test_hypothesis_categories_follow_rejection_and_account(integration_engine):
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    service = DemandServiceImpl(
        lambda t: SqlAlchemyDemandUnitOfWork(sessions, t, now=lambda: NOW),
        now=lambda: NOW,
    )
    tenant, account = TenantId(new_id("tn")), ProspectAccountId(new_id("acc"))
    signal = await service.capture_signal(
        tenant,
        SignalCaptureRequest(
            "product_line_expansion",
            "Acme",
            "new facility",
            NOW,
            "employee_input",
            "manual-1",
            "human",
        ),
    )
    hypothesis = await service.create_hypothesis(
        tenant,
        account,
        "hinges",
        [str(signal)],
        "facility may require hinges",
        "model-v1",
    )
    assert (
        await service.get_outreach_hypothesis_categories(tenant, account)
    ).categories == ("hinges",)
    assert (
        await service.get_outreach_hypothesis_categories(
            tenant, ProspectAccountId(new_id("acc"))
        )
    ).categories == ()
    await service.reject_hypothesis(tenant, hypothesis, "no_need")
    assert (
        await service.get_outreach_hypothesis_categories(tenant, account)
    ).categories == ()


async def test_reply_unknown_auto_history_and_current_correction(integration_engine):
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    service = ConversationServiceImpl(
        lambda t: SqlAlchemyConversationsUnitOfWork(sessions, t, now=lambda: NOW),
        now=lambda: NOW,
    )
    tenant, account = TenantId(new_id("tn")), ProspectAccountId(new_id("acc"))
    assert (await service.get_account_reply_status(tenant, account)).state == "no_reply"
    message = await service.ingest_inbound(
        tenant, None, account, "art_test", "<first@test.invalid>", NOW
    )
    assert (await service.get_account_reply_status(tenant, account)).state == "unknown"
    await service.record_classification(
        tenant, message, ReplyCategory.AUTO_REPLY, "model-v1"
    )
    assert (await service.get_account_reply_status(tenant, account)).state == "no_reply"
    await service.correct_classification(
        tenant, message, ReplyCategory.REJECTION, "employee-1"
    )
    assert (await service.get_account_reply_status(tenant, account)).state == "replied"
    later = await service.ingest_inbound(
        tenant, None, account, "art_test2", "<later@test.invalid>", NOW
    )
    await service.record_classification(
        tenant, later, ReplyCategory.AUTO_REPLY, "model-v1"
    )
    from sqlalchemy import event

    statements = []

    def count_statement(*args):
        statements.append(1)

    event.listen(
        integration_engine.sync_engine, "before_cursor_execute", count_statement
    )
    try:
        assert (
            await service.get_account_reply_status(tenant, account)
        ).state == "replied"
        assert len(statements) == 1
    finally:
        event.remove(
            integration_engine.sync_engine, "before_cursor_execute", count_statement
        )
    assert (
        await service.get_account_reply_status(TenantId(new_id("tn")), account)
    ).state == "no_reply"


async def test_real_sender_reader_does_not_reserve_and_reads_exhaustion(
    integration_engine,
):
    import importlib

    from domains.sending_identity.permissions import (
        Actor,
        Phase1SendingIdentityAuthorizer,
        ScopeLevel,
        SendingIdentityScope,
        StandardAuditLogger,
    )
    from domains.sending_identity.schemas import (
        AuthenticationResult,
        IdentityRegisterRequest,
    )
    from domains.sending_identity.service_impl import SendingIdentityServiceImpl
    from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
    from shared.schemas.identifiers import IdempotencyKey

    module = importlib.import_module("apps.composition_support.outreach_fact_readers")
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = SendingIdentityServiceImpl(
        lambda t: SqlAlchemySendingIdentityUnitOfWork(sessions, t, now=lambda: NOW),
        Phase1SendingIdentityAuthorizer(tenant),
        StandardAuditLogger(),
        now=lambda: NOW,
    )
    models = importlib.import_module("domains.sending_identity.models")
    boss = Actor("boss:test", SendingIdentityScope(level=ScopeLevel.TENANT), "boss")
    identity = await service.register(
        tenant,
        IdentityRegisterRequest(
            "sender@outreach.example",
            "outreach.example",
            models.DomainRole.COLD_OUTREACH,
        ),
        actor=boss,
    )
    actor = Actor(
        "system:test",
        SendingIdentityScope(
            level=ScopeLevel.SYSTEM, allowed_identity_ids=frozenset({identity})
        ),
        "system",
    )
    await service.begin_authentication(tenant, identity, actor=boss)
    await service.record_authentication_result(
        tenant,
        identity,
        AuthenticationResult(
            checked_at=NOW,
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="auth-test",
        ),
        actor=actor,
    )
    await service.start_warmup(tenant, identity, 5, actor=boss)
    reader = module.CurrentSendingIdentityReader(tenant, service, now=lambda: NOW)
    assert (
        await reader.get_sending_identity_eligibility(tenant, identity)
    ).remaining_slots == 5
    assert (
        await reader.get_sending_identity_eligibility(tenant, identity)
    ).remaining_slots == 5
    for n in range(5):
        await service.reserve_send_slot(
            tenant, identity, IdempotencyKey(f"slot-{n}"), True, actor=actor
        )
    fact = await reader.get_sending_identity_eligibility(tenant, identity)
    assert not fact.sendable and fact.remaining_slots == 0


async def test_canonical_bootstrap_builds_real_domains_without_campaign(db_url):
    import importlib

    from apps.api.runtime_config import Phase1RuntimeSettings
    from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
    from tests.integration.test_api_runtime import _runtime_env
    from tests.integration.test_scheduler_worker import (
        _factory_environ,
        _FactoryHealthServer,
        _FactoryResolver,
    )

    module = importlib.import_module("apps.scheduler_worker.bootstrap")
    settings = Phase1RuntimeSettings.from_environ(_runtime_env(str(db_url)))
    tenant = TenantId(new_id("tn"))
    bootstrap = module.CanonicalSchedulerBootstrap(
        settings.scoring_policy, settings.handoff_policy
    )
    async with SchedulerRuntimeFactory(
        _factory_environ(str(db_url), tenant, hunter_enabled=False),
        bootstrap=bootstrap,
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )() as runtime:
        assert runtime.campaign_driver is None
        assert runtime.tenant_id == tenant
        assert runtime.lock_engine.pool.checkedout() == 0


async def test_api_transport_only_composes_current_facts(integration_engine, db_url):
    from apps.api.composition.runtime import build_phase1_dependencies
    from apps.api.runtime_config import Phase1RuntimeSettings
    from infra.secrets import EnvironmentSecretResolver
    from tests.integration.test_api_runtime import _runtime_env
    from tests.unit.test_api_runtime import _ManualTransport

    env = _runtime_env(str(db_url))
    env["TRADEOS_TENANT_ID"] = str(TenantId(new_id("tn")))
    dependencies = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(env),
        async_sessionmaker(integration_engine, expire_on_commit=False),
        now=lambda: NOW,
        secret_resolver=EnvironmentSecretResolver(env),
        gmail_transport=_ManualTransport(),
    )
    from apps.api.composition.runtime import ResolvedManualSendGateway

    assert isinstance(dependencies.tool_gateway, ResolvedManualSendGateway)
    await dependencies.model_lifecycle.aclose()


async def test_account_actor_uses_persistent_user_mapping(integration_engine):
    import importlib
    from functools import partial

    from apps.composition_support.employee_readers import employee_service_scope
    from apps.scheduler_worker.account_discovery import (
        BossAccountDiscoveryActorResolver,
    )
    from domains.employees.permissions import (
        Phase1EmployeeAuthorizer,
        StandardAuditLogger,
    )
    from infra.db.repositories.employees import EmployeeRepositoryImpl
    from shared.schemas.identifiers import EmployeeId, UserId

    tenant, employee, user = (
        TenantId(new_id("tn")),
        EmployeeId(new_id("emp")),
        UserId(new_id("usr")),
    )
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    models = importlib.import_module("domains.employees.models")
    async with sessions() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee_id=employee,
                tenant_id=tenant,
                name="老板",
                role=models.Role.BOSS,
                user_id=user,
                created_at=NOW,
            )
        )
        await session.commit()
    scope = partial(
        employee_service_scope,
        sessions,
        now=lambda: NOW,
        authorizer=Phase1EmployeeAuthorizer(tenant),
        audit=StandardAuditLogger(),
    )
    async with scope(tenant) as service:
        resolver = BossAccountDiscoveryActorResolver(service)
        actors = await resolver.resolve(tenant, user)
        assert actors.employee.actor_id == employee
        with pytest.raises(PermissionDenied):
            await resolver.resolve(tenant, UserId(employee))


async def test_api_capabilities_are_typed_and_do_not_claim_unconfigured_workers():
    import httpx

    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings

    tenant = TenantId(new_id("tn"))
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=1)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/health/capabilities", headers={"X-Tenant-Id": str(tenant)}
        )
    assert response.status_code == 200
    body = response.json()
    assert body and all(set(item) == {"name", "status", "reason"} for item in body)
    assert all(item["status"] == "disabled" for item in body)
    assert {item["name"] for item in body} >= {
        "research",
        "contacts",
        "campaign",
        "reply",
        "sourcing",
        "quotation",
    }


async def test_category_limit_applies_after_exact_account_filter(integration_engine):
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    service = DemandServiceImpl(
        lambda t: SqlAlchemyDemandUnitOfWork(sessions, t, now=lambda: NOW),
        now=lambda: NOW,
    )
    tenant, account, other = (
        TenantId(new_id("tn")),
        ProspectAccountId(new_id("acc")),
        ProspectAccountId(new_id("acc")),
    )
    signal = await service.capture_signal(
        tenant,
        SignalCaptureRequest(
            "product_line_expansion",
            "Acme",
            "new facility",
            NOW,
            "employee_input",
            "manual-1",
            "human",
        ),
    )
    for n in range(201):
        await service.create_hypothesis(
            tenant,
            other,
            f"category-{n:03}",
            [str(signal)],
            "observed demand signal",
            "model-v1",
        )
    await service.create_hypothesis(
        tenant, account, "hinges", [str(signal)], "observed demand signal", "model-v1"
    )
    assert (
        await service.get_outreach_hypothesis_categories(tenant, account)
    ).categories == ("hinges",)
    from shared.errors import ValidationError

    with pytest.raises(ValidationError, match="当前需求假设类别超出读取上限"):
        await service.get_outreach_hypothesis_categories(tenant, other)


async def test_canonical_approval_reader_never_falls_back_between_versions(
    integration_engine,
):
    from apps.composition_support.campaign_approval_reader import (
        CurrentCampaignApprovalReader,
    )
    from domains.approvals.service import ApprovalType, BlastRadius
    from domains.approvals.service_impl import ApprovalServiceImpl
    from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
    from shared.schemas.identifiers import CampaignId, EmployeeId

    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant, campaign = TenantId(new_id("tn")), CampaignId(new_id("cmp"))
    service = ApprovalServiceImpl(
        lambda t: SqlAlchemyApprovalUnitOfWork(sessions, t, now=lambda: NOW),
        now=lambda: NOW,
    )
    reader = CurrentCampaignApprovalReader(tenant, service, now=lambda: NOW)
    assert await reader.get_campaign_approval(tenant, campaign, 1) is None
    approved_by = EmployeeId(new_id("emp"))
    for version, approve in ((1, True), (2, False)):
        approval = await service.submit(
            tenant,
            ApprovalType.CAMPAIGN_BOUNDARY_CHANGE,
            "触达范围",
            {"version": version},
            "核对版本范围",
            BlastRadius(["Campaign"], "允许范围生效", "保持停止", True),
            proposed_by_employee=EmployeeId(new_id("emp")),
            change_set_ref=f"campaign:{campaign}:v{version}",
        )
        await service.decide(
            tenant,
            approval,
            approve,
            approved_by,
            note="范围未满足" if not approve else None,
        )
    assert (
        await reader.get_campaign_approval(tenant, campaign, 1)
    ).approved_by == approved_by
    assert (
        await reader.get_campaign_approval(tenant, campaign, 2)
    ).state.value == "rejected"
    assert await reader.get_campaign_approval(tenant, campaign, 3) is None


async def test_current_api_send_gate_rechecks_contact_reply_and_material_binding(
    integration_engine, db_url
):
    from dataclasses import replace

    from apps.api.composition.runtime import build_phase1_dependencies
    from apps.api.runtime_config import Phase1RuntimeSettings
    from domains.approvals.service import ApprovalType, BlastRadius
    from domains.outreach.errors import (
        ContactNotEligibleError,
        OutreachProviderUnavailableError,
    )
    from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
    from domains.outreach.schemas import (
        CampaignCreateRequest,
        EnrollmentCreateRequest,
        SequenceStepRequest,
        StepIntent,
    )
    from domains.prospecting.schemas import (
        VerificationRecordRequest,
        VerificationStatus,
    )
    from domains.sending_identity.permissions import Actor as SenderActor
    from domains.sending_identity.permissions import ScopeLevel as SenderScope
    from domains.sending_identity.permissions import SendingIdentityScope
    from domains.sending_identity.schemas import (
        AuthenticationResult,
        DomainRole,
        IdentityRegisterRequest,
    )
    from infra.secrets import EnvironmentSecretResolver
    from shared.errors import ValidationError
    from shared.schemas.identifiers import EmployeeId, IdempotencyKey
    from tests.integration.test_api_runtime import _runtime_env
    from tests.unit.test_api_runtime import _ManualTransport

    env = _runtime_env(str(db_url))
    tenant = TenantId(new_id("tn"))
    env["TRADEOS_TENANT_ID"] = str(tenant)
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    deps = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(env),
        sessions,
        now=lambda: NOW,
        secret_resolver=EnvironmentSecretResolver(env),
        gmail_transport=_ManualTransport(),
    )
    try:
        boss_id = EmployeeId(new_id("emp"))
        actor = Actor(str(boss_id), OutreachScope(level=ScopeLevel.TENANT), "boss")
        sender_boss = SenderActor(
            str(boss_id), SendingIdentityScope(level=SenderScope.TENANT), "boss"
        )
        sender = await deps.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                "sender@outreach.test", "outreach.test", DomainRole.COLD_OUTREACH
            ),
            actor=sender_boss,
        )
        await deps.sending_identities.begin_authentication(
            tenant, sender, actor=sender_boss
        )
        sender_system = SenderActor(
            "system:test",
            SendingIdentityScope(
                level=SenderScope.SYSTEM, allowed_identity_ids=frozenset({sender})
            ),
            "system",
        )
        await deps.sending_identities.record_authentication_result(
            tenant,
            sender,
            AuthenticationResult(NOW, True, True, True, (), "auth-test"),
            actor=sender_system,
        )
        await deps.sending_identities.start_warmup(tenant, sender, 5, actor=sender_boss)
        account = await deps.prospecting.resolve_account(
            tenant, AccountResolveRequest("Acme", "DE", entity_type="manufacturer")
        )
        contact = await deps.prospecting.create_contact(
            tenant, ContactCreateRequest(account)
        )
        legal = LegalBasisInput(
            LegalBasisType.LEGITIMATE_INTEREST,
            SubjectType.LEGAL_ENTITY,
            ContactType.PERSONAL_BUSINESS,
            "website",
            NOW,
            assessment_ref="lia-1",
        )
        point = await deps.prospecting.add_contact_point(
            tenant,
            ContactPointCreateRequest(
                contact, ContactPointKind.EMAIL, "buyer@example.test", legal
            ),
        )
        phone = await deps.prospecting.add_contact_point(
            tenant,
            ContactPointCreateRequest(
                contact, ContactPointKind.PHONE, "+493012345678", legal
            ),
        )
        demand = DemandServiceImpl(
            lambda t: SqlAlchemyDemandUnitOfWork(sessions, t, now=lambda: NOW),
            now=lambda: NOW,
        )
        signal = await demand.capture_signal(
            tenant,
            SignalCaptureRequest(
                "product_line_expansion",
                "Acme",
                "new facility",
                NOW,
                "employee_input",
                "manual-1",
                "human",
            ),
        )
        hypothesis = await demand.create_hypothesis(
            tenant,
            account,
            "hinges",
            [str(signal)],
            "possible facility requirement",
            "model-v1",
        )
        campaign = await deps.outreach.create_campaign(
            tenant,
            CampaignCreateRequest(
                "Discovery",
                ("DE",),
                ("manufacturer",),
                ("hinges",),
                (sender,),
                (SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
                5,
                5,
                (),
            ),
            actor=actor,
        )
        await deps.outreach.submit_campaign(tenant, campaign.campaign_id, actor=actor)
        approval = await deps.approvals.submit(
            tenant,
            ApprovalType.CAMPAIGN_BOUNDARY_CHANGE,
            "范围",
            {"version": 1},
            "检查范围",
            BlastRadius(["Campaign"], "启用", "停止", True),
            proposed_by_employee=boss_id,
            change_set_ref=f"campaign:{campaign.campaign_id}:v1",
        )
        await deps.approvals.decide(tenant, approval, True, EmployeeId(new_id("emp")))
        await deps.outreach.activate_campaign(tenant, campaign.campaign_id, actor=actor)
        with pytest.raises(ContactNotEligibleError):
            await deps.outreach.enroll(
                tenant,
                campaign.campaign_id,
                EnrollmentCreateRequest(account, point, IdempotencyKey("unverified")),
                actor=actor,
            )
        await deps.prospecting.record_verification(
            tenant,
            VerificationRecordRequest(
                point,
                VerificationStatus.VERIFIED,
                "controlled",
                NOW,
                "no external cost",
            ),
        )
        with pytest.raises(ValidationError, match="当前联系人事实不可用"):
            await deps.outreach.enroll(
                tenant,
                campaign.campaign_id,
                EnrollmentCreateRequest(account, phone, IdempotencyKey("phone")),
                actor=actor,
            )
        enrollment = await deps.outreach.enroll(
            tenant,
            campaign.campaign_id,
            EnrollmentCreateRequest(account, point, IdempotencyKey("qualified")),
            actor=actor,
        )
        send_actor = Actor(
            "system:controlled-send",
            OutreachScope(
                level=ScopeLevel.SYSTEM,
                allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
            ),
            "system",
        )
        attempt = await deps.outreach.prepare_message_attempt(
            tenant, enrollment.enrollment_id, actor=send_actor
        )
        preflight = await deps.outreach.preflight_message_send(
            tenant, attempt.attempt_id, actor=send_actor
        )
        material = await deps.delivery_materials.resolve(tenant, preflight)
        assert (
            material.recipient_address == "buyer@example.test"
            and material.from_address == "sender@outreach.test"
        )
        assert "buyer@example.test" not in repr(material)
        with pytest.raises(ValidationError):
            await deps.delivery_materials.resolve(
                tenant, replace(preflight, account_id=ProspectAccountId(new_id("acc")))
            )
        message = await deps.conversations.ingest_inbound(
            tenant, None, account, "art_inbound", "<inbound@test.invalid>", NOW
        )
        with pytest.raises(OutreachProviderUnavailableError):
            await deps.outreach.preflight_message_send(
                tenant, attempt.attempt_id, actor=send_actor
            )
        await deps.conversations.record_classification(
            tenant, message, ReplyCategory.AUTO_REPLY, "model-v1"
        )
        assert (
            await deps.outreach.preflight_message_send(
                tenant, attempt.attempt_id, actor=send_actor
            )
            == preflight
        )
        await demand.reject_hypothesis(tenant, hypothesis, "no_need")
        with pytest.raises(ContactNotEligibleError):
            await deps.outreach.preflight_message_send(
                tenant, attempt.attempt_id, actor=send_actor
            )
    finally:
        if deps.model_lifecycle:
            await deps.model_lifecycle.aclose()


class ForbiddenExternalPorts:
    """构造/注册测试禁止任何外部动作；只有这些外部 transport 允许替代。"""

    async def complete_json(self, **kwargs):
        raise AssertionError("组合期不得调用模型")

    async def search(self, **kwargs):
        raise AssertionError("组合期不得搜索")

    async def usage(self, **kwargs):
        raise AssertionError("组合期不得查配额")

    async def send(self, **kwargs):
        raise AssertionError("组合期不得发送")

    async def validate_url(self, url):
        raise AssertionError("组合期不得访问页面")

    async def fetch(self, url):
        raise AssertionError("组合期不得访问页面")

    async def put(self, key, content):
        raise AssertionError("组合期不得写对象")

    async def get(self, key):
        raise AssertionError("组合期不得读对象")

    async def delete(self, key):
        raise AssertionError("组合期不得删对象")

    async def find_contacts(self, *args):
        raise AssertionError("组合期不得发现联系人")

    async def verify(self, *args):
        raise AssertionError("组合期不得验证联系人")


@pytest.mark.parametrize("group", ["campaign", "research", "contacts"])
def test_requested_group_without_ports_is_configuration_error(group):
    from apps.api.runtime_config import Phase1RuntimeSettings
    from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap
    from shared.errors import ValidationError
    from tests.integration.test_api_runtime import _runtime_env

    settings = Phase1RuntimeSettings.from_environ(
        _runtime_env("postgresql+asyncpg://unused/unused")
    )
    with pytest.raises(ValidationError, match="依赖未完整配置"):
        CanonicalSchedulerBootstrap(
            settings.scoring_policy,
            settings.handoff_policy,
            **{f"{group}_enabled": True},
        )


async def test_enabled_groups_share_canonical_services_and_call_no_external_ports(
    db_url, monkeypatch
):
    from apps.api.runtime_config import Phase1RuntimeSettings
    from apps.scheduler_worker import runtime as worker
    from apps.scheduler_worker.bootstrap import (
        CanonicalSchedulerBootstrap,
        ContactRuntimePorts,
        ResearchRuntimePorts,
    )
    from infra.secrets import EnvironmentSecretResolver
    from shared.schemas.identifiers import UserId
    from tests.integration.test_api_runtime import _runtime_env
    from tests.integration.test_scheduler_worker import (
        _factory_environ,
        _FactoryHealthServer,
        _FactoryResolver,
    )

    settings = Phase1RuntimeSettings.from_environ(_runtime_env(str(db_url)))
    tenant = TenantId(new_id("tn"))
    env = _factory_environ(str(db_url), tenant, hunter_enabled=False)
    external = ForbiddenExternalPorts()
    secrets = EnvironmentSecretResolver(env)
    captured = []
    original = CanonicalSchedulerBootstrap.build_base

    def build(self, config, sessions, core, *, now):
        dependencies = original(self, config, sessions, core, now=now)
        captured.append((core, dependencies))
        return dependencies

    monkeypatch.setattr(CanonicalSchedulerBootstrap, "build_base", build)
    bootstrap = CanonicalSchedulerBootstrap(
        settings.scoring_policy,
        settings.handoff_policy,
        campaign_enabled=True,
        gmail_transport=external,
        secret_resolver=secrets,
        research_enabled=True,
        research=ResearchRuntimePorts(
            external,
            "controlled-v1",
            UserId(new_id("usr")),
            external,
            external,
            external,
            10000,
            "CONTROLLED_TAVILY_REF",
            secrets,
            True,
        ),
        contacts_enabled=True,
        contacts=ContactRuntimePorts(
            external, "controlled-v1", ("DE",), external, external
        ),
    )
    async with worker.SchedulerRuntimeFactory(
        env,
        bootstrap=bootstrap,
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )() as runtime:
        assert runtime.campaign_driver is not None
        states = {item.name: item.status for item in runtime.capabilities}
        assert (
            states["campaign"] == states["research"] == states["contacts"] == "enabled"
        )
        assert states["inbound_body"] == states["full_reply"] == "disabled"
    assert len(captured) == 1
    core, dependencies = captured[0]
    assert (
        dependencies.account_discovery.prospecting
        is dependencies.demand_discovery.prospecting
        is core.prospecting
    )
    assert dependencies.demand_discovery.demand is core.demand
    assert (
        dependencies.campaign_messaging.campaign_approvals._approvals is core.approvals
    )
    assert (
        dependencies.campaign_messaging.sending_identity_eligibility._sending
        is core.sending
    )
    assert (
        dependencies.campaign_messaging.reply_status._conversations
        is core.conversations
    )
    assert dependencies.campaign_messaging.delivery_materials._sending is core.sending


async def test_http_account_run_keeps_user_id_for_actual_worker_mapping(
    integration_engine, db_url
):
    import importlib

    import httpx

    from apps.api.composition.runtime import build_phase1_dependencies
    from apps.api.main import create_app
    from apps.api.middleware import ApiSettings
    from apps.api.runtime_config import Phase1RuntimeSettings
    from apps.scheduler_worker.account_discovery import (
        BossAccountDiscoveryActorResolver,
    )
    from apps.scheduler_worker.bootstrap import CurrentNotificationAudience
    from infra.db.repositories.employees import EmployeeRepositoryImpl
    from infra.secrets import EnvironmentSecretResolver
    from shared.events.catalog import HandoffQueueBacklogged
    from shared.schemas.identifiers import EmployeeId, RunId, UserId
    from tests.integration.test_api_runtime import _runtime_env

    tenant, employee, user = (
        TenantId(new_id("tn")),
        EmployeeId(new_id("emp")),
        UserId(new_id("usr")),
    )
    env = _runtime_env(str(db_url))
    env["TRADEOS_TENANT_ID"] = str(tenant)
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    models = importlib.import_module("domains.employees.models")
    async with sessions() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee, tenant, "老板", models.Role.BOSS, NOW, user_id=user
            )
        )
        await session.commit()
    deps = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(env),
        sessions,
        now=lambda: NOW,
        secret_resolver=EnvironmentSecretResolver(env),
    )
    try:
        app = create_app(
            settings=ApiSettings(
                tenant_id=tenant, dev_mode=True, retry_after_seconds=1
            ),
            dependencies=deps,
        )
        headers = {"X-Tenant-Id": str(tenant), "X-Employee-Id": str(employee)}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/prospects/discoveries",
                headers=headers,
                json={
                    "hypothesis_id": new_id("hyp"),
                    "campaign_id": new_id("cmp"),
                    "role_hints": ["procurement"],
                    "assessment_ref": "lia-test",
                },
            )
        assert response.status_code == 200, response.text
        run = await deps.workflow_engine.get_run(
            tenant, RunId(response.json()["run_id"])
        )
        assert run.context["acting_user_id"] == str(user)
        assert run.context["acting_user_id"] != str(employee)
        async with deps.employees(tenant) as service:
            actors = await BossAccountDiscoveryActorResolver(service).resolve(
                tenant, UserId(run.context["acting_user_id"])
            )
        assert actors.employee.actor_id == str(employee)
        from dataclasses import asdict

        from apps.composition_support.employee_readers import CurrentEmployeeUserReader
        from apps.scheduler_worker.directive_reader import (
            DirectiveDemandDiscoveryTaskReader,
        )
        from domains.directives.schemas import (
            DemandDiscoveryPlanInput,
            DiscoverySearchQueryInput,
        )
        from tests.unit.workflows.test_demand_discovery import _plan

        plan = _plan()
        dto = DemandDiscoveryPlanInput(
            **{
                **asdict(plan),
                "queries": tuple(
                    DiscoverySearchQueryInput(**asdict(query)) for query in plan.queries
                ),
            }
        )
        proposal = await deps.directives.submit_discovery_proposal(
            tenant, "test", dto, "test", ["受控确认"], "test"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            confirmation = await client.post(
                f"/commands/discovery-proposals/{proposal}/confirm", headers=headers
            )
        assert confirmation.status_code == 200, confirmation.text
        demand_run = await deps.workflow_engine.get_run(
            tenant, RunId(confirmation.json()["run_id"])
        )
        assert demand_run.context["acting_user_id"] == str(user)
        confirmed = await deps.directives.get_proposal(tenant, proposal)
        assert confirmed.decided_by_id == str(employee)
        reader = DirectiveDemandDiscoveryTaskReader(
            deps.directives,
            CurrentEmployeeUserReader(deps.employees, deps.employee_lookup_actor),
        )
        assert (
            await reader.load_confirmed(
                tenant, proposal, UserId(demand_run.context["acting_user_id"])
            )
            == plan
        )
        audience = CurrentNotificationAudience(
            tenant, deps.employees, deps.opportunities
        )
        event = HandoffQueueBacklogged(tenant_id=tenant, occurred_at=NOW)
        assert [
            item.employee_id for item in await audience.recipients_for(tenant, event)
        ] == [employee]
        from shared.errors import ValidationError

        with pytest.raises(ValidationError):
            await audience.recipients_for(TenantId(new_id("tn")), event)
    finally:
        if deps.model_lifecycle:
            await deps.model_lifecycle.aclose()
