"""真实 PostgreSQL、领域服务、Tool Gateway 与 ASGI 的单封发送闭环。"""

from __future__ import annotations

import asyncio
import importlib
from collections import Counter
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
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
    MessageAttemptView,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SuppressionReason,
    SuppressionRequest,
    SuppressionTarget,
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
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.repositories.employees import EmployeeRepositoryImpl
from infra.db.session import create_engine_from
from infra.db.tables import (
    OutreachEnrollmentRow,
    OutreachMessageAttemptRow,
    SendReservationRow,
    ToolCallEventRow,
    ToolCallRow,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
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
from tool_gateway.handlers.email_send import DeliveryMaterial

_NOW = datetime(2026, 8, 13, 4, 0, tzinfo=UTC)


class _Secrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "fingerprint-ref": "f" * 32,
            "gmail-ref": "g" * 32,
            "UNSUBSCRIBE_HMAC_REF": "u" * 32,
        }
        return values[secret_ref]


class _Transport:
    def __init__(self) -> None:
        self.search_calls = 0
        self.send_calls = 0
        self.block_next_send = False
        self.send_entered = asyncio.Event()
        self.release_send = asyncio.Event()

    async def search(self, **kwargs: object) -> None:
        del kwargs
        self.search_calls += 1

    async def send(self, **kwargs: object) -> str:
        del kwargs
        self.send_calls += 1
        if self.block_next_send:
            self.block_next_send = False
            self.send_entered.set()
            await self.release_send.wait()
        return "gmail_ref_manual_1"


class _Materials:
    def __init__(self) -> None:
        self.fail_next = False

    async def resolve(self, tenant_id: TenantId, preflight: object) -> DeliveryMaterial:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("material-runtime-marker")
        return DeliveryMaterial(
            tenant_id=tenant_id,
            attempt_id=preflight.attempt_id,
            account_id=preflight.account_id,
            contact_point_id=preflight.contact_point_id,
            sending_identity_id=preflight.sending_identity_id,
            from_address="sender-marker@example.test",
            recipient_address="recipient-marker@example.test",
        )


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


async def _seed_campaign(
    factory: async_sessionmaker,
    tenant: TenantId,
    campaign_id: CampaignId,
    identity_id: SendingIdentityId,
    boss: EmployeeId,
    approval_id: ApprovalId,
    *,
    new_contact_limit: int = 5,
    total_message_limit: int = 10,
) -> None:
    models = importlib.import_module("domains.outreach.models")
    boundary = models.CampaignBoundary(
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(identity_id,),
        steps=(models.SequenceStepSpec(1, models.StepIntent.DISCOVERY, 0),),
        daily_new_contact_limit=new_contact_limit,
        daily_total_message_limit=total_message_limit,
        handoff_triggers=(),
    )
    campaign = models.Campaign(
        tenant,
        campaign_id,
        models.CampaignState.ACTIVE,
        1,
        boss,
        _NOW,
        approval_id=str(approval_id),
        approved_by=boss,
        approved_at=_NOW,
    )
    version = models.CampaignVersion(
        tenant,
        campaign_id,
        1,
        "Manual send integration",
        boundary,
        boss,
        _NOW,
    )
    async with SqlAlchemyOutreachUnitOfWork(
        factory, tenant, now=lambda: _NOW
    ) as uow:
        await uow.campaigns.add(campaign, version)


async def _seed_employee(
    factory: async_sessionmaker,
    tenant: TenantId,
    boss: EmployeeId,
) -> None:
    models = importlib.import_module("domains.employees.models")
    async with factory() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee_id=boss,
                tenant_id=tenant,
                user_id=UserId(new_id("usr")),
                name="Boss",
                role=models.Role.BOSS,
                created_at=_NOW,
            )
        )
        await session.commit()


async def _reservation_count(
    factory: async_sessionmaker,
    tenant: TenantId,
) -> int:
    async with factory() as session:
        value = await session.scalar(
            select(func.count())
            .select_from(SendReservationRow)
            .where(SendReservationRow.tenant_id == tenant)
        )
    return int(value or 0)


async def test_manual_send_is_atomic_idempotent_and_never_persists_raw_material(
    db_url: str,
) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    boss = EmployeeId(new_id("emp"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    trace = Trace()
    approvals = FakeApprovals(trace)
    transport = _Transport()
    try:
        await _seed_employee(factory, tenant, boss)
        dependencies_without_send = build_phase1_dependencies(
            _settings(tenant),
            factory,
            now=lambda: _NOW,
            secret_resolver=_Secrets(),
        )
        identity_id = await dependencies_without_send.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address="sender-marker@example.test",
                domain="example.test",
                role=importlib.import_module(
                    "domains.sending_identity.models"
                ).DomainRole.COLD_OUTREACH,
            ),
            actor=SendingIdentityActor(
                str(boss),
                SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
                "boss",
            ),
        )
        system_identity = SendingIdentityActor(
            "system:manual-send-test",
            SendingIdentityScope(
                level=SendingIdentityScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({identity_id}),
            ),
            "system",
        )
        boss_identity = SendingIdentityActor(
            str(boss),
            SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
            "boss",
        )
        await dependencies_without_send.sending_identities.begin_authentication(
            tenant, identity_id, actor=boss_identity
        )
        await dependencies_without_send.sending_identities.record_authentication_result(
            tenant,
            identity_id,
            AuthenticationResult(
                checked_at=_NOW,
                spf_passed=True,
                dkim_passed=True,
                dmarc_passed=True,
                failures=(),
                check_ref="auth_manual_1",
            ),
            actor=system_identity,
        )
        await dependencies_without_send.sending_identities.start_warmup(
            tenant, identity_id, 5, actor=boss_identity
        )
        await _seed_campaign(
            factory, tenant, campaign_id, identity_id, boss, approval_id
        )

        contact_snapshots = {
            (contact, account): ContactEligibilitySnapshot(
                tenant,
                contact,
                account,
                ContactVerificationStatus.VERIFIED,
                _NOW,
                ContactLegalBasis.LEGITIMATE_INTEREST,
                "basis_manual_1",
                True,
                "US",
                "importer",
                frozenset({"hardware"}),
                _NOW,
            )
        }
        contacts = FakeContacts(contact_snapshots, trace)
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
        reply_snapshots = {
            (contact, account): ReplyStatusSnapshot(
                tenant, contact, account, ReplyState.NO_REPLY, None, _NOW
            )
        }
        replies = FakeReplies(reply_snapshots, trace)
        approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
            tenant,
            campaign_id,
            1,
            approval_id,
            CampaignApprovalState.APPROVED,
            boss,
            _NOW,
        )
        materials = _Materials()
        dependencies = build_phase1_dependencies(
            _settings(tenant),
            factory,
            now=lambda: _NOW,
            manual_send=ManualSendComposition(
                contact_eligibility=contacts,
                sending_identity_eligibility=senders,
                campaign_approvals=approvals,
                reply_status=replies,
                delivery_materials=materials,
                secret_resolver=_Secrets(),
                gmail_transport=transport,
            ),
        )
        enrollment = await dependencies.outreach.enroll(
            tenant,
            campaign_id,
            EnrollmentCreateRequest(
                account, contact, IdempotencyKey("manual-enrollment-1")
            ),
            actor=OutreachActor(
                str(boss),
                OutreachScope(level=OutreachScopeLevel.TENANT),
                "boss",
            ),
        )
        attempt = await dependencies.outreach.prepare_message_attempt(
            tenant,
            EnrollmentId(str(enrollment.enrollment_id)),
            actor=OutreachActor(
                "system:manual-send-test",
                OutreachScope(
                    level=OutreachScopeLevel.SYSTEM,
                    allowed_enrollment_ids=frozenset(
                        {EnrollmentId(str(enrollment.enrollment_id))}
                    ),
                ),
                "system",
            ),
        )
        app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant), dev_mode=True, retry_after_seconds=30
            ),
            dependencies=dependencies,
        )
        headers = {
            "X-Tenant-Id": str(tenant),
            "X-Employee-Id": str(boss),
        }
        subject = "subject-marker-must-not-persist"
        body = "body-marker-must-not-persist"

        async def prepare_attempt(
            suffix: str,
        ) -> tuple[MessageAttemptView, ContactPointId]:
            next_account = ProspectAccountId(new_id("acc"))
            next_contact = ContactPointId(new_id("cp"))
            contact_snapshots[(next_contact, next_account)] = (
                ContactEligibilitySnapshot(
                    tenant,
                    next_contact,
                    next_account,
                    ContactVerificationStatus.VERIFIED,
                    _NOW,
                    ContactLegalBasis.LEGITIMATE_INTEREST,
                    f"basis_manual_{suffix}",
                    True,
                    "US",
                    "importer",
                    frozenset({"hardware"}),
                    _NOW,
                )
            )
            reply_snapshots[(next_contact, next_account)] = ReplyStatusSnapshot(
                tenant,
                next_contact,
                next_account,
                ReplyState.NO_REPLY,
                None,
                _NOW,
            )
            next_enrollment = await dependencies.outreach.enroll(
                tenant,
                campaign_id,
                EnrollmentCreateRequest(
                    next_account,
                    next_contact,
                    IdempotencyKey(f"manual-enrollment-{suffix}"),
                ),
                actor=OutreachActor(
                    str(boss),
                    OutreachScope(level=OutreachScopeLevel.TENANT),
                    "boss",
                ),
            )
            return (
                await dependencies.outreach.prepare_message_attempt(
                    tenant,
                    EnrollmentId(str(next_enrollment.enrollment_id)),
                    actor=OutreachActor(
                        "system:manual-send-test",
                        OutreachScope(
                            level=OutreachScopeLevel.SYSTEM,
                            allowed_enrollment_ids=frozenset(
                                {EnrollmentId(str(next_enrollment.enrollment_id))}
                            ),
                        ),
                        "system",
                    ),
                ),
                next_contact,
            )

        suppressed_attempt, suppressed_contact = await prepare_attempt(
            "suppression-first"
        )
        await dependencies.outreach.add_suppression(
            tenant,
            SuppressionRequest(
                SuppressionTarget(contact_point_id=suppressed_contact),
                SuppressionReason.MANUAL_BLOCK,
                _NOW,
                "manual_suppression_first",
                IdempotencyKey("manual-suppression-first"),
            ),
            actor=OutreachActor(
                str(boss),
                OutreachScope(level=OutreachScopeLevel.TENANT),
                "boss",
            ),
        )
        race_attempt, race_contact = await prepare_attempt("claim-first")
        commit_failure_attempt, _commit_failure_contact = await prepare_attempt(
            "completion-failure"
        )
        unexpected_attempt, _unexpected_contact = await prepare_attempt(
            "unexpected-provider"
        )
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            unknown = await client.post(
                f"/crm/message-attempts/{new_id('mat')}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )
            wrong_tenant = await client.post(
                f"/crm/message-attempts/{attempt.attempt_id}/send",
                headers={**headers, "X-Tenant-Id": str(TenantId(new_id("tn")))},
                json={"subject": subject, "body": body},
            )
            forbidden = await client.post(
                f"/crm/message-attempts/{attempt.attempt_id}/send",
                headers=headers,
                json={"subject": "Price USD 99", "body": body},
            )
            materials.fail_next = True
            sends_before_unexpected = transport.send_calls
            reservations_before_unexpected = await _reservation_count(
                factory, tenant
            )
            unexpected = await client.post(
                f"/crm/message-attempts/{unexpected_attempt.attempt_id}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )
            assert transport.send_calls == sends_before_unexpected
            assert await _reservation_count(factory, tenant) == (
                reservations_before_unexpected
            )
            reservations_before_suppressed = await _reservation_count(
                factory, tenant
            )
            sends_before_suppressed = transport.send_calls
            suppressed = await client.post(
                f"/crm/message-attempts/{suppressed_attempt.attempt_id}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )
            assert await _reservation_count(factory, tenant) == (
                reservations_before_suppressed
            )
            assert transport.send_calls == sends_before_suppressed
            responses = await asyncio.gather(
                *(
                    client.post(
                        f"/crm/message-attempts/{attempt.attempt_id}/send",
                        headers=headers,
                        json={"subject": subject, "body": body},
                    )
                    for _ in range(20)
                )
            )
            transport.block_next_send = True
            race_request = asyncio.create_task(
                client.post(
                    f"/crm/message-attempts/{race_attempt.attempt_id}/send",
                    headers=headers,
                    json={"subject": subject, "body": body},
                )
            )
            await asyncio.wait_for(transport.send_entered.wait(), timeout=3)
            await dependencies.outreach.add_suppression(
                tenant,
                SuppressionRequest(
                    SuppressionTarget(contact_point_id=race_contact),
                    SuppressionReason.MANUAL_BLOCK,
                    _NOW,
                    "manual_claim_first",
                    IdempotencyKey("manual-claim-first"),
                ),
                actor=OutreachActor(
                    str(boss),
                    OutreachScope(level=OutreachScopeLevel.TENANT),
                    "boss",
                ),
            )
            transport.release_send.set()
            raced = await race_request
            sends_after_race = transport.send_calls
            reservations_after_race = await _reservation_count(factory, tenant)
            repeated_race = await client.post(
                f"/crm/message-attempts/{race_attempt.attempt_id}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )
            assert transport.send_calls == sends_after_race
            assert await _reservation_count(factory, tenant) == reservations_after_race

        fail_sent_commit = True

        class SentCommitFailsOnce(AsyncSession):
            async def commit(self) -> None:
                nonlocal fail_sent_commit
                current_state = await self.scalar(
                    select(OutreachMessageAttemptRow.state).where(
                        OutreachMessageAttemptRow.tenant_id == tenant,
                        OutreachMessageAttemptRow.attempt_id
                        == commit_failure_attempt.attempt_id,
                    )
                )
                if fail_sent_commit and current_state == "sent":
                    fail_sent_commit = False
                    raise RuntimeError("local completion commit failed")
                await super().commit()

        failing_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=SentCommitFailsOnce,
        )
        failing_dependencies = build_phase1_dependencies(
            _settings(tenant),
            failing_factory,
            now=lambda: _NOW,
            manual_send=ManualSendComposition(
                contact_eligibility=contacts,
                sending_identity_eligibility=senders,
                campaign_approvals=approvals,
                reply_status=replies,
                delivery_materials=materials,
                secret_resolver=_Secrets(),
                gmail_transport=transport,
            ),
        )
        failing_app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant), dev_mode=True, retry_after_seconds=30
            ),
            dependencies=failing_dependencies,
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=failing_app,
                raise_app_exceptions=False,
            ),
            base_url="http://test",
        ) as client:
            commit_failed = await client.post(
                f"/crm/message-attempts/{commit_failure_attempt.attempt_id}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )
            sends_after_commit_failure = transport.send_calls
            commit_failure_retry = await client.post(
                f"/crm/message-attempts/{commit_failure_attempt.attempt_id}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )

        tenant_b = TenantId(new_id("tn"))
        boss_b = EmployeeId(new_id("emp"))
        await _seed_employee(factory, tenant_b, boss_b)
        dependencies_b = build_phase1_dependencies(
            _settings(tenant_b),
            factory,
            now=lambda: _NOW,
            manual_send=ManualSendComposition(
                contact_eligibility=contacts,
                sending_identity_eligibility=senders,
                campaign_approvals=approvals,
                reply_status=replies,
                delivery_materials=materials,
                secret_resolver=_Secrets(),
                gmail_transport=transport,
            ),
        )
        tenant_b_app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant_b), dev_mode=True, retry_after_seconds=30
            ),
            dependencies=dependencies_b,
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=tenant_b_app,
                raise_app_exceptions=False,
            ),
            base_url="http://test",
        ) as client:
            tenant_b_response = await client.post(
                f"/crm/message-attempts/{attempt.attempt_id}/send",
                headers={
                    "X-Tenant-Id": str(tenant_b),
                    "X-Employee-Id": str(boss_b),
                },
                json={"subject": subject, "body": body},
            )

        unavailable_app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant), dev_mode=True, retry_after_seconds=30
            ),
            dependencies=dependencies_without_send,
        )
        async with AsyncClient(
            transport=ASGITransport(
                app=unavailable_app, raise_app_exceptions=False
            ),
            base_url="http://test",
        ) as client:
            unavailable = await client.post(
                f"/crm/message-attempts/{attempt.attempt_id}/send",
                headers=headers,
                json={"subject": subject, "body": body},
            )

        assert unknown.status_code == 400
        assert unknown.json() == {
            "code": "validation_error",
            "message": "发送请求无效",
        }
        assert wrong_tenant.status_code == 403
        assert suppressed.status_code == 403, suppressed.text
        assert suppressed.json() == {
            "code": "suppressed",
            "message": "当前事实不允许发送",
        }
        assert forbidden.status_code == 400
        assert forbidden.json() == {
            "code": "approval_required",
            "message": "客户内容需要人工审批",
        }
        assert unexpected.status_code == 503
        assert unexpected.json() == {
            "code": "service_unavailable",
            "message": "发送服务暂不可用",
        }
        assert "material-runtime-marker" not in unexpected.text
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "code": "service_unavailable",
            "message": "发送服务暂不可用",
        }
        assert all(response.status_code in {200, 409} for response in responses)
        assert any(response.status_code == 200 for response in responses)
        # 发送已经在途时新增抑制不能撤回已发生的发送；成功事实必须落账。
        assert raced.status_code == 200
        # 抑制后的SENT重放只读取canonical结果，不能重新发信或预留额度。
        assert raced.json()["duplicate"] is False
        assert repeated_race.status_code == 200
        assert repeated_race.json() == {
            **raced.json(), "status": "duplicate", "duplicate": True
        }
        assert commit_failed.status_code == 409
        assert commit_failed.json() == {
            "code": "reconciliation_required",
            "message": "发送结果需要人工对账",
        }
        assert commit_failure_retry.status_code == 409
        assert transport.send_calls == sends_after_commit_failure == 3
        assert tenant_b_response.status_code == 400
        assert tenant_b_response.json() == {
            "code": "validation_error",
            "message": "发送请求无效",
        }
        assert all(
            raw not in response.text
            for response in responses
            for raw in (subject, body, "recipient-marker@example.test")
        )
        async with factory() as session:
            attempt_row = await session.get(
                OutreachMessageAttemptRow,
                (str(tenant), str(attempt.attempt_id)),
            )
            race_attempt_row = await session.get(
                OutreachMessageAttemptRow,
                (str(tenant), str(race_attempt.attempt_id)),
            )
            race_enrollment_row = await session.get(
                OutreachEnrollmentRow,
                (str(tenant), str(race_attempt.enrollment_id)),
            )
            commit_failure_attempt_row = await session.get(
                OutreachMessageAttemptRow,
                (str(tenant), str(commit_failure_attempt.attempt_id)),
            )
            reservations = (
                await session.execute(
                    select(SendReservationRow).where(
                        SendReservationRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            calls = (
                await session.execute(
                    select(ToolCallRow).where(ToolCallRow.tenant_id == tenant)
                )
            ).scalars().all()
            events = (
                await session.execute(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == tenant
                    )
                )
            ).scalars().all()
            tenant_b_calls = (
                await session.execute(
                    select(ToolCallRow).where(
                        ToolCallRow.tenant_id == tenant_b
                    )
                )
            ).scalars().all()
        assert attempt_row is not None
        assert (attempt_row.state, attempt_row.provider_ref) == (
            "sent",
            "gmail_ref_manual_1",
        )
        assert race_attempt_row is not None
        assert (race_attempt_row.state, race_attempt_row.provider_ref) == (
            "sent",
            "gmail_ref_manual_1",
        )
        assert race_enrollment_row is not None
        assert race_enrollment_row.state == "stopped_suppressed"
        assert race_enrollment_row.stop_reason == "suppression"
        assert race_enrollment_row.stopped_at == _NOW
        assert race_enrollment_row.next_send_at is None
        assert commit_failure_attempt_row is not None
        assert (
            commit_failure_attempt_row.state,
            commit_failure_attempt_row.provider_ref,
        ) == ("sending", None)
        assert len(reservations) == 3
        assert tenant_b_calls == []
        assert Counter(row.status for row in calls)["succeeded"] == 2
        assert events
        persisted = repr([row.__dict__ for row in [*calls, *events, *reservations]])
        for raw in (
            subject,
            body,
            "recipient-marker@example.test",
            "sender-marker@example.test",
            "https://unsubscribe.example.test/u/",
            "f" * 32,
            "g" * 32,
        ):
            assert raw not in persisted
    finally:
        await engine.dispose()


async def test_manual_send_current_facts_win_and_duplicate_semantics_are_fixed(
    db_url: str,
) -> None:
    """发送前事实（回复/身份/审批/额度）变更必须生效；重复与冲突语义固定。"""
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    boss = EmployeeId(new_id("emp"))
    campaign_id = CampaignId(new_id("cmp"))
    approval_id = ApprovalId(new_id("apr"))
    trace = Trace()
    approvals = FakeApprovals(trace)
    transport = _Transport()
    subject = "subject-marker-current-facts"
    body = "body-marker-current-facts"
    try:
        await _seed_employee(factory, tenant, boss)
        without_send = build_phase1_dependencies(
            _settings(tenant), factory, now=lambda: _NOW, secret_resolver=_Secrets()
        )
        identity_id = await without_send.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address="sender-marker@example.test",
                domain="example.test",
                role=importlib.import_module(
                    "domains.sending_identity.models"
                ).DomainRole.COLD_OUTREACH,
            ),
            actor=SendingIdentityActor(
                str(boss),
                SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
                "boss",
            ),
        )
        system_identity = SendingIdentityActor(
            "system:manual-send-test",
            SendingIdentityScope(
                level=SendingIdentityScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({identity_id}),
            ),
            "system",
        )
        boss_identity = SendingIdentityActor(
            str(boss),
            SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
            "boss",
        )
        await without_send.sending_identities.begin_authentication(
            tenant, identity_id, actor=boss_identity
        )
        await without_send.sending_identities.record_authentication_result(
            tenant,
            identity_id,
            AuthenticationResult(
                checked_at=_NOW,
                spf_passed=True,
                dkim_passed=True,
                dmarc_passed=True,
                failures=(),
                check_ref="auth_current_facts",
            ),
            actor=system_identity,
        )
        await without_send.sending_identities.start_warmup(
            tenant, identity_id, 5, actor=boss_identity
        )
        await _seed_campaign(
            factory,
            tenant,
            campaign_id,
            identity_id,
            boss,
            approval_id,
            new_contact_limit=20,
            total_message_limit=20,
        )
        contact_snapshots: dict = {}
        reply_snapshots: dict = {}
        approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
            tenant,
            campaign_id,
            1,
            approval_id,
            CampaignApprovalState.APPROVED,
            boss,
            _NOW,
        )
        contacts = FakeContacts(contact_snapshots, trace)
        senders = FakeSenders(
            {
                identity_id: SendingIdentityEligibilitySnapshot(
                    tenant,
                    identity_id,
                    OutreachSenderRole.COLD_OUTREACH,
                    True,
                    True,
                    10,
                    _NOW,
                )
            },
            trace,
        )
        replies = FakeReplies(reply_snapshots, trace)
        dependencies = build_phase1_dependencies(
            _settings(tenant),
            factory,
            now=lambda: _NOW,
            manual_send=ManualSendComposition(
                contact_eligibility=contacts,
                sending_identity_eligibility=senders,
                campaign_approvals=approvals,
                reply_status=replies,
                delivery_materials=_Materials(),
                secret_resolver=_Secrets(),
                gmail_transport=transport,
            ),
        )
        app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant), dev_mode=True, retry_after_seconds=30
            ),
            dependencies=dependencies,
        )
        headers = {
            "X-Tenant-Id": str(tenant),
            "X-Employee-Id": str(boss),
        }
        boss_actor = OutreachActor(
            str(boss),
            OutreachScope(level=OutreachScopeLevel.TENANT),
            "boss",
        )

        async def enroll_and_prepare(
            suffix: str,
        ) -> tuple[MessageAttemptView, ContactPointId, ProspectAccountId]:
            account = ProspectAccountId(new_id("acc"))
            contact = ContactPointId(new_id("cp"))
            contact_snapshots[(contact, account)] = ContactEligibilitySnapshot(
                tenant,
                contact,
                account,
                ContactVerificationStatus.VERIFIED,
                _NOW,
                ContactLegalBasis.LEGITIMATE_INTEREST,
                f"basis_current_{suffix}",
                True,
                "US",
                "importer",
                frozenset({"hardware"}),
                _NOW,
            )
            reply_snapshots[(contact, account)] = ReplyStatusSnapshot(
                tenant,
                contact,
                account,
                ReplyState.NO_REPLY,
                None,
                _NOW,
            )
            enrollment = await dependencies.outreach.enroll(
                tenant,
                campaign_id,
                EnrollmentCreateRequest(
                    account, contact, IdempotencyKey(f"current-facts-{suffix}")
                ),
                actor=boss_actor,
            )
            attempt = await dependencies.outreach.prepare_message_attempt(
                tenant,
                enrollment.enrollment_id,
                actor=OutreachActor(
                    "system:manual-send-test",
                    OutreachScope(
                        level=OutreachScopeLevel.SYSTEM,
                        allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
                    ),
                    "system",
                ),
            )
            return attempt, contact, account

        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            async def send(attempt: MessageAttemptView, payload: dict[str, str]) -> Response:
                return await client.post(
                    f"/crm/message-attempts/{attempt.attempt_id}/send",
                    headers=headers,
                    json=payload,
                )

            reply_attempt, reply_contact, reply_account = (
                await enroll_and_prepare("reply")
            )
            # 回复事实变更：当前状态胜出，零发送。
            reply_snapshots[(reply_contact, reply_account)] = ReplyStatusSnapshot(
                tenant,
                reply_contact,
                reply_account,
                ReplyState.REPLIED,
                _NOW,
                _NOW,
            )
            replied = await send(
                reply_attempt, {"subject": subject, "body": body}
            )
            assert replied.status_code == 403
            assert replied.json() == {
                "code": "suppressed",
                "message": "当前事实不允许发送",
            }
            assert transport.send_calls == 0
            assert "subject-marker-current-facts" not in replied.text

            sender_attempt, _, _ = await enroll_and_prepare("sender")
            senders.snapshots[identity_id] = SendingIdentityEligibilitySnapshot(
                tenant,
                identity_id,
                OutreachSenderRole.COLD_OUTREACH,
                False,
                False,
                0,
                _NOW,
            )
            blocked = await send(sender_attempt, {"subject": subject, "body": body})
            assert blocked.status_code == 403
            assert transport.send_calls == 0
            # 恢复快照：发件身份资格是发送时刻事实，不影响后续 enrollment 准备。
            senders.snapshots[identity_id] = SendingIdentityEligibilitySnapshot(
                tenant,
                identity_id,
                OutreachSenderRole.COLD_OUTREACH,
                True,
                True,
                10,
                _NOW,
            )

            approval_attempt, _, _ = await enroll_and_prepare("approval")
            del approvals.values[(campaign_id, 1)]
            no_approval = await send(
                approval_attempt, {"subject": subject, "body": body}
            )
            assert no_approval.status_code == 403
            assert transport.send_calls == 0
            # 恢复审批：审批是发送时刻事实，不影响后续 enrollment 准备。
            approvals.values[(campaign_id, 1)] = CampaignApprovalSnapshot(
                tenant,
                campaign_id,
                1,
                approval_id,
                CampaignApprovalState.APPROVED,
                boss,
                _NOW,
            )

            duplicate_attempt, _, _ = await enroll_and_prepare("duplicate")
            first = await send(
                duplicate_attempt, {"subject": subject, "body": body}
            )
            assert first.status_code == 200
            assert first.json()["duplicate"] is False
            assert first.json()["status"] == "succeeded"
            second = await send(
                duplicate_attempt, {"subject": subject, "body": body}
            )
            assert second.status_code == 200
            assert second.json()["duplicate"] is True
            conflict = await send(
                duplicate_attempt,
                {"subject": "different-subject-marker", "body": body},
            )
            assert conflict.status_code == 409
            assert conflict.json() == {
                "code": "idempotency_conflict",
                "message": "发送请求与既有记录冲突",
            }

            # 当日预热额度（5）用尽后固定 429，当前状态仍然胜出。
            for index in range(4):
                extra, _, _ = await enroll_and_prepare(f"rate-{index}")
                drained = await send(extra, {"subject": subject, "body": body})
                assert drained.status_code == 200
            rate_attempt, _, _ = await enroll_and_prepare("rate-limit")
            rate_limited = await send(
                rate_attempt, {"subject": subject, "body": body}
            )
            assert rate_limited.status_code == 429
            assert rate_limited.json() == {
                "code": "rate_limited",
                "message": "发送额度暂不可用",
            }
            assert "body-marker-current-facts" not in rate_limited.text
    finally:
        await engine.dispose()
