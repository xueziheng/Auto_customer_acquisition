"""事务通知邮件经过真实 PostgreSQL Tool Gateway ledger 的语义。"""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.gmail.client import GmailConnector
from connectors.gmail.transport import GmailNetworkError
from domains.sending_identity.permissions import Actor, ScopeLevel, SendingIdentityScope
from domains.sending_identity.schemas import (
    DomainRole,
    IdentityState,
    IdentityView,
    SendPermission,
    SendReservation,
)
from infra.db.session import create_engine_from
from infra.db.tables import ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from notification_gateway.jobs import NotificationContext, NotificationKind
from notification_gateway.models import Notification, NotificationPriority
from shared.errors import PolicyViolation, TransientError
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationJobId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory


def _module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"RED：事务通知邮件 Gateway 尚未实现（{exc}）")


class _Secrets:
    def resolve(self, _ref: str) -> str:
        return "oauth-integration-private"


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class _Transport:
    def __init__(self, outcomes: list[str | BaseException]) -> None:
        self.outcomes = outcomes
        self.existing: str | None = None
        self.searches = 0
        self.sends = 0
        self.raw_messages: list[bytes] = []

    async def search(self, **_kwargs):
        self.searches += 1
        return self.existing

    async def send(self, *, token: str, raw_message: bytes) -> str:
        assert token == "oauth-integration-private"
        self.sends += 1
        self.raw_messages.append(raw_message)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _IdentityService:
    def __init__(self, identity_id: SendingIdentityId) -> None:
        self.identity_id = identity_id
        self.calls: list[str] = []

    async def get(self, tenant_id, identity_id, *, actor):
        del tenant_id, actor
        self.calls.append("get")
        return IdentityView(
            identity_id,
            "alerts@example.com",
            "example.com",
            DomainRole.TRANSACTIONAL,
            IdentityState.ACTIVE,
            datetime(2026, 8, 14, tzinfo=UTC),
            can_send_today=True,
            remaining_today=10,
        )

    async def check_send_permission(
        self, tenant_id, identity_id, for_cold_outreach, *, actor
    ):
        del tenant_id, identity_id, actor
        self.calls.append("check")
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
        assert for_cold_outreach is False
        return SendReservation(
            "notification-reservation",
            identity_id,
            reservation_key,
            date(2026, 8, 14),
            1,
            10,
            9,
        )


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
        f"notification:{job_id}",
        next_step="处理逾期承诺并更新下一步",
        link="/crm/commitments/safe",
        source_job_id=job_id,
    )


async def _harness(db_url: str, *, allowed: bool, outcomes: list[str | BaseException]):
    handler_module = _module("tool_gateway.handlers.notification_email")
    recipient_module = _module("apps.notification_worker.recipients")
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    identity_id = SendingIdentityId(new_id("sid"))
    job_id = NotificationJobId(new_id("njb"))
    transport = _Transport(outcomes)
    clock = _Clock()
    gmail = GmailConnector(transport)
    await gmail.configure(_Secrets())
    handler = handler_module.NotificationEmailSendHandler(
        gmail, HmacFingerprintProvider("notification-v1", b"g" * 32)
    )
    identity = _IdentityService(identity_id)
    directory = recipient_module.ConfiguredNotificationRecipientDirectory.from_value(
        [
            {
                "tenant_id": str(tenant),
                "employee_id": str(employee),
                "address": "owner@example.net",
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
    user_id = UserId(new_id("usr"))

    async def authorize(ctx, _state) -> bool:
        return allowed and ctx.tenant_id == tenant and ctx.user_id == user_id

    registry = ToolRegistry()
    registry.register(handler_module.MANIFEST, handler)
    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]
        {
            "tenant": handler_module.NotificationEmailTenantCheck(tenant),
            "permission": PermissionCheck(authorize),
            "idempotency": IdempotencyCheck(),
            "rate_limit": handler_module.NotificationEmailRateLimitCheck(
                handler, directory, identity, identity_id, actor
            ),
        },
        cast(
            ToolGatewayUnitOfWorkFactory,
            lambda requested: SqlAlchemyToolGatewayUnitOfWork(
                factory, requested, now=clock.now
            ),
        ),
        lease_duration=timedelta(seconds=5),
        lease_owner="notification-worker",
        now=clock.now,
        id_factory=new_id,
    )
    sender = handler_module.ToolGatewayTransactionalNotificationSender(
        gateway, handler, user_id
    )
    return engine, tenant, employee, job_id, transport, identity, sender, clock


@pytest.mark.asyncio
async def test_gateway_sends_once_and_persists_only_safe_notification_projection(
    db_url: str,
) -> None:
    engine, tenant, employee, job_id, transport, identity, sender, _clock = await _harness(
        db_url, allowed=True, outcomes=["gmail_notification_1"]
    )
    notification = _notification(tenant, employee, job_id)
    try:
        await sender.send(notification)
        await sender.send(notification)
        assert transport.sends == 1
        assert identity.calls == ["get", "check", "reserve"]
        async with engine.connect() as connection:
            calls = (
                await connection.execute(
                    select(
                        ToolCallRow.tool_id,
                        ToolCallRow.idempotency_key,
                        ToolCallRow.request_fingerprint,
                        ToolCallRow.provider_ref,
                    )
                )
            ).all()
            events = (
                await connection.execute(
                    select(
                        ToolCallEventRow.stage,
                        ToolCallEventRow.outcome,
                        ToolCallEventRow.rule,
                    )
                )
            ).all()
        persisted = repr((calls, events)).casefold()
        assert "notification.email.send" in persisted
        assert str(job_id).casefold() in persisted
        for marker in (
            "owner@example.net",
            "alerts@example.com",
            "承诺已逾期",
            "处理逾期承诺",
            "oauth-integration-private",
        ):
            assert marker.casefold() not in persisted
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_permission_failure_leaves_zero_identity_reservations_and_connector_writes(
    db_url: str,
) -> None:
    engine, tenant, employee, job_id, transport, identity, sender, _clock = await _harness(
        db_url, allowed=False, outcomes=["must-not-send"]
    )
    try:
        with pytest.raises(PolicyViolation):
            await sender.send(_notification(tenant, employee, job_id))
        assert identity.calls == []
        assert transport.searches == 0
        assert transport.sends == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ambiguous_write_retries_only_by_reconciliation_and_never_resends(
    db_url: str,
) -> None:
    engine, tenant, employee, job_id, transport, _identity, sender, clock = await _harness(
        db_url,
        allowed=True,
        outcomes=[GmailNetworkError(may_have_written=True)],
    )
    notification = _notification(tenant, employee, job_id)
    try:
        with pytest.raises(TransientError):
            await sender.send(notification)
        assert transport.sends == 1
        transport.existing = "gmail_notification_recovered"
        clock.advance(6)
        await sender.send(notification)
        assert transport.sends == 1
        assert transport.searches == 2
    finally:
        await engine.dispose()
