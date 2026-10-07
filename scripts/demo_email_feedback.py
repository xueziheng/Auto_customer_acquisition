"""离线演示真实 PostgreSQL 邮件反馈闭环，不构成生产 composition。

显式提供 TRADEOS_TENANT_ID 与对应受限角色 DATABASE_URL；仅使用隔离演示库。
启动前必须完成迁移和角色配置，本入口不创建角色、不绕过租户门禁。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
from apps.api.runtime_config import Phase1RuntimeSettings
from apps.email_feedback_worker.runtime import EmailFeedbackRuntimeFactory
from domains.outreach.permissions import Actor as OutreachActor
from domains.outreach.permissions import OutreachScope
from domains.outreach.permissions import ScopeLevel as OutreachScopeLevel
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    DeliveryCorrelationBinding,
    EnrollmentCreateRequest,
    MessageSendPreflight,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
)
from domains.sending_identity.permissions import Actor as SendingIdentityActor
from domains.sending_identity.permissions import ScopeLevel as SendingScopeLevel
from domains.sending_identity.permissions import SendingIdentityScope
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DomainRole,
    IdentityRegisterRequest,
)
from infra.db.repositories.employees import EmployeeRepositoryImpl
from infra.db.session import create_engine_from
from infra.db.tables import (
    EmailFeedbackCursorRow,
    EmailFeedbackQuarantineRow,
    EmailFeedbackReceiptRow,
    OutboxEventRow,
    OutreachActionRow,
    OutreachEnrollmentRow,
    OutreachSuppressionRow,
    ReputationEventRow,
    UnsubscribeTokenRow,
)
from infra.db.tenant_security import assert_tenant_database_isolation
from shared.errors import PermissionDenied
from shared.schemas.email_feedback import EmailFeedbackPage
from shared.schemas.identifiers import (
    ApprovalId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.handlers.email_send import DeliveryMaterial

_FAILURE_MESSAGE = "邮件反馈演示运行失败"
_NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
_ROUTE = "feedback-v1"
_MAILBOX = "feedback"


class _DemoContacts:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[
            tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot
        ] = {}

    async def get_contact_eligibility(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ContactEligibilitySnapshot:
        value = self.values.get((contact_point_id, account_id))
        if tenant_id != self._tenant or value is None:
            raise PermissionDenied("演示联系人事实拒绝")
        return value


class _DemoSenders:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[SendingIdentityId, SendingIdentityEligibilitySnapshot] = {}

    async def get_sending_identity_eligibility(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> SendingIdentityEligibilitySnapshot:
        value = self.values.get(identity_id)
        if tenant_id != self._tenant or value is None:
            raise PermissionDenied("演示发件身份事实拒绝")
        return value


class _DemoApprovals:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[tuple[str, int], CampaignApprovalSnapshot] = {}

    async def get_campaign_approval(
        self, tenant_id: TenantId, campaign_id: str, version: int
    ) -> CampaignApprovalSnapshot | None:
        if tenant_id != self._tenant:
            raise PermissionDenied("演示审批事实拒绝")
        return self.values.get((str(campaign_id), version))


class _DemoReplies:
    def __init__(self, tenant: TenantId) -> None:
        self._tenant = tenant
        self.values: dict[
            tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot
        ] = {}

    async def get_reply_status(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ReplyStatusSnapshot:
        value = self.values.get((contact_point_id, account_id))
        if tenant_id != self._tenant or value is None:
            raise PermissionDenied("演示回复事实拒绝")
        return value


class _DemoMaterials:
    async def resolve(
        self, tenant_id: TenantId, preflight: MessageSendPreflight
    ) -> DeliveryMaterial:
        return DeliveryMaterial(
            tenant_id=tenant_id,
            attempt_id=preflight.attempt_id,
            account_id=preflight.account_id,
            contact_point_id=preflight.contact_point_id,
            sending_identity_id=preflight.sending_identity_id,
            from_address="feedback-demo-sender@example.test",
            recipient_address="feedback-demo-recipient@example.test",
        )


class _DemoSecrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "DEMO_FINGERPRINT_REF": "f" * 32,
            "DEMO_GMAIL_REF": "demo-oauth-private-marker",
            "DEMO_UNSUBSCRIBE_REF": "u" * 32,
        }
        try:
            return values[secret_ref]
        except KeyError:
            raise PermissionDenied("演示凭证引用拒绝") from None


class _NoSendTransport:
    async def search(self, **_kwargs: object) -> None:
        return None

    async def send(self, **_kwargs: object) -> str:
        raise AssertionError("邮件反馈演示不得发送")


class _FeedbackTransport(_NoSendTransport):
    def __init__(self) -> None:
        self.messages: dict[str, bytes] = {}

    async def get_profile_history_id(self, **_kwargs: object) -> str:
        return "100"

    async def list_feedback_messages(
        self, **_kwargs: object
    ) -> tuple[tuple[str, ...], None]:
        return tuple(self.messages), None

    async def list_feedback_history(
        self, **_kwargs: object
    ) -> tuple[tuple[str, ...], None, str]:
        return (), None, "100"

    async def get_raw_message(self, *, message_ref: str, **_kwargs: object) -> bytes:
        return self.messages[message_ref]


def _dsn(status: str, binding: DeliveryCorrelationBinding) -> bytes:
    boundary = "tradeos-feedback-demo-boundary"
    lines = [
        "Date: Thu, 13 Aug 2026 10:00:00 +0000",
        f'Content-Type: multipart/report; report-type="delivery-status"; boundary="{boundary}"',
        "MIME-Version: 1.0",
        "",
        f"--{boundary}",
        "Content-Type: text/plain",
        "",
        "private-customer-body-marker",
        f"--{boundary}",
        "Content-Type: message/delivery-status",
        "",
        "Reporting-MTA: dns; mx.example.invalid",
        "",
        "Final-Recipient: rfc822; private-customer@example.test",
        f"Status: {status}",
        "",
        f"--{boundary}",
        "Content-Type: message/rfc822",
        "",
        f"Message-ID: {binding.deterministic_message_id}",
        f"X-TradeOS-Idempotency-V1: {binding.idempotency_header}",
        "",
        f"--{boundary}--",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


def _malformed() -> bytes:
    raw = b"Content-Type: multipart/report; report-type=delivery-status\r\n\r\nmalformed"
    return raw


def _settings(tenant: TenantId) -> Phase1RuntimeSettings:
    return Phase1RuntimeSettings.from_environ(
        {
            "DATABASE_URL": "postgresql+asyncpg://unused.invalid/tradeos",
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
            "GMAIL_OAUTH_TOKEN_REF": "DEMO_GMAIL_REF",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "DEMO_FINGERPRINT_REF",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "feedback-v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": _ROUTE,
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
                '{"2026-v1":"DEMO_UNSUBSCRIBE_REF"}'
            ),
            "TRADEOS_TOOL_LEASE_SECONDS": "5",
        }
    )


def _worker_environment(database_url: str, tenant: TenantId, identity: str) -> dict[str, str]:
    gmail_secret_ref = os.environ.get(
        "TRADEOS_EMAIL_FEEDBACK_DEMO_SECRET_REF", "GMAIL_WORKER_OAUTH_TOKEN"
    )
    return {
        "DATABASE_URL": database_url,
        "GMAIL_OAUTH_TOKEN_REF": gmail_secret_ref,
        "GMAIL_WORKER_OAUTH_TOKEN": "demo-oauth-private-marker",
        "TOOL_CALL_FINGERPRINT_KEY_REF": "TOOL_WORKER_FINGERPRINT_KEY",
        "TOOL_WORKER_FINGERPRINT_KEY": "k" * 32,
        "TOOL_CALL_FINGERPRINT_KEY_VERSION": "feedback-v1",
        "TRADEOS_DEV_MODE": "false",
        "TRADEOS_TENANT_ID": str(tenant),
        "TRADEOS_EMAIL_FEEDBACK_GMAIL_BASE_URL": "https://gmail.googleapis.com",
        "TRADEOS_EMAIL_FEEDBACK_MAILBOX_ALIAS": _MAILBOX,
        "TRADEOS_EMAIL_FEEDBACK_SENDING_IDENTITY_ID": identity,
        "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": _ROUTE,
        "TRADEOS_EMAIL_FEEDBACK_ENABLED": "true",
        "TRADEOS_EMAIL_FEEDBACK_HEALTH_PORT": "8092",
        "TRADEOS_EMAIL_FEEDBACK_POLL_INTERVAL_SECONDS": "30",
        "TRADEOS_EMAIL_FEEDBACK_PAGE_LIMIT": "100",
    }


def _outreach_boss(employee: EmployeeId) -> OutreachActor:
    return OutreachActor(
        str(employee), OutreachScope(level=OutreachScopeLevel.TENANT), "boss"
    )


def _outreach_system(
    *, enrollment: EnrollmentId | None = None, attempt: MessageAttemptId | None = None
) -> OutreachActor:
    return OutreachActor(
        "system:feedback-demo",
        OutreachScope(
            level=OutreachScopeLevel.SYSTEM,
            allowed_enrollment_ids=(
                frozenset({enrollment}) if enrollment is not None else None
            ),
            allowed_attempt_ids=(
                frozenset({attempt}) if attempt is not None else None
            ),
        ),
        "system",
    )


def _sending_boss(employee: EmployeeId) -> SendingIdentityActor:
    return SendingIdentityActor(
        str(employee), SendingIdentityScope(level=SendingScopeLevel.TENANT), "boss"
    )


def _sending_system(identity: SendingIdentityId) -> SendingIdentityActor:
    return SendingIdentityActor(
        "system:feedback-demo",
        SendingIdentityScope(
            level=SendingScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity}),
        ),
        "system",
    )


async def _seed_employee(
    factory: async_sessionmaker, tenant: TenantId, employee: EmployeeId
) -> None:
    models = __import__("domains.employees.models", fromlist=["Employee"])
    async with factory() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee_id=employee,
                tenant_id=tenant,
                user_id=UserId(str(employee)),
                name="Feedback Demo Operator",
                role=models.Role.BOSS,
                created_at=_NOW,
            )
        )
        await session.commit()


async def _readback(
    factory: async_sessionmaker,
    tenant: TenantId,
    identity: SendingIdentityId,
    campaign_id: str,
    enrollments: tuple[EnrollmentId, EnrollmentId],
    attempts: tuple[MessageAttemptId, MessageAttemptId],
    duplicate_count: int,
) -> dict[str, object]:
    async with factory() as session:
        cursor = await session.get(EmailFeedbackCursorRow, (str(tenant), _MAILBOX))
        enrollment_rows = [
            await session.get(OutreachEnrollmentRow, (str(tenant), str(item)))
            for item in enrollments
        ]

        async def count(row_type: Any) -> int:
            value = await session.scalar(
                select(func.count())
                .select_from(row_type)
                .where(row_type.tenant_id == tenant)
            )
            return int(value or 0)

        outbox = (
            await session.execute(
                select(OutboxEventRow.event_type).where(
                    OutboxEventRow.tenant_id == tenant
                )
            )
        ).scalars().all()
        consumed = await session.scalar(
            select(func.count())
            .select_from(UnsubscribeTokenRow)
            .where(
                UnsubscribeTokenRow.tenant_id == tenant,
                UnsubscribeTokenRow.consumed_at.is_not(None),
            )
        )
        if cursor is None or any(row is None for row in enrollment_rows):
            raise RuntimeError("邮件反馈演示读回失败")
        return {
            "action_count": await count(OutreachActionRow),
            "campaign_id": campaign_id,
            "cursor_version": cursor.version,
            "duplicate_count": duplicate_count,
            "enrollment_ids": [str(item) for item in enrollments],
            "enrollment_states": [row.state for row in enrollment_rows],
            "identity_id": str(identity),
            "message_attempt_ids": [str(item) for item in attempts],
            "outbox_counts": dict(sorted(Counter(outbox).items())),
            "quarantine_count": await count(EmailFeedbackQuarantineRow),
            "receipt_count": await count(EmailFeedbackReceiptRow),
            "reputation_count": await count(ReputationEventRow),
            "suppression_count": await count(OutreachSuppressionRow),
            "tenant_id": str(tenant),
            "token_consumed_count": int(consumed or 0),
        }


async def _exercise(database_url: str) -> dict[str, object]:
    if os.environ.get("TRADEOS_EMAIL_FEEDBACK_DEMO_MODE") != "controlled":
        raise RuntimeError("邮件反馈演示模式未启用")
    tenant = TenantId(os.environ["TRADEOS_TENANT_ID"])
    employee = EmployeeId(new_id("emp"))
    contacts = _DemoContacts(tenant)
    senders = _DemoSenders(tenant)
    approvals = _DemoApprovals(tenant)
    replies = _DemoReplies(tenant)
    secrets = _DemoSecrets()
    no_send = _NoSendTransport()
    engine = create_engine_from(database_url)
    try:
        await assert_tenant_database_isolation(engine, tenant)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_employee(factory, tenant, employee)
        dependencies = build_phase1_dependencies(
            _settings(tenant),
            factory,
            now=lambda: _NOW,
            manual_send=ManualSendComposition(
                contacts,
                senders,
                approvals,
                replies,
                _DemoMaterials(),
                secrets,
                no_send,
            ),
        )
        identity = await dependencies.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address="feedback-demo-sender@example.test",
                domain="example.test",
                role=DomainRole.COLD_OUTREACH,
                connector_ref="feedback_demo_connector",
            ),
            actor=_sending_boss(employee),
        )
        await dependencies.sending_identities.begin_authentication(
            tenant, identity, actor=_sending_boss(employee)
        )
        await dependencies.sending_identities.record_authentication_result(
            tenant,
            identity,
            AuthenticationResult(_NOW, True, True, True, (), "feedback_demo_auth"),
            actor=_sending_system(identity),
        )
        await dependencies.sending_identities.start_warmup(
            tenant, identity, 5, actor=_sending_boss(employee)
        )
        senders.values[identity] = SendingIdentityEligibilitySnapshot(
            tenant, identity, OutreachSenderRole.COLD_OUTREACH, True, True, 5, _NOW
        )

        boss = _outreach_boss(employee)
        campaign = await dependencies.outreach.create_campaign(
            tenant,
            CampaignCreateRequest(
                name="Feedback demo campaign",
                markets=("US",),
                target_entity_types=("importer",),
                allowed_categories=("hardware",),
                sender_identity_ids=(identity,),
                steps=(
                    SequenceStepRequest(1, StepIntent.DISCOVERY, 0),
                    SequenceStepRequest(2, StepIntent.FOLLOW_UP, 2),
                ),
                daily_new_contact_limit=5,
                daily_total_message_limit=5,
                handoff_triggers=(),
            ),
            actor=boss,
        )
        await dependencies.outreach.submit_campaign(
            tenant, campaign.campaign_id, actor=boss
        )
        approvals.values[(str(campaign.campaign_id), 1)] = CampaignApprovalSnapshot(
            tenant,
            campaign.campaign_id,
            1,
            ApprovalId(new_id("apr")),
            CampaignApprovalState.APPROVED,
            employee,
            _NOW,
        )
        await dependencies.outreach.activate_campaign(
            tenant, campaign.campaign_id, actor=boss
        )

        enrollment_ids: list[EnrollmentId] = []
        attempt_ids: list[MessageAttemptId] = []
        bindings: list[DeliveryCorrelationBinding] = []
        preflights: list[MessageSendPreflight] = []
        for index in range(2):
            account = ProspectAccountId(new_id("acc"))
            contact = ContactPointId(new_id("cp"))
            contacts.values[(contact, account)] = ContactEligibilitySnapshot(
                tenant,
                contact,
                account,
                ContactVerificationStatus.VERIFIED,
                _NOW,
                ContactLegalBasis.LEGITIMATE_INTEREST,
                f"feedback_demo_basis_{index}",
                True,
                "US",
                "importer",
                frozenset({"hardware"}),
                _NOW,
            )
            replies.values[(contact, account)] = ReplyStatusSnapshot(
                tenant, contact, account, ReplyState.NO_REPLY, None, _NOW
            )
            enrollment = await dependencies.outreach.enroll(
                tenant,
                campaign.campaign_id,
                EnrollmentCreateRequest(
                    account,
                    contact,
                    IdempotencyKey(f"feedback-demo-enrollment-{index}"),
                ),
                actor=boss,
            )
            attempt = await dependencies.outreach.prepare_message_attempt(
                tenant,
                enrollment.enrollment_id,
                actor=_outreach_system(enrollment=enrollment.enrollment_id),
            )
            delivery_actor = _outreach_system(
                enrollment=enrollment.enrollment_id
            )
            await dependencies.outreach.claim_message_send(
                tenant, attempt.attempt_id, actor=delivery_actor
            )
            await dependencies.outreach.record_sent(
                tenant,
                attempt.attempt_id,
                f"feedback_demo_provider_{index}",
                actor=delivery_actor,
            )
            digest = chr(ord("a") + index) * 64
            binding = DeliveryCorrelationBinding(
                f"<{_ROUTE}.{digest}@messages.tradeos.invalid>",
                f"{_ROUTE}.{digest}",
                _ROUTE,
            )
            await dependencies.outreach.bind_delivery_correlation(
                tenant,
                attempt.attempt_id,
                binding,
                actor=_outreach_system(attempt=attempt.attempt_id),
            )
            preflights.append(
                await dependencies.outreach.preflight_message_send(
                    tenant, attempt.attempt_id, actor=delivery_actor
                )
            )
            enrollment_ids.append(enrollment.enrollment_id)
            attempt_ids.append(attempt.attempt_id)
            bindings.append(binding)

        link = await dependencies.unsubscribe_service.issue(tenant, preflights[1])
        opaque_token = link.url.rsplit("/", 1)[-1]
        feedback_transport = _FeedbackTransport()
        feedback_transport.messages = {
            "feedback_hard_ref": _dsn("5.1.1", bindings[0]),
            "feedback_soft_ref": _dsn("4.2.2", bindings[1]),
            "feedback_bad_ref": _malformed(),
        }
        runtime_factory = EmailFeedbackRuntimeFactory(
            _worker_environment(database_url, tenant, identity),
            transport_factory=lambda _base_url: feedback_transport,
            now=lambda: _NOW,
        )
        async with runtime_factory() as application:
            page = await application.runtime.reader.fetch(
                tenant, _MAILBOX, None, 100
            )
            result = await application.runtime.processor.process(
                tenant, _MAILBOX, identity, None, page
            )
            replay = await application.runtime.processor.process(
                tenant,
                _MAILBOX,
                identity,
                page.next_cursor,
                EmailFeedbackPage(page.next_cursor, page.next_cursor, page.items),
            )
        if (
            (result.processed, result.hard_bounces, result.soft_bounces)
            != (3, 1, 1)
            or result.quarantined != 1
            or replay.duplicates != 3
            or not await dependencies.unsubscribe_service.consume(opaque_token)
        ):
            raise RuntimeError("邮件反馈演示行为不符合预期")
        return await _readback(
            factory,
            tenant,
            identity,
            str(campaign.campaign_id),
            (enrollment_ids[0], enrollment_ids[1]),
            (attempt_ids[0], attempt_ids[1]),
            replay.duplicates,
        )
    finally:
        await engine.dispose()


def main() -> int:
    try:
        logging.getLogger().handlers.clear()
        logging.getLogger().addHandler(logging.NullHandler())
        summary = asyncio.run(_exercise(os.environ["DATABASE_URL"]))
    except Exception:  # noqa: BLE001 - 进程边界不得泄露配置或客户内容
        print(_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
