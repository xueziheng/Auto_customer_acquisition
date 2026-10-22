"""本人阿里企业邮箱连接；认证走Gateway，凭证只留受信私有存储。"""
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.conversations.aliyun_connection import (
    AliyunActor,
    AliyunConnectCommand,
    AliyunConnectionStatus,
)
from infra.aliyun_mail_store import AliyunBinding, AliyunMailStore
from infra.db.tables import EmployeeRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    IdempotencyKey,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.aliyun_connect import (
    MANIFEST,
    AliyunConnectHandler,
    MailAuthenticator,
)
from tool_gateway.handlers.email_send import OutboundMailConnector
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork


def _connector(address: str, password: SecretStr) -> MailAuthenticator:
    from connectors.aliyun_mail.client import AliyunMailConnector
    return AliyunMailConnector(address, password)


def _outbound_connector(address: str, password: SecretStr) -> OutboundMailConnector:
    from connectors.aliyun_mail.client import AliyunMailConnector
    return AliyunMailConnector(address, password)


class AliyunOutboundConnectors:
    """发送/对账时重核当前员工，仅解析其本人已连接的精确邮箱。"""

    def __init__(self, store: AliyunMailStore, sessions: async_sessionmaker[AsyncSession], *,
        connector_factory: Callable[[str, SecretStr], OutboundMailConnector] = _outbound_connector):
        self.store, self.sessions, self.connector_factory = store, sessions, connector_factory

    async def resolve(self, tenant_id: TenantId, employee_id: EmployeeId, from_address: str) -> OutboundMailConnector:
        async with self.sessions() as s:
            user_id = await s.scalar(select(EmployeeRow.user_id).where(
                EmployeeRow.tenant_id == tenant_id, EmployeeRow.employee_id == employee_id,
                EmployeeRow.is_active.is_(True), EmployeeRow.role.in_(("boss", "manager", "sales")),
            ))
        if user_id is None:
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        actor = AliyunActor(tenant_id=tenant_id, employee_id=employee_id, user_id=UserId(user_id))
        binding = self.store.binding(actor)
        if binding is None or binding.email != from_address:
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
        return self.connector_factory(binding.email, binding.password)


class _ConnectCheck:
    def __init__(self, name: str, service: "AliyunWebService", actor: AliyunActor, operation: str):
        self.name, self.service, self.actor, self.operation = name, service, actor, operation

    async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
        if (ctx.tenant_id != self.actor.tenant_id or ctx.user_id != self.actor.employee_id
            or ctx.tool_id != self.service.manifest.tool_id or dict(ctx.params) != {"operation_id": self.operation}
            or ctx.idempotency_key != "aliyun-connect:" + self.operation):
            return CheckRejection(self.name, "mailbox:denied", "邮箱授权范围无效")
        await self.service.current(self.actor)
        if self.name == "rate_limit":
            async with self.service.sessions() as s:
                count = await s.scalar(select(func.count()).select_from(ToolCallRow).where(
                    ToolCallRow.tenant_id == self.actor.tenant_id,
                    ToolCallRow.user_id == self.actor.employee_id,
                    ToolCallRow.tool_id == self.service.manifest.tool_id,
                    ToolCallRow.created_at > datetime.now(UTC) - timedelta(hours=1),
                ))
            if count is None or count > 10:
                return CheckRejection(self.name, "mailbox:limited", "邮箱连接尝试过于频繁，请稍后重试")
        return None


class AliyunWebService:
    manifest = MANIFEST
    provider_label = "阿里邮箱"

    def __init__(self, root: Path, sessions: async_sessionmaker[AsyncSession],
        fingerprints: HmacFingerprintProvider, *,
        connector_factory: Callable[[str, SecretStr], MailAuthenticator] = _connector):
        self.store, self.sessions, self.fingerprints = AliyunMailStore(root), sessions, fingerprints
        self.connector_factory = connector_factory

    async def current(self, actor: AliyunActor) -> None:
        async with self.sessions() as s:
            role = await s.scalar(select(EmployeeRow.role).where(
                EmployeeRow.tenant_id == actor.tenant_id, EmployeeRow.employee_id == actor.employee_id,
                EmployeeRow.user_id == actor.user_id, EmployeeRow.is_active.is_(True),
            ))
        if role not in {"boss", "manager", "sales"}:
            raise PermissionDenied("当前账号不能连接或使用该邮箱")

    async def status(self, actor: AliyunActor) -> AliyunConnectionStatus:
        await self.current(actor)
        binding = self.store.binding(actor)
        return AliyunConnectionStatus(configured=True, email=binding.email if binding else None,
            connected_at=binding.connected_at if binding else None,
            smtp_authenticated=binding is not None, imap_authenticated=binding is not None)

    async def connect(self, actor: AliyunActor, body: AliyunConnectCommand) -> AliyunConnectionStatus:
        await self.current(actor)
        async with self.sessions() as s, s.begin():
            # 身份锁同时串行化本人重连；不持有Gateway写入涉及的行。
            employee = await s.scalar(select(EmployeeRow.employee_id).where(
                EmployeeRow.tenant_id == actor.tenant_id, EmployeeRow.employee_id == actor.employee_id,
                EmployeeRow.user_id == actor.user_id, EmployeeRow.is_active.is_(True),
                EmployeeRow.role.in_(("boss", "manager", "sales")),
            ).with_for_update(key_share=True))
            if employee is None:
                raise PermissionDenied("邮箱授权身份已改变")
            previous = self.store.binding(actor)
            if previous is not None and previous.email != body.email:
                raise ValidationError("请使用当前已绑定地址重新连接")
            operation = new_id("aco")
            registry = ToolRegistry()
            registry.register(self.manifest, AliyunConnectHandler(actor, operation,
                self.connector_factory(body.email, body.password), self.fingerprints))
            # 既有registry只读协议的类型不协变，沿用其他发送插件的装配边界。
            gateway = ToolGateway(registry,  # type: ignore[arg-type]
                {name: _ConnectCheck(name, self, actor, operation) for name in self.manifest.checks},
                lambda tenant: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self.sessions, tenant)),
                lease_duration=timedelta(seconds=120), lease_owner="web-aliyun-connect", id_factory=new_id,
            )
            result = await gateway.invoke(ToolCallContext(tenant_id=actor.tenant_id,
                user_id=UserId(actor.employee_id), tool_id=self.manifest.tool_id,
                params={"operation_id": operation}, idempotency_key=IdempotencyKey("aliyun-connect:" + operation)))
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ValidationError(f"{self.provider_label}连接未完成，请检查授权凭证和SMTP/IMAP权限；不会自动重试")
            self.store.publish(AliyunBinding(actor=actor, email=body.email, password=body.password,
                connected_at=datetime.now(UTC), operation_id=operation))
        return await self.status(actor)
