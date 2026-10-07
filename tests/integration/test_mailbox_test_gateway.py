"""本人测试经真实 PostgreSQL Gateway：精确授权、一次发送与不确定结果只读对账。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.gmail.client import GmailConnector
from connectors.gmail.transport import GmailNetworkError
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.mailbox_test import (
    MANIFEST,
    STAGES,
    MailTestCheck,
    MailTestGrant,
    MailTestHandler,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallContext, ToolGateway

_NOW = datetime(2026, 10, 5, tzinfo=UTC)


class _Policy:
    denied: str | None = None

    async def check(self, stage, grant):
        return stage != self.denied


class _Secrets:
    calls = 0

    def resolve(self, reference):
        self.calls += 1
        assert reference == "GMAIL_OAUTH_TOKEN_REF"
        return "controlled-test-value"


class _Transport:
    sends = 0
    searches = 0
    ref = None
    uncertain = False

    async def search(self, **kwargs):
        self.searches += 1
        return self.ref

    async def send(self, **kwargs):
        self.sends += 1
        self.ref = "controlled_mail_test_ref"
        if self.uncertain:
            raise GmailNetworkError(may_have_written=True)
        return self.ref

    async def close(self):
        pass


def _setup(engine):
    grant = MailTestGrant(
        new_id("mtg"),
        TenantId(new_id("tn")),
        new_id("emp"),
        "sender@example.test",
        "owner@example.test",
        _NOW,
        _NOW + timedelta(minutes=45),
    )
    transport, secrets, policy = _Transport(), _Secrets(), _Policy()
    clock = [_NOW]
    handler = MailTestHandler(
        grant,
        HmacFingerprintProvider("v1", b"x" * 32),
        GmailConnector(transport),
        secrets,
    )
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    gateway = ToolGateway(
        registry,
        {
            name: MailTestCheck(name, grant, policy, now=lambda: clock[0])
            for name in STAGES
        },
        lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
            sessions, tenant, now=lambda: clock[0]
        ),
        lease_duration=timedelta(seconds=120),
        lease_owner="test_mailbox",
        now=lambda: clock[0],
        id_factory=new_id,
    )
    ctx = ToolCallContext(
        grant.tenant_id,
        UserId(grant.employee_id),
        MANIFEST.tool_id,
        {"grant_id": grant.grant_id},
        idempotency_key=grant.key,
        approval_ref=grant.grant_id,
    )
    return gateway, ctx, transport, secrets, policy, clock


async def test_replaying_approved_test_sends_only_once(integration_engine):
    gateway, ctx, transport, secrets, _, _ = _setup(integration_engine)
    first = await gateway.invoke(ctx)
    assert first.status is ToolCallStatus.SUCCEEDED, (
        first.status,
        first.error_category,
        first.rejected,
    )
    second = await gateway.invoke(ctx)
    assert second.status is ToolCallStatus.DUPLICATE
    assert transport.sends == 1
    assert secrets.calls == 1
    assert second.output["provider_ref"] == first.output["provider_ref"]


@pytest.mark.parametrize(
    "mutation", ["tenant", "user", "key", "body", "approval", "campaign"]
)
async def test_changed_scope_rejected_before_credentials(integration_engine, mutation):
    gateway, ctx, transport, secrets, _, _ = _setup(integration_engine)
    changes = {
        "tenant": {"tenant_id": TenantId(new_id("tn"))},
        "user": {"user_id": UserId(new_id("usr"))},
        "key": {"idempotency_key": IdempotencyKey("another-key")},
        "body": {"params": {**ctx.params, "body": "not a test"}},
        "approval": {"approval_ref": None},
        "campaign": {"campaign_ref": "campaign"},
    }
    result = await gateway.invoke(replace(ctx, **changes[mutation]))
    assert result.status is ToolCallStatus.REJECTED
    assert transport.sends == transport.searches == secrets.calls == 0


@pytest.mark.parametrize(
    "stage", ["permission", "suppression", "approval", "rate_limit"]
)
async def test_current_policy_rejection_prevents_any_provider_call(
    integration_engine, stage
):
    gateway, ctx, transport, secrets, policy, _ = _setup(integration_engine)
    policy.denied = stage
    result = await gateway.invoke(ctx)
    if stage == "rate_limit":
        assert result.status is ToolCallStatus.FAILED_PERMANENT
        assert result.error_category is ToolErrorCategory.PERMISSION_DENIED
    else:
        assert result.status is ToolCallStatus.REJECTED
        assert result.rejected.stage == stage
    assert transport.sends == transport.searches == secrets.calls == 0


async def test_expired_grant_prevents_provider_call(integration_engine):
    gateway, ctx, transport, secrets, _, clock = _setup(integration_engine)
    clock[0] += timedelta(hours=1)
    result = await gateway.invoke(ctx)
    assert result.status is ToolCallStatus.REJECTED
    assert transport.sends == transport.searches == secrets.calls == 0


async def test_uncertain_delivery_only_searches_on_recovery(integration_engine):
    gateway, ctx, transport, _, _, clock = _setup(integration_engine)
    transport.uncertain = True
    first = await gateway.invoke(ctx)
    assert first.error_category is ToolErrorCategory.RECONCILIATION_REQUIRED
    clock[0] += timedelta(minutes=3)
    second = await gateway.invoke(ctx)
    assert second.status is ToolCallStatus.SUCCEEDED
    assert transport.sends == 1
    assert transport.searches == 2


async def test_cli_policy_reads_current_owner_and_private_grant(
    integration_engine, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from sqlalchemy import update

    from domains.sending_identity.permissions import (
        Actor,
        Phase1SendingIdentityAuthorizer,
        ScopeLevel,
        SendingIdentityScope,
        StandardAuditLogger,
    )
    from domains.sending_identity.schemas import DomainRole, IdentityRegisterRequest
    from domains.sending_identity.service_impl import SendingIdentityServiceImpl
    from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
    from infra.db.tables import EmployeeRow
    from infra.pilot.config import PilotConfig, private_write
    from scripts.send_mailbox_test import CurrentPolicy, encode

    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    grant = MailTestGrant(
        new_id("mtg"),
        TenantId(new_id("tn")),
        new_id("emp"),
        "sender@example.test",
        "owner@example.test",
        _NOW,
        _NOW + timedelta(minutes=45),
    )
    path = tmp_path / "grant.json"
    tmp_path.chmod(0o700)
    private_write(path, encode(grant))
    config = SimpleNamespace(
        tenant_id=grant.tenant_id,
        gmail=SimpleNamespace(address=grant.sender, employee_id=grant.employee_id),
    )
    monkeypatch.setattr(PilotConfig, "read", lambda path: config)
    service = SendingIdentityServiceImpl(
        lambda tenant: SqlAlchemySendingIdentityUnitOfWork(
            sessions, tenant, now=lambda: _NOW
        ),
        Phase1SendingIdentityAuthorizer(grant.tenant_id),
        StandardAuditLogger(),
        now=lambda: _NOW,
    )
    boss = Actor("test_operator", SendingIdentityScope(level=ScopeLevel.TENANT), "boss")
    identity_id = await service.register(
        grant.tenant_id,
        IdentityRegisterRequest(
            address=grant.sender, domain="example.test", role=DomainRole.COLD_OUTREACH
        ),
        actor=boss,
    )
    async with sessions.begin() as session:
        session.add(
            EmployeeRow(
                employee_id=grant.employee_id,
                tenant_id=grant.tenant_id,
                name="Test owner",
                role="boss",
                is_active=True,
            )
        )
    policy = CurrentPolicy(sessions, tmp_path / "config.json", path)
    assert await policy.check("permission", grant)
    assert await policy.check("suppression", grant)
    private_write(path, encode(replace(grant, recipient="different@example.test")))
    assert not await policy.check("approval", grant)
    private_write(path, encode(grant))
    async with sessions.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == grant.tenant_id,
                EmployeeRow.employee_id == grant.employee_id,
            )
            .values(is_active=False)
        )
    assert not await policy.check("permission", grant)
    async with sessions.begin() as session:
        await session.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == grant.tenant_id,
                EmployeeRow.employee_id == grant.employee_id,
            )
            .values(is_active=True)
        )
    await service.retire(
        grant.tenant_id, identity_id, "测试退役身份拒绝诊断发送", actor=boss
    )
    assert not await policy.check("rate_limit", grant)
