"""真实 PostgreSQL 验证云端邮箱授权、员工隔离及网关一次执行。"""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.composition.gmail_web import GmailWebService
from domains.conversations.gmail_connection import (
    GmailActor,
    GmailAuthorizationComplete,
    GmailTestCommand,
)
from infra.db.tables import EmployeeRow
from infra.pilot.config import private_write
from infra.standalone.gmail_settings import GmailWebSettings
from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id
from tool_gateway.fingerprint import HmacFingerprintProvider


async def setup(integration_engine, tmp_path):
    tmp_path.chmod(0o700)
    actor = GmailActor(tenant_id=new_id("tn"), employee_id=new_id("emp"), user_id=new_id("usr"))
    sessions = async_sessionmaker(integration_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        db.add(EmployeeRow(tenant_id=actor.tenant_id, employee_id=actor.employee_id,
                           user_id=actor.user_id, name="合成测试管理员", role="boss", is_active=True))
    service = GmailWebService(GmailWebSettings(
        root=tmp_path, client_file=tmp_path / "unused-client.json", public_origin="https://trade.example.com",
    ), sessions, HmacFingerprintProvider("v1", b"x" * 32))
    pending = service.store.begin(actor, "synthetic-session", "owner@gmail.com", datetime.now(UTC))
    calls = []

    def exchange(code, verifier, state, destination, email):
        calls.append(email)
        assert code.get_secret_value() == "synthetic-code"
        private_write(destination, b'{"synthetic":"credentials-not-live"}')
    service.connector.exchange = exchange
    command = GmailAuthorizationComplete(code=SecretStr("synthetic-code"), state=pending.state)
    return service, actor, pending, command, calls


async def test_complete_creates_only_verified_owner_mailbox_and_no_replay(integration_engine, tmp_path):
    service, actor, pending, command, calls = await setup(integration_engine, tmp_path)
    wrong = actor.model_copy(update={"user_id": new_id("usr")})
    with pytest.raises(PermissionDenied):
        await service.complete(wrong, SecretStr("synthetic-session"), command)
    with pytest.raises(PermissionDenied):
        await service.complete(actor, SecretStr("wrong-session"), command)
    assert calls == []
    result = await service.complete(actor, SecretStr("synthetic-session"), command)
    assert result.email == "owner@gmail.com" and result.can_test
    assert result.mailbox_id
    assert service.store.binding(actor).operation_id == pending.operation_id
    assert len(await service.mailboxes.mailboxes(service._mail_actor(actor))) == 1
    with pytest.raises(PermissionDenied):
        await service.complete(actor, SecretStr("synthetic-session"), command)
    assert len(calls) == 1
    other = actor.model_copy(update={"tenant_id": new_id("tn")})
    with pytest.raises(PermissionDenied):
        await service.status(other)
    async with service.sessions.begin() as db:
        await db.execute(update(EmployeeRow).where(
            EmployeeRow.tenant_id == actor.tenant_id, EmployeeRow.employee_id == actor.employee_id,
        ).values(is_active=False))
    with pytest.raises(PermissionDenied):
        await service.status(actor)


async def test_failed_exchange_does_not_register_mailbox(integration_engine, tmp_path):
    from connectors.gmail.send_oauth import GmailSendAuthorizationError
    service, actor, pending, command, _calls = await setup(integration_engine, tmp_path)
    def rejected(*args):
        raise GmailSendAuthorizationError()
    service.connector.exchange = rejected
    with pytest.raises(PermissionDenied):
        await service.complete(actor, SecretStr("synthetic-session"), command)
    assert service.store.binding(actor) is None
    assert await service.mailboxes.mailboxes(service._mail_actor(actor)) == []
    assert not service.store.credentials_file(pending.operation_id).exists()


async def test_send_uses_real_gateway_and_preserves_same_grant(integration_engine, tmp_path, monkeypatch):
    import apps.api.composition.gmail_web as module
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
    from tests.integration.test_mailbox_test_gateway import _Secrets, _Transport
    service, actor, _, command, _calls = await setup(integration_engine, tmp_path)
    await service.complete(actor, SecretStr("synthetic-session"), command)
    identities = SendingIdentityServiceImpl(
        lambda tenant: SqlAlchemySendingIdentityUnitOfWork(service.sessions, tenant),
        Phase1SendingIdentityAuthorizer(actor.tenant_id), StandardAuditLogger(),
    )
    identity_id = await identities.register(actor.tenant_id, IdentityRegisterRequest(
        address="owner@gmail.com", domain="gmail.com", role=DomainRole.PRIMARY_BUSINESS,
    ), actor=Actor(actor.employee_id, SendingIdentityScope(level=ScopeLevel.TENANT), "boss"))
    transport = _Transport()
    monkeypatch.setattr(module, "GmailApiHttpTransport", lambda **kwargs: transport)
    monkeypatch.setattr(module, "_MailboxSecrets", lambda source: _Secrets())
    request = GmailTestCommand(email="recipient@example.com", request_id=uuid4(), confirm_my_mailbox=True)
    first = await service.send_test(actor, request)
    assert first.status == "succeeded" and first.provider_ref
    second = await service.send_test(actor, request)
    assert second == first
    assert transport.sends == 1
    assert (await service.latest_test(actor)).request_id == request.request_id
    async with service.sessions.begin() as db:
        await db.execute(update(EmployeeRow).where(
            EmployeeRow.tenant_id == actor.tenant_id, EmployeeRow.employee_id == actor.employee_id,
        ).values(role="sales"))
    with pytest.raises(PermissionDenied):
        await service.send_test(actor, request.model_copy(update={"request_id": uuid4()}))
    assert transport.sends == 1
    assert identity_id


async def test_web_worker_checks_current_user_before_any_gmail_read(integration_engine, tmp_path, monkeypatch):
    import apps.email_feedback_worker.web_mailbox as module
    from apps.email_feedback_worker.web_mailbox import sync_binding
    service, actor, _, command, _ = await setup(integration_engine, tmp_path)
    await service.complete(actor, SecretStr("synthetic-session"), command)
    binding = service.store.binding(actor)
    async with service.sessions.begin() as db:
        await db.execute(update(EmployeeRow).where(
            EmployeeRow.tenant_id == actor.tenant_id, EmployeeRow.employee_id == actor.employee_id,
        ).values(user_id=new_id("usr")))
    def no_provider(*args, **kwargs):
        pytest.fail("员工映射已改变，不得建立 Gmail reader")
    monkeypatch.setattr(module, "GmailWebMailboxHttpProvider", no_provider)
    with pytest.raises(PermissionDenied):
        await sync_binding(integration_engine, service.sessions, service.store, binding, service.fingerprints)
