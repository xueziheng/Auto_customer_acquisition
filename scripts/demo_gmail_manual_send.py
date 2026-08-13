"""用受控 Gmail transport 演示真实 Postgres 手工单封发送与对账边界。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.runtime import (
    ManualSendComposition,
    build_phase1_dependencies,
)
from apps.api.runtime_config import Phase1RuntimeSettings
from connectors.gmail.transport import GmailNetworkError
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
    CampaignCreateRequest,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    MessageSendPreflight,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
)
from domains.sending_identity.permissions import (
    Actor as SendingIdentityActor,
)
from domains.sending_identity.permissions import (
    ScopeLevel as SendingIdentityScopeLevel,
)
from domains.sending_identity.permissions import SendingIdentityScope
from domains.sending_identity.schemas import (
    AuthenticationResult,
    DomainRole,
    IdentityRegisterRequest,
)
from infra.db.repositories.employees import EmployeeRepositoryImpl
from infra.db.session import create_engine_from
from infra.db.tables import SendReservationRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ApprovalId,
    ContactPointId,
    EmployeeId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.handlers.email_send import DeliveryMaterial
from tool_gateway.pipeline import ToolCallContext

_FAILURE_MESSAGE = "Gmail 手动发送演示运行失败"
_NOW = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)


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
        return self.values.get((campaign_id, version))


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
            from_address="demo-sender-marker@example.test",
            recipient_address="demo-recipient-marker@example.test",
        )


class _DemoSecrets:
    def resolve(self, secret_ref: str) -> str:
        values = {
            "demo-fingerprint-ref": "f" * 32,
            "demo-gmail-ref": "demo-oauth-value-marker",
            "DEMO_UNSUBSCRIBE_REF": "u" * 32,
        }
        if secret_ref not in values:
            raise PermissionDenied("演示凭证引用拒绝")
        return values[secret_ref]


class _ControlledGmailTransport:
    """只在本进程内返回固定结果；绝不打开 socket。"""

    def __init__(self) -> None:
        self.search_count = 0
        self.send_attempt_count = 0
        self.successful_send_count = 0
        self.closed = False

    async def search(self, **kwargs: object) -> None:
        del kwargs
        self.search_count += 1

    async def send(self, **kwargs: object) -> str:
        del kwargs
        self.send_attempt_count += 1
        if self.send_attempt_count == 1:
            self.successful_send_count += 1
            return "gmail_demo_ref_1"
        raise GmailNetworkError(may_have_written=True)

    async def close(self) -> None:
        self.closed = True


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
            "GMAIL_OAUTH_TOKEN_REF": "demo-gmail-ref",
            "TOOL_CALL_FINGERPRINT_KEY_REF": "demo-fingerprint-ref",
            "TOOL_CALL_FINGERPRINT_KEY_VERSION": "demo-v1",
            "TRADEOS_UNSUBSCRIBE_BASE_URL": "https://unsubscribe.example.test",
            "TRADEOS_EMAIL_FEEDBACK_ROUTE_ID": "feedback-route-v1",
            "TRADEOS_UNSUBSCRIBE_ACTIVE_KEY_ID": "2026-v1",
            "TRADEOS_UNSUBSCRIBE_KEY_REFS_JSON": (
                '{"2026-v1":"DEMO_UNSUBSCRIBE_REF"}'
            ),
            "TRADEOS_TOOL_LEASE_SECONDS": "5",
        }
    )


def _outreach_boss(employee_id: EmployeeId) -> OutreachActor:
    return OutreachActor(
        str(employee_id),
        OutreachScope(level=OutreachScopeLevel.TENANT),
        "boss",
    )


def _sending_boss(employee_id: EmployeeId) -> SendingIdentityActor:
    return SendingIdentityActor(
        str(employee_id),
        SendingIdentityScope(level=SendingIdentityScopeLevel.TENANT),
        "boss",
    )


def _sending_system(identity_id: SendingIdentityId) -> SendingIdentityActor:
    return SendingIdentityActor(
        "system:gmail-demo",
        SendingIdentityScope(
            level=SendingIdentityScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )


async def _seed_employee(
    factory: async_sessionmaker, tenant: TenantId, employee_id: EmployeeId
) -> None:
    models = importlib.import_module("domains.employees.models")
    async with factory() as session:
        await EmployeeRepositoryImpl(session, tenant).add(
            models.Employee(
                employee_id=employee_id,
                tenant_id=tenant,
                user_id=UserId(str(employee_id)),
                name="Gmail Demo Operator",
                role=models.Role.BOSS,
                created_at=_NOW,
            )
        )
        await session.commit()


async def _exercise(database_url: str) -> dict[str, object]:
    if os.environ.get("TRADEOS_GMAIL_DEMO_MODE") != "controlled":
        raise RuntimeError("Gmail 演示模式未启用")
    tenant = TenantId(new_id("tn"))
    employee_id = EmployeeId(new_id("emp"))
    contacts = _DemoContacts(tenant)
    senders = _DemoSenders(tenant)
    approvals = _DemoApprovals(tenant)
    replies = _DemoReplies(tenant)
    materials = _DemoMaterials()
    secrets = _DemoSecrets()
    transport = _ControlledGmailTransport()
    engine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await _seed_employee(factory, tenant, employee_id)
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
                secret_resolver=secrets,
                gmail_transport=transport,
            ),
        )
        identity_id = await dependencies.sending_identities.register(
            tenant,
            IdentityRegisterRequest(
                address="demo-sender-marker@example.test",
                domain="example.test",
                role=DomainRole.COLD_OUTREACH,
                connector_ref="gmail_demo_connector",
            ),
            actor=_sending_boss(employee_id),
        )
        await dependencies.sending_identities.begin_authentication(
            tenant, identity_id, actor=_sending_boss(employee_id)
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
                check_ref="gmail_demo_auth",
            ),
            actor=_sending_system(identity_id),
        )
        await dependencies.sending_identities.start_warmup(
            tenant, identity_id, 5, actor=_sending_boss(employee_id)
        )
        senders.values[identity_id] = SendingIdentityEligibilitySnapshot(
            tenant,
            identity_id,
            OutreachSenderRole.COLD_OUTREACH,
            True,
            True,
            5,
            _NOW,
        )

        boss = _outreach_boss(employee_id)
        campaign = await dependencies.outreach.create_campaign(
            tenant,
            CampaignCreateRequest(
                name="Controlled Gmail demo",
                markets=("US",),
                target_entity_types=("importer",),
                allowed_categories=("hardware",),
                sender_identity_ids=(identity_id,),
                steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
                daily_new_contact_limit=5,
                daily_total_message_limit=5,
                handoff_triggers=(),
            ),
            actor=boss,
        )
        await dependencies.outreach.submit_campaign(
            tenant, campaign.campaign_id, actor=boss
        )
        approval = CampaignApprovalSnapshot(
            tenant,
            campaign.campaign_id,
            1,
            ApprovalId(new_id("apr")),
            CampaignApprovalState.APPROVED,
            employee_id,
            _NOW,
        )
        approvals.values[(str(campaign.campaign_id), 1)] = approval
        await dependencies.outreach.activate_campaign(
            tenant, campaign.campaign_id, actor=boss
        )

        attempts = []
        for index in range(2):
            account_id = ProspectAccountId(new_id("acc"))
            contact_id = ContactPointId(new_id("cp"))
            contacts.values[(contact_id, account_id)] = ContactEligibilitySnapshot(
                tenant,
                contact_id,
                account_id,
                ContactVerificationStatus.VERIFIED,
                _NOW,
                ContactLegalBasis.LEGITIMATE_INTEREST,
                f"gmail_demo_basis_{index + 1}",
                True,
                "US",
                "importer",
                frozenset({"hardware"}),
                _NOW,
            )
            replies.values[(contact_id, account_id)] = ReplyStatusSnapshot(
                tenant,
                contact_id,
                account_id,
                ReplyState.NO_REPLY,
                None,
                _NOW,
            )
            enrollment = await dependencies.outreach.enroll(
                tenant,
                campaign.campaign_id,
                EnrollmentCreateRequest(
                    account_id,
                    contact_id,
                    IdempotencyKey(f"gmail-demo-enrollment-{index + 1}"),
                ),
                actor=boss,
            )
            system = OutreachActor(
                "system:gmail-demo",
                OutreachScope(
                    level=OutreachScopeLevel.SYSTEM,
                    allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
                ),
                "system",
            )
            attempts.append(
                await dependencies.outreach.prepare_message_attempt(
                    tenant, enrollment.enrollment_id, actor=system
                )
            )

        def context(attempt_id: str) -> ToolCallContext:
            return ToolCallContext(
                tenant_id=tenant,
                user_id=UserId(str(employee_id)),
                tool_id="email.send",
                params={
                    "attempt_id": attempt_id,
                    "subject": "demo-subject-marker",
                    "body": "demo-body-marker",
                },
            )

        first = await dependencies.tool_gateway.invoke(
            context(str(attempts[0].attempt_id))
        )
        duplicate = await dependencies.tool_gateway.invoke(
            context(str(attempts[0].attempt_id))
        )
        ambiguous = await dependencies.tool_gateway.invoke(
            context(str(attempts[1].attempt_id))
        )
        if (
            first.status is not ToolCallStatus.SUCCEEDED
            or duplicate.status is not ToolCallStatus.DUPLICATE
            or ambiguous.error_category
            is not ToolErrorCategory.RECONCILIATION_REQUIRED
            or first.tool_call_id is None
            or ambiguous.tool_call_id is None
        ):
            raise RuntimeError("Gmail 演示结果不符合预期")
        async with factory() as session:
            reservation_count = await session.scalar(
                select(func.count())
                .select_from(SendReservationRow)
                .where(SendReservationRow.tenant_id == tenant)
            )
        return {
            "ambiguous_status": ambiguous.error_category.value,
            "attempt_ids": [str(attempt.attempt_id) for attempt in attempts],
            "duplicate_status": duplicate.status.value,
            "first_status": first.status.value,
            "gmail_search_count": transport.search_count,
            "gmail_send_count": transport.successful_send_count,
            "reservation_count": int(reservation_count or 0),
            "tenant_id": str(tenant),
            "tool_call_ids": [first.tool_call_id, ambiguous.tool_call_id],
        }
    finally:
        await transport.close()
        await engine.dispose()
        if not transport.closed:
            raise RuntimeError("Gmail 演示 transport 未关闭")


def main() -> int:
    try:
        database_url = os.environ["DATABASE_URL"]
        logging.getLogger().handlers.clear()
        logging.getLogger().addHandler(logging.NullHandler())
        summary = asyncio.run(_exercise(database_url))
    except Exception:  # noqa: BLE001 - 进程边界只能输出固定脱敏消息
        print(_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
