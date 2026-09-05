"""ADR0026唯一具名入站机械组合；各进程显式拥有独立对象与生命周期。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from artifact_store.repository import ArtifactUnitOfWork
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.gmail.client import SecretResolver
from connectors.gmail.inbound import GmailInboundReader
from connectors.gmail.inbound_cursor import initial_inbound_cursor
from connectors.gmail.inbound_transport import GmailInboundHttpTransport
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from domains.employees.service import Actor as EmployeeActor
from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    ScopeLevel,
    StandardAuditLogger,
)
from domains.sending_identity.service import SendingIdentityService
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.email_feedback_uow import OutreachServiceBuilder
from infra.db.email_inbound_uow import SqlAlchemyInboundPageUnitOfWork
from infra.db.repositories.email_inbound import InboundStore
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.email_inbound_artifacts import InboundRawArtifactArchiver
from shared.schemas.email_inbound import (
    MIME_BYTES,
    InboundDTO,
    InboundError,
    InboundRoute,
)
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import (
    EmployeeId,
    SendingIdentityId,
    TenantId,
    UserId,
    new_id,
)
from shared.schemas.quote_facts import QuoteEmployeeFact
from tool_gateway.checks.email_inbound import InboundTenantCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_inbound import (
    MANIFEST,
    EmailInboundFetchHandler,
    ToolGatewayEmailInboundReader,
)
from tool_gateway.handlers.email_inbound_raw import (
    RAW_MANIFEST,
    EmailInboundRawHandler,
    InboundRawSlot,
    ToolGatewayInboundRawReader,
)
from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.reply_qualification.inbound import InboundPageProcessor
from workflows.reply_qualification.inbound_contracts import (
    InboundCursor,
    InboundPageTransaction,
)
from workflows.reply_qualification.inbound_management import InboundManagement

from .employee_readers import EmployeeServiceScope


class InboundMailbox(InboundDTO):
    tenant_id: TenantId
    mailbox_alias: str
    route_id: str
    config_version: str

    def route(self, identity: SendingIdentityId) -> InboundRoute:
        return InboundRoute(
            tenant_id=self.tenant_id,
            mailbox_alias=self.mailbox_alias,
            route_id=self.route_id,
            config_version=self.config_version,
            configured_identity_id=identity,
        )


@dataclass(frozen=True, repr=False)
class InboundRuntimePorts:
    """由进程入口显式提供的唯一邮箱外部端口，不读取环境或建立业务服务。"""

    profile: InboundMailbox
    provider: GmailInboundHttpTransport
    secret_resolver: SecretResolver
    secret_ref: str
    object_settings: S3ObjectStoreSettings
    fingerprint_key_ref: str
    lease_owner: str


class _Employees:
    def __init__(self, scope: EmployeeServiceScope, actor: EmployeeActor):
        self._scope, self._actor = scope, actor

    async def read(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> QuoteEmployeeFact:
        async with self._scope(tenant_id) as service:
            employee = await service.get_employee(
                tenant_id, employee_id, actor=self._actor
            )
        return QuoteEmployeeFact.model_validate(
            {
                "tenant_id": employee.tenant_id,
                "employee_id": employee.employee_id,
                "role": employee.role,
                "is_active": employee.is_active,
                "manager_id": employee.manager_id,
                "team_id": employee.team_id,
            }
        )


class _RawTenant:
    name = "tenant"

    def __init__(self, tenant: TenantId):
        self._tenant = tenant

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if ctx.tenant_id != self._tenant or ctx.tool_id != RAW_MANIFEST.tool_id:
            return CheckRejection(
                "tenant", "tenant:resource_binding", "入站原件租户不匹配"
            )
        return None


@dataclass(frozen=True)
class InboundComposition:
    management: InboundManagement
    raw: ToolGatewayInboundRawReader
    store: InboundStore
    processor: InboundPageProcessor
    reader_for: Callable[[InboundRoute], ToolGatewayEmailInboundReader]
    objects: DeferredS3ObjectBlobTransport

    async def aclose(self) -> None:
        await self.objects.aclose()


def build_inbound_composition(
    profile: InboundMailbox,
    factory: async_sessionmaker[AsyncSession],
    *,
    sending_identities: SendingIdentityService,
    employees: EmployeeServiceScope,
    employee_actor: EmployeeActor,
    outreach_builder: OutreachServiceBuilder,
    provider: GmailInboundHttpTransport,
    secret_resolver: SecretResolver,
    secret_ref: str,
    object_settings: S3ObjectStoreSettings,
    fingerprint_key_ref: str,
    lease_owner: str,
    now: Callable[[], datetime],
) -> InboundComposition:
    """构造无IO；只注册本批两个具名工具，各调用共享代码而非运行实例。"""
    tenant = profile.tenant_id
    fingerprints = HmacFingerprintProvider(
        "v1", secret_resolver.resolve(fingerprint_key_ref).encode()
    )
    objects = DeferredS3ObjectBlobTransport(object_settings, secret_resolver)
    raw_store = RawArtifactStoreImpl(
        lambda t: cast(ArtifactUnitOfWork, SqlAlchemyArtifactUnitOfWork(factory, t)),
        objects,
        MIME_BYTES,
        now,
        new_id,
        bounded_transport=S3BoundedObjectBlobTransport(
            object_settings,
            secret_resolver,
            limits=ObjectReadLimits(
                connect_timeout_ms=2000,
                read_timeout_ms=2000,
                total_timeout_ms=10000,
                maximum_attempts=1,
                chunk_bytes=65536,
            ),
        ),
    )
    store = InboundStore(factory, tenant, profile.mailbox_alias, now=now)

    def initial(
        sid: SendingIdentityId, stamp: datetime
    ) -> tuple[InboundRoute, str, int]:
        route = profile.route(sid)
        after = int((stamp - timedelta(days=30)).timestamp())
        return route, initial_inbound_cursor(route, stamp, after), after

    management = InboundManagement(
        tenant,
        store,
        _Employees(employees, employee_actor),
        sending_identities,
        initial,
        now=now,
    )
    slot = InboundRawSlot()
    registry = ToolRegistry()
    registry.register(RAW_MANIFEST, EmailInboundRawHandler(management, raw_store, slot))

    async def raw_allowed(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        if (
            ctx.tenant_id != tenant
            or state.manifest.tool_id != RAW_MANIFEST.tool_id
            or not isinstance(ctx.params.get("review_id"), str)
        ):
            return False
        await management.authorize_raw(
            tenant, EmployeeId(str(ctx.user_id)), str(ctx.params["review_id"])
        )
        return True

    raw_gateway = ToolGateway(
        registry,  # type: ignore[arg-type]  # 原Gateway registry协变契约
        {"tenant": _RawTenant(tenant), "permission": PermissionCheck(raw_allowed)},
        lambda t: cast(
            ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(factory, t, now=now)
        ),
        lease_duration=timedelta(seconds=60),
        lease_owner=lease_owner,
        now=now,
        id_factory=new_id,
    )

    def reader_for(route: InboundRoute) -> ToolGatewayEmailInboundReader:
        if route != profile.route(route.configured_identity_id):
            raise InboundError()
        page_slot = InboundPageSlot()
        user = UserId(new_id("usr"))
        page_registry = ToolRegistry()
        page_registry.register(
            MANIFEST,
            EmailInboundFetchHandler(
                route,
                lambda: GmailInboundReader(
                    route, provider, secret_resolver, secret_ref
                ),
                InboundRawArtifactArchiver(raw_store, raw_store),
                CredentialMarkerGuard(),
                page_slot,
                fingerprints,
            ),
        )

        async def allowed(ctx: ToolCallContext, state: ToolInvocationState) -> bool:
            current = await store.read_cursor()
            return (
                ctx.user_id == user
                and ctx.tenant_id == tenant
                and state.manifest.tool_id == MANIFEST.tool_id
                and current is not None
                and current.route == route
            )

        gateway = ToolGateway(
            page_registry,  # type: ignore[arg-type]  # 原Gateway registry协变契约
            {
                "tenant": InboundTenantCheck(route),
                "permission": PermissionCheck(allowed),
            },
            lambda t: cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(factory, t, now=now),
            ),
            lease_duration=timedelta(seconds=60),
            lease_owner=lease_owner,
            now=now,
            id_factory=new_id,
        )
        return ToolGatewayEmailInboundReader(gateway, page_slot, user, route)

    def actor(sid: SendingIdentityId) -> Actor:
        return Actor(
            "system:email-inbound",
            OutreachScope(
                level=ScopeLevel.SYSTEM, allowed_sending_identity_ids=frozenset({sid})
            ),
            "system",
        )

    def page_uow(
        expected: InboundCursor,
    ) -> AbstractAsyncContextManager[InboundPageTransaction]:
        return cast(
            AbstractAsyncContextManager[InboundPageTransaction],
            SqlAlchemyInboundPageUnitOfWork(
                store,
                expected,
                outreach_builder=outreach_builder,
                audit_sink=StandardAuditLogger(),
            ),
        )

    return InboundComposition(
        management,
        ToolGatewayInboundRawReader(raw_gateway, slot, management),
        store,
        InboundPageProcessor(store, page_uow, actor),
        reader_for,
        objects,
    )
