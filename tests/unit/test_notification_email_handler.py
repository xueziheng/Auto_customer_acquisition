"""事务通知邮件 Tool Gateway 适配器的安全合同。"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from connectors.gmail.client import GmailConnector
from domains.sending_identity.permissions import Actor, ScopeLevel, SendingIdentityScope
from domains.sending_identity.schemas import (
    DomainRole,
    IdentityState,
    IdentityView,
    SendPermission,
    SendReservation,
)
from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import PolicyViolation
from shared.schemas.identifiers import (
    EmployeeId,
    IdempotencyKey,
    NotificationJobId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.pipeline import ToolCallContext, ToolInvocationState


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：事务通知邮件模块尚未实现（{exc}）")


class _Secrets:
    def resolve(self, _secret_ref: str) -> str:
        return "oauth-private-marker"


class _Transport:
    def __init__(self) -> None:
        self.searches = 0
        self.sends: list[bytes] = []

    async def search(self, **_kwargs):
        self.searches += 1

    async def send(self, *, token: str, raw_message: bytes) -> str:
        assert token == "oauth-private-marker"
        self.sends.append(raw_message)
        return "gmail_notification_ref_1"


def _notification(
    tenant: TenantId, employee: EmployeeId, job_id: NotificationJobId
) -> Notification:
    return Notification(
        tenant,
        employee,
        NotificationPriority.URGENT,
        "承诺已逾期",
        NotificationContext(
            NotificationKind.COMMITMENT_OVERDUE,
            new_id("com"),
            None,
            None,
            None,
        ),
        "CommitmentOverdue",
        "safe:dedup",
        next_step="处理逾期承诺并更新下一步",
        link="/crm/commitments/safe",
        source_job_id=job_id,
    )


def _context(
    tenant: TenantId, employee: EmployeeId, job_id: NotificationJobId
) -> ToolCallContext:
    module = _module("tool_gateway.handlers.notification_email")
    return ToolCallContext(
        tenant,
        UserId(new_id("usr")),
        module.MANIFEST.tool_id,
        {
            "notification_id": str(job_id),
            "recipient_employee_id": str(employee),
            "template_code": NotificationKind.COMMITMENT_OVERDUE.value,
        },
        idempotency_key=IdempotencyKey(f"notification:{job_id}"),
    )


def test_manifest_is_the_exact_non_campaign_transactional_contract() -> None:
    module = _module("tool_gateway.handlers.notification_email")
    manifest = module.MANIFEST
    assert manifest.tool_id == "notification.email.send"
    assert manifest.checks == (
        "tenant",
        "permission",
        "idempotency",
        "rate_limit",
    )
    assert manifest.requires_approval is False
    assert "suppression" not in manifest.checks
    assert "approval" not in manifest.checks
    assert set(manifest.input_schema["properties"]) == {
        "notification_id",
        "recipient_employee_id",
        "template_code",
    }


@pytest.mark.asyncio
async def test_prepare_persists_only_safe_ids_and_template_then_rate_preflight_materializes() -> None:
    module = _module("tool_gateway.handlers.notification_email")
    recipient_module = _module("apps.notification_worker.recipients")
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    job_id = NotificationJobId(new_id("njb"))
    identity_id = SendingIdentityId(new_id("sid"))
    notification = _notification(tenant, employee, job_id)
    context = _context(tenant, employee, job_id)
    transport = _Transport()
    gmail = GmailConnector(transport)
    await gmail.configure(_Secrets())
    handler = module.NotificationEmailSendHandler(
        gmail,
        HmacFingerprintProvider("notification-v1", b"n" * 32),
    )
    with handler.bind(notification):
        prepared = await handler.prepare(context, None)
        rendered = repr(prepared)
        assert prepared.audit_projection == {
            "notification_id": str(job_id),
            "employee_id": str(employee),
            "template_code": NotificationKind.COMMITMENT_OVERDUE.value,
        }
        for marker in (
            "owner@example.com",
            "alerts@example.com",
            "承诺已逾期",
            "处理逾期承诺",
        ):
            assert marker not in rendered
        assert transport.searches == 0 and transport.sends == []

        identity = _IdentityService(identity_id)
        directory = recipient_module.ConfiguredNotificationRecipientDirectory.from_value(
            [
                {
                    "tenant_id": str(tenant),
                    "employee_id": str(employee),
                    "address": "owner@example.com",
                }
            ]
        )
        actor = Actor(
            "system:notification",
            SendingIdentityScope(
                level=ScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({identity_id}),
            ),
            "system",
        )
        stage = module.NotificationEmailRateLimitCheck(
            handler, directory, identity, identity_id, actor
        )
        state = ToolInvocationState(module.MANIFEST, new_id("tcl"), prepared)
        assert await stage.check(context, state) is None
        assert identity.calls == ["get", "check", "reserve"]
        result = await handler.execute(tenant, state.prepared)
    assert result == {
        "provider_ref": "gmail_notification_ref_1",
        "already_existed": False,
    }
    assert len(transport.sends) == 1
    assert b"List-Unsubscribe" not in transport.sends[0]


class _IdentityService:
    def __init__(
        self,
        identity_id: SendingIdentityId,
        *,
        role: DomainRole = DomainRole.TRANSACTIONAL,
    ) -> None:
        self.identity_id = identity_id
        self.role = role
        self.calls: list[str] = []

    async def get(self, tenant_id, identity_id, *, actor):
        del tenant_id, actor
        self.calls.append("get")
        assert identity_id == self.identity_id
        return IdentityView(
            identity_id,
            "alerts@example.com",
            "example.com",
            self.role,
            IdentityState.ACTIVE,
            datetime(2026, 8, 14, tzinfo=UTC),
            can_send_today=True,
            remaining_today=10,
        )

    async def check_send_permission(
        self, tenant_id, identity_id, for_cold_outreach, *, actor
    ):
        del tenant_id, actor
        self.calls.append("check")
        assert identity_id == self.identity_id
        assert for_cold_outreach is False
        return SendPermission(True, 10, 10, IdentityState.ACTIVE)

    async def reserve_send_slot(
        self,
        tenant_id,
        identity_id,
        reservation_key,
        for_cold_outreach,
        *,
        actor,
    ):
        del tenant_id, actor
        self.calls.append("reserve")
        assert identity_id == self.identity_id
        assert for_cold_outreach is False
        return SendReservation(
            "reservation-safe",
            identity_id,
            reservation_key,
            date(2026, 8, 14),
            1,
            10,
            9,
        )


@pytest.mark.asyncio
async def test_cold_outreach_identity_is_rejected_before_connector_execution() -> None:
    module = _module("tool_gateway.handlers.notification_email")
    recipient_module = _module("apps.notification_worker.recipients")
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    job_id = NotificationJobId(new_id("njb"))
    identity_id = SendingIdentityId(new_id("sid"))
    transport = _Transport()
    gmail = GmailConnector(transport)
    await gmail.configure(_Secrets())
    handler = module.NotificationEmailSendHandler(
        gmail, HmacFingerprintProvider("notification-v1", b"n" * 32)
    )
    directory = recipient_module.ConfiguredNotificationRecipientDirectory.from_value(
        [
            {
                "tenant_id": str(tenant),
                "employee_id": str(employee),
                "address": "owner@example.com",
            }
        ]
    )
    actor = Actor(
        "system:notification",
        SendingIdentityScope(
            level=ScopeLevel.SYSTEM,
            allowed_identity_ids=frozenset({identity_id}),
        ),
        "system",
    )
    stage = module.NotificationEmailRateLimitCheck(
        handler,
        directory,
        _IdentityService(identity_id, role=DomainRole.COLD_OUTREACH),
        identity_id,
        actor,
    )
    context = _context(tenant, employee, job_id)
    with handler.bind(_notification(tenant, employee, job_id)):
        prepared = await handler.prepare(context, None)
        state = ToolInvocationState(module.MANIFEST, new_id("tcl"), prepared)
        rejection = await stage.check(context, state)
        assert rejection is not None
        assert rejection.stage == "rate_limit"
    assert transport.searches == 0 and transport.sends == []


@pytest.mark.asyncio
async def test_email_channel_validates_job_binding_and_delegates_without_exposing_notification() -> None:
    module = _module("notification_gateway.channels.email")
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    notification = _notification(tenant, employee, NotificationJobId(new_id("njb")))
    sent: list[Notification] = []

    class Sender:
        async def send(self, item: Notification) -> None:
            sent.append(item)

    channel = module.EmailNotificationChannel(Sender())
    assert channel.name == "email"
    await channel.deliver(notification)
    assert sent == [notification]
    with pytest.raises(PolicyViolation):
        await channel.deliver(SimpleNamespace())
