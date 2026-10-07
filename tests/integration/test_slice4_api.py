"""Task 6 内部 API 的真实 runtime 集成：enrollments / sending identities / inbox。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from apps.api.runtime_config import Phase1RuntimeSettings
from domains.outreach.permissions import (
    Actor as OutreachActor,
)
from domains.outreach.permissions import (
    OutreachScope,
)
from domains.outreach.permissions import (
    ScopeLevel as OutreachScopeLevel,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
)
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.permissions import (
    SendingIdentityScope,
)
from domains.sending_identity.schemas import (
    AuthenticationResult,
    IdentityRegisterRequest,
)
from infra.db.session import create_engine_from
from infra.db.tables import InAppNotificationRow, NotificationJobRow
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    NotificationId,
    NotificationJobId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tests.outreach_fakes import (
    FakeApprovals,
    FakeContacts,
    FakeReplies,
    FakeSenders,
    Trace,
)

_NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


class _Materials:
    async def resolve(self, tenant_id: TenantId, preflight: object) -> object:
        from tool_gateway.handlers.email_send import DeliveryMaterial

        return DeliveryMaterial(
            tenant_id=tenant_id,
            attempt_id=preflight.attempt_id,
            account_id=preflight.account_id,
            contact_point_id=preflight.contact_point_id,
            sending_identity_id=preflight.sending_identity_id,
            from_address="sender-marker@example.test",
            recipient_address="recipient-marker@example.test",
        )


class _Transport:
    async def search(self, **kwargs: object) -> None:
        del kwargs

    async def send(self, **kwargs: object) -> str:
        del kwargs
        return "gmail_ref_slice4"


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "fingerprint-ref": "f" * 32,
            "gmail-ref": "g" * 32,
            "UNSUBSCRIBE_HMAC_REF": "u" * 32,
        }
        return values[secret_ref]


def _settings(tenant: TenantId) -> Phase1RuntimeSettings:
    return Phase1RuntimeSettings.from_environ(
        {
            "DATABASE_URL": "postgresql+asyncpg://db.invalid/tradeos",
            "TRADEOS_TENANT_ID": str(tenant),
            "TRADEOS_DEV_MODE": "true",
            "TRADEOS_CORS_ALLOWED_ORIGINS": '["http://127.0.0.1:4173"]',
            "TRADEOS_API_RETRY_AFTER_SECONDS": "30",
            "TRADEOS_HANDOFF_POLICY": (
                '{"sla_seconds":300,"backlog_threshold":20,'
                '"t1_seconds":120,"t2_seconds":180}'
            ),
            "TRADEOS_SCORING_POLICY": (
                '{"version":"phase1-v1","currency":"USD",'
                '"value_band_boundaries":["1000","5000"],'
                '"bucket_map":{"1":"low","2":"low","3":"mid",'
                '"4":"mid","5":"high","6":"high","7":"high"}}'
            ),
            "TRADEOS_OUTBOX_MAX_ATTEMPTS": "3",
            "GMAIL_OAUTH_TOKEN_REF": "gmail-ref",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "fingerprint-ref",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
                '{"2026-v1":"UNSUBSCRIBE_HMAC_REF"}'
            ),
            "TRADEOS_TOOL_LEASE_SECONDS": "30",
        }
    )


async def _seed_employee(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    employee_id: EmployeeId,
    role: str,
) -> None:
    models = importlib.import_module("domains.employees.models")
    from infra.db.repositories.employees import EmployeeRepositoryImpl

    async with factory() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee_id=employee_id,
                tenant_id=tenant,
                user_id=UserId(new_id("usr")),
                name="Slice4",
                role=models.Role(role),
                created_at=_NOW,
            )
        )
        await session.commit()


async def _seed_identity(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    boss: EmployeeId,
) -> SendingIdentityId:
    dependencies = build_phase1_dependencies(
        _settings(tenant),
        factory,
        now=lambda: _NOW,
        secret_resolver=_Secrets(),
    )
    boss_identity = SendingIdentityActor(
        str(boss),
        SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
        "boss",
    )
    identity_id = await dependencies.sending_identities.register(
        tenant,
        IdentityRegisterRequest(
            address="sender-marker@example.test",
            domain="example.test",
            role=importlib.import_module(
                "domains.sending_identity.models"
            ).DomainRole.COLD_OUTREACH,
        ),
        actor=boss_identity,
    )
    system_identity = SendingIdentityActor(
        "system:slice4-test",
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )
    await dependencies.sending_identities.begin_authentication(
        tenant, identity_id, actor=boss_identity
    )
    await dependencies.sending_identities.record_authentication_result(
        tenant,
        identity_id,
        AuthenticationResult(
            checked_at=_NOW,
            spf_passed=True,
            dkim_passed=True,
            dmarc_passed=True,
            failures=(),
            check_ref="auth_slice4",
        ),
        actor=system_identity,
    )
    await dependencies.sending_identities.start_warmup(
        tenant, identity_id, 5, actor=boss_identity
    )
    return identity_id


async def _seed_campaign(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign_id: CampaignId,
    identity_id: SendingIdentityId,
    created_by: EmployeeId,
    approval_id: ApprovalId,
) -> None:
    models = importlib.import_module("domains.outreach.models")
    boundary = models.CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(identity_id,),
        steps=(models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0),),
        daily_new_contact_limit=5,
        daily_total_message_limit=10,
        handoff_triggers=(),
    )
    campaign = models.Campaign(
        tenant,
        campaign_id,
        models.CampaignState.ACTIVE,
        1,
        created_by,
        _NOW,
        approval_id=str(approval_id),
        approved_by=created_by,
        approved_at=_NOW,
    )
    version = models.CampaignVersion(
        tenant,
        campaign_id,
        1,
        "Slice4 integration",
        boundary,
        created_by,
        _NOW,
    )
    from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork

    async with SqlAlchemyOutreachUnitOfWork(
        factory, tenant, now=lambda: _NOW
    ) as uow:
        await uow.campaigns.add(campaign, version)


def _boss_actor(tenant: TenantId, boss: EmployeeId) -> OutreachActor:
    return OutreachActor(
        str(boss),
        OutreachScope(level=OutreachScopeLevel.TENANT),
        "boss",
    )


class _Composition:
    def __init__(
        self,
        dependencies: object,
        app: object,
        approvals: FakeApprovals,
        contact_snapshots: dict,
        reply_snapshots: dict,
        senders: FakeSenders,
        tenant: TenantId,
    ) -> None:
        self.dependencies = dependencies
        self.app = app
        self.approvals = approvals
        self.contact_snapshots = contact_snapshots
        self.reply_snapshots = reply_snapshots
        self.senders = senders
        self.tenant = tenant

    async def enroll(
        self,
        campaign_id: CampaignId,
        approval_id: ApprovalId,
        created_by: EmployeeId,
        suffix: str,
    ) -> EnrollmentId:
        self.approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
            self.tenant,
            campaign_id,
            1,
            approval_id,
            CampaignApprovalState.APPROVED,
            created_by,
            _NOW,
        )
        account = ProspectAccountId(new_id("acc"))
        contact = ContactPointId(new_id("cp"))
        self.contact_snapshots[(contact, account)] = ContactEligibilitySnapshot(
            self.tenant,
            contact,
            account,
            ContactVerificationStatus.VERIFIED,
            _NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST,
            f"basis_slice4_{suffix}",
            True,
            "US",
            "importer",
            frozenset({"hardware"}),
            _NOW,
        )
        self.reply_snapshots[(contact, account)] = ReplyStatusSnapshot(
            self.tenant,
            contact,
            account,
            ReplyState.NO_REPLY,
            None,
            _NOW,
        )
        enrollment = await self.dependencies.outreach.enroll(
            self.tenant,
            campaign_id,
            EnrollmentCreateRequest(
                account, contact, IdempotencyKey(f"slice4-{suffix}")
            ),
            actor=_boss_actor(self.tenant, created_by),
        )
        return enrollment.enrollment_id


async def _build(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    boss: EmployeeId,
    identity_id: SendingIdentityId,
) -> _Composition:
    trace = Trace()
    approvals = FakeApprovals(trace)
    contact_snapshots: dict = {}
    reply_snapshots: dict = {}
    senders = FakeSenders(
        {
            identity_id: SendingIdentityEligibilitySnapshot(
                tenant,
                identity_id,
                OutreachSenderRole.COLD_OUTREACH,
                True,
                True,
                5,
                _NOW,
            )
        },
        trace,
    )
    dependencies = build_phase1_dependencies(
        _settings(tenant),
        factory,
        now=lambda: _NOW,
        manual_send=ManualSendComposition(
            contact_eligibility=FakeContacts(contact_snapshots, trace),
            sending_identity_eligibility=senders,
            campaign_approvals=approvals,
            reply_status=FakeReplies(reply_snapshots, trace),
            delivery_materials=_Materials(),
            secret_resolver=_Secrets(),
            gmail_transport=_Transport(),
        ),
    )
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(tenant), dev_mode=True, retry_after_seconds=30
        ),
        dependencies=dependencies,
    )
    return _Composition(
        dependencies, app, approvals, contact_snapshots, reply_snapshots, senders, tenant
    )


def _headers(tenant: TenantId, employee_id: EmployeeId) -> dict[str, str]:
    return {"X-Tenant-Id": str(tenant), "X-Employee-Id": str(employee_id)}


async def _request(
    method: str, app: object, path: str, headers: dict[str, str], json: dict | None = None
):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, json=json)


async def test_slice4_enrollments_visibility_and_prepare(db_url: str) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    boss = EmployeeId(new_id("emp"))
    sales = EmployeeId(new_id("emp"))
    other = EmployeeId(new_id("emp"))
    campaign_a = CampaignId(new_id("cmp"))
    campaign_b = CampaignId(new_id("cmp"))
    approval_a = ApprovalId(new_id("apr"))
    approval_b = ApprovalId(new_id("apr"))
    try:
        await _seed_employee(factory, tenant, boss, "boss")
        await _seed_employee(factory, tenant, sales, "sales")
        await _seed_employee(factory, tenant, other, "sales")
        identity_id = await _seed_identity(factory, tenant, boss)
        await _seed_campaign(factory, tenant, campaign_a, identity_id, sales, approval_a)
        await _seed_campaign(factory, tenant, campaign_b, identity_id, other, approval_b)
        composition = await _build(factory, tenant, boss, identity_id)
        enrollment_a = await composition.enroll(campaign_a, approval_a, sales, "a")
        enrollment_b = await composition.enroll(campaign_b, approval_b, other, "b")

        boss_list = await _request(
            "GET", composition.app, "/crm/enrollments?limit=10",
            _headers(tenant, boss),
        )
        assert boss_list.status_code == 200
        assert {row["enrollment_id"] for row in boss_list.json()} == {
            str(enrollment_a),
            str(enrollment_b),
        }

        sales_list = await _request(
            "GET", composition.app, "/crm/enrollments?limit=10",
            _headers(tenant, sales),
        )
        assert sales_list.status_code == 200
        assert [row["enrollment_id"] for row in sales_list.json()] == [
            str(enrollment_a)
        ]

        prepared = await _request(
            "POST",
            composition.app,
            f"/crm/enrollments/{enrollment_a}/attempts/prepare",
            _headers(tenant, boss),
        )
        assert prepared.status_code == 200
        assert prepared.json()["enrollment_id"] == str(enrollment_a)
        assert prepared.json()["state"] == "reserved"

        not_owned = await _request(
            "POST",
            composition.app,
            f"/crm/enrollments/{enrollment_b}/attempts/prepare",
            _headers(tenant, sales),
        )
        assert not_owned.status_code == 403
        assert not_owned.json() == {
            "code": "forbidden",
            "message": "没有权限",
        }
    finally:
        await engine.dispose()


async def test_slice4_sending_identity_endpoints_are_boss_only(db_url: str) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    boss = EmployeeId(new_id("emp"))
    sales = EmployeeId(new_id("emp"))
    try:
        await _seed_employee(factory, tenant, boss, "boss")
        await _seed_employee(factory, tenant, sales, "sales")
        identity_id = await _seed_identity(factory, tenant, boss)
        composition = await _build(factory, tenant, boss, identity_id)

        listed = await _request(
            "GET", composition.app, "/crm/sending-identities?limit=10",
            _headers(tenant, boss),
        )
        assert listed.status_code == 200
        assert [row["identity_id"] for row in listed.json()] == [str(identity_id)]

        single = await _request(
            "GET", composition.app, f"/crm/sending-identities/{identity_id}",
            _headers(tenant, boss),
        )
        assert single.status_code == 200
        assert single.json()["identity_id"] == str(identity_id)

        checked = await _request(
            "POST",
            composition.app,
            f"/crm/sending-identities/{identity_id}/authentication-checks",
            _headers(tenant, boss),
            json={"request_key": "slice4-auth-check"},
        )
        assert checked.status_code == 200
        assert checked.json()["request_key"] == "slice4-auth-check"

        denied = await _request(
            "GET", composition.app, "/crm/sending-identities?limit=10",
            _headers(tenant, sales),
        )
        assert denied.status_code == 403
        assert denied.json() == {"code": "forbidden", "message": "没有权限"}
    finally:
        await engine.dispose()


async def test_slice4_notification_inbox_is_employee_scoped(db_url: str) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    boss = EmployeeId(new_id("emp"))
    sales = EmployeeId(new_id("emp"))
    notification_id = NotificationId(new_id("ntf"))
    job_id = NotificationJobId(new_id("njb"))
    try:
        await _seed_employee(factory, tenant, boss, "boss")
        await _seed_employee(factory, tenant, sales, "sales")
        identity_id = await _seed_identity(factory, tenant, boss)
        async with factory() as session:
            session.add(
                NotificationJobRow(
                    tenant_id=str(tenant),
                    notification_job_id=str(job_id),
                    source_event_fingerprint="f" * 64,
                    source_event="HandoffRequested",
                    recipient_employee_id=str(boss),
                    priority="urgent",
                    context_kind="handoff_escalation",
                    primary_id=str(new_id("hnd")),
                    secondary_id=None,
                    reason_code="t1",
                    level=None,
                    dedup_key="slice4-notification-dedup",
                    status="completed",
                    available_at=_NOW,
                    created_at=_NOW,
                )
            )
            await session.flush()
            session.add(
                InAppNotificationRow(
                    tenant_id=str(tenant),
                    notification_id=str(notification_id),
                    recipient_employee_id=str(boss),
                    priority="urgent",
                    title="Slice4 通知",
                    context_kind="handoff_escalation",
                    primary_id=str(new_id("hnd")),
                    secondary_id=None,
                    reason_code="t1",
                    level=None,
                    relative_link="/crm/handoffs/handoff-1",
                    source_job_id=str(job_id),
                    created_at=_NOW,
                    read_at=None,
                )
            )
            await session.commit()
        composition = await _build(factory, tenant, boss, identity_id)

        listed = await _request(
            "GET", composition.app, "/notifications?limit=10",
            _headers(tenant, boss),
        )
        assert listed.status_code == 200
        assert [row["notification_id"] for row in listed.json()] == [
            str(notification_id)
        ]
        assert listed.json()[0]["read_at"] is None

        sales_list = await _request(
            "GET", composition.app, "/notifications?limit=10",
            _headers(tenant, sales),
        )
        assert sales_list.status_code == 200
        assert sales_list.json() == []

        read = await _request(
            "POST", composition.app, f"/notifications/{notification_id}/read",
            _headers(tenant, boss),
        )
        assert read.status_code == 200
        assert read.json()["read_at"] is not None

        cross_recipient = await _request(
            "POST", composition.app, f"/notifications/{notification_id}/read",
            _headers(tenant, sales),
        )
        assert cross_recipient.status_code == 400
        async with factory() as session:
            row = await session.get(
                InAppNotificationRow,
                (str(tenant), str(notification_id)),
            )
            assert row.read_at is not None
    finally:
        await engine.dispose()
