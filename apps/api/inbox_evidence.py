"""API独立Message原件Gateway装配；资源仍由原入站组合持有。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import BoundedRawArtifactStore
from domains.conversations.service import ConversationService
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import TenantId, new_id
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.handlers.inbox_evidence import (
    MANIFEST,
    InboxEvidenceHandler,
    ToolGatewayInboxEvidenceReader,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.repository import ToolGatewayUnitOfWork


class _Tenant:
    name = "tenant"

    def __init__(self, tenant: TenantId):
        self.tenant = tenant

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if ctx.tenant_id != self.tenant or ctx.tool_id != MANIFEST.tool_id:
            return CheckRejection("tenant", "tenant:resource_binding", "收件箱访问拒绝")
        return None


def build_inbox_evidence_reader(
    tenant: TenantId,
    factory: async_sessionmaker[AsyncSession],
    conversations: ConversationService,
    store: BoundedRawArtifactStore,
    *,
    now: Callable[[], datetime],
) -> ToolGatewayInboxEvidenceReader:
    """只注册本插件，复用已配置的bounded store，不读取环境或构造凭证。"""
    handler = InboxEvidenceHandler(tenant, conversations, store)
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]  # 原registry协变契约
        {"tenant": _Tenant(tenant), "permission": PermissionCheck(handler.allowed)},
        lambda t: cast(
            ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(factory, t, now=now)
        ),
        lease_duration=timedelta(seconds=60),
        lease_owner="api-inbox-evidence",
        now=now,
        id_factory=new_id,
    )
    return ToolGatewayInboxEvidenceReader(gateway, handler)
