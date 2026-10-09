"""云端本人 Gmail 的请求级装配；凭证交换和发信始终经独立 Gateway。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.gmail.client import GmailConnector
from connectors.gmail.send_oauth import GmailOAuthTokenSource
from connectors.gmail.transport import GmailApiHttpTransport
from connectors.gmail.web_oauth import GmailWebOAuth
from domains.conversations.gmail_connection import (
    GmailActor,
    GmailAuthorizationComplete,
    GmailAuthorizationStart,
    GmailConnectionStatus,
    GmailTestCommand,
    GmailTestResult,
    GmailTestTemplate,
    require_gmail_test_role,
)
from domains.conversations.mailbox import MailboxActor
from infra.db.gmail_web_access import SqlGmailWebAccess
from infra.db.mailbox import SqlMailboxRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.gmail_web_store import GmailBinding, GmailTestRecord, GmailWebStore
from infra.standalone.gmail_settings import GmailWebSettings
from shared.errors import PermissionDenied
from shared.schemas.identifiers import IdempotencyKey, UserId, new_id
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.gmail_connect import MANIFEST as CONNECT
from tool_gateway.handlers.gmail_connect import GmailConnectHandler, GmailExchange
from tool_gateway.handlers.mailbox_test import (
    BODY,
    STAGES,
    SUBJECT,
    MailTestCheck,
    MailTestGrant,
    MailTestHandler,
)
from tool_gateway.handlers.mailbox_test import MANIFEST as TEST
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork


class _ConnectCheck:
    def __init__(self, name: str, actor: GmailActor, operation_id: str, access: SqlGmailWebAccess,
                 store: GmailWebStore):
        self.name, self.actor, self.operation_id, self.access = name, actor, operation_id, access
        self.store = store

    async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
        del state
        if (ctx.tenant_id != self.actor.tenant_id or ctx.user_id != self.actor.employee_id
                or ctx.tool_id != CONNECT.tool_id
                or dict(ctx.params) != {"operation_id": self.operation_id}
                or ctx.idempotency_key != "gmail-connect:" + self.operation_id):
            return CheckRejection(self.name, "gmail_connection:denied", "邮箱授权范围无效")
        await self.access.role(self.actor)
        if self.name == "rate_limit" and not self.store.connection_allowed(
            self.actor, self.operation_id, datetime.now(UTC),
        ):
            return CheckRejection(self.name, "gmail_connection:limited", "邮箱授权请求已过期或超出限额")
        return None


class _TestPolicy:
    def __init__(self, store: GmailWebStore, access: SqlGmailWebAccess, record: GmailTestRecord):
        self.store, self.access, self.record = store, access, record

    async def check(self, stage: str, grant: MailTestGrant) -> bool:
        del stage
        record = self.store.test_record(self.record.actor, self.record.request_id)
        binding = self.store.binding(self.record.actor)
        if (record is None or record.grant != grant or binding is None
                or binding.operation_id != record.binding_operation_id
                or binding.email != grant.sender):
            return False
        return await self.access.test_allowed(record.actor, grant.sender, grant.recipient)


class _MailboxSecrets:
    def __init__(self, source: GmailOAuthTokenSource):
        self.source = source

    def resolve(self, secret_ref: str) -> str:
        if secret_ref != "GMAIL_OAUTH_TOKEN_REF":
            raise PermissionDenied("邮箱凭证引用无效")
        return self.source.token()


class GmailWebService:
    """身份、连接和单次诊断的受信协调器；不提供模型调用接口。"""

    def __init__(self, settings: GmailWebSettings, sessions: async_sessionmaker[AsyncSession],
                 fingerprints: HmacFingerprintProvider):
        self.settings, self.sessions, self.fingerprints = settings, sessions, fingerprints
        self.store = GmailWebStore(settings.root)
        self.access = SqlGmailWebAccess(sessions)
        self.mailboxes = SqlMailboxRepository(sessions)
        self.connector = GmailWebOAuth(settings.client_file, settings.redirect_uri)

    @staticmethod
    def _mail_actor(actor: GmailActor) -> MailboxActor:
        return MailboxActor(tenant_id=actor.tenant_id, employee_id=actor.employee_id)

    async def _binding(self, actor: GmailActor) -> GmailBinding:
        await self.access.role(actor)
        binding = self.store.binding(actor)
        if binding is None:
            raise PermissionDenied("请先完成 Gmail 授权")
        checkpoint = await self.mailboxes.checkpoint(self._mail_actor(actor), binding.mailbox_id)
        if checkpoint.email != binding.email:
            raise PermissionDenied("邮箱绑定已失效")
        return binding

    async def status(self, actor: GmailActor) -> GmailConnectionStatus:
        role = await self.access.role(actor)
        binding = self.store.binding(actor)
        if binding is not None:
            binding = await self._binding(actor)
        return GmailConnectionStatus(configured=True,
                                     email=binding.email if binding else None,
                                     mailbox_id=binding.mailbox_id if binding else None,
                                     can_test=binding is not None and role == "boss")

    async def start(self, actor: GmailActor, session: SecretStr, email: str) -> GmailAuthorizationStart:
        await self.access.role(actor)
        pending = self.store.begin(actor, session.get_secret_value(), email, datetime.now(UTC))
        return GmailAuthorizationStart(authorization_url=self.connector.authorization_url(
            pending.state, pending.verifier, pending.email,
        ))

    async def complete(self, actor: GmailActor, session: SecretStr,
                       body: GmailAuthorizationComplete) -> GmailConnectionStatus:
        await self.access.role(actor)
        pending = self.store.claim(actor, session.get_secret_value(),
                                   body.state.get_secret_value(), datetime.now(UTC))
        destination = self.store.credentials_file(pending.operation_id)
        request = GmailExchange(actor.tenant_id, actor.employee_id, pending.operation_id,
                                pending.email, body.code, pending.verifier, pending.state, destination)
        registry = ToolRegistry()
        registry.register(CONNECT, GmailConnectHandler(request, self.connector, self.fingerprints))
        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            {name: _ConnectCheck(name, actor, pending.operation_id, self.access, self.store) for name in CONNECT.checks},
            lambda tenant: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self.sessions, tenant)),
            lease_duration=timedelta(seconds=120), lease_owner="web_gmail_connect", id_factory=new_id,
        )
        published = False
        try:
            result = await gateway.invoke(ToolCallContext(
                tenant_id=actor.tenant_id, user_id=UserId(actor.employee_id), tool_id=CONNECT.tool_id,
                params={"operation_id": pending.operation_id},
                idempotency_key=IdempotencyKey("gmail-connect:" + pending.operation_id),
            ))
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise PermissionDenied("Gmail 授权未完成，请重新连接并选择完整的读取和发送权限")
            await self.access.role(actor)
            mailbox_id = await self.mailboxes.register(self._mail_actor(actor), pending.email)
            self.store.publish(pending, mailbox_id, datetime.now(UTC))
            published = True
            return await self.status(actor)
        finally:
            if not published:
                destination.unlink(missing_ok=True)

    async def template(self, actor: GmailActor) -> GmailTestTemplate:
        await self._binding(actor)
        return GmailTestTemplate(subject=SUBJECT, body=BODY)

    async def latest_test(self, actor: GmailActor) -> GmailTestResult | None:
        await self._binding(actor)
        record = self.store.latest_test(actor)
        return record.view() if record else None

    async def send_test(self, actor: GmailActor, command: GmailTestCommand) -> GmailTestResult:
        require_gmail_test_role(await self.access.role(actor))
        binding = await self._binding(actor)
        if not await self.access.test_allowed(actor, binding.email, command.email):
            raise PermissionDenied("发件身份缺失、受限制或收件地址已被抑制")
        record = self.store.prepare_test(actor, binding, command, datetime.now(UTC))
        if record.status in {"succeeded", "duplicate"}:
            return record.view()
        policy = _TestPolicy(self.store, self.access, record)
        registry = ToolRegistry()
        transport = GmailApiHttpTransport(timeout_seconds=30)
        gmail = GmailConnector(transport)
        source = GmailOAuthTokenSource(self.store.credentials_file(binding.operation_id), binding.email)
        registry.register(TEST, MailTestHandler(record.grant, self.fingerprints, gmail, _MailboxSecrets(source)))
        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            {stage: MailTestCheck(stage, record.grant, policy) for stage in STAGES},
            lambda tenant: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self.sessions, tenant)),
            lease_duration=timedelta(seconds=120), lease_owner="web_mailbox_test", id_factory=new_id,
        )
        result = await gateway.invoke(ToolCallContext(
            tenant_id=actor.tenant_id, user_id=UserId(actor.employee_id), tool_id=TEST.tool_id,
            params={"grant_id": record.grant.grant_id}, idempotency_key=record.grant.key,
            approval_ref=record.grant.grant_id,
        ))
        provider_ref = (result.output or {}).get("provider_ref")
        updated = self.store.finish_test(record, result.status.value, str(result.tool_call_id),
                                         provider_ref if isinstance(provider_ref, str) else None)
        if result.status in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
            await self.mailboxes.request_sync(self._mail_actor(actor), binding.mailbox_id)
        return updated.view()
