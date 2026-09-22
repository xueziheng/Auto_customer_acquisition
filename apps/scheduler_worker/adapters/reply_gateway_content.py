"""本进程独立 Message Gateway 组合；只在成功授权交付后解析内容。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import BoundedRawArtifactStore
from domains.conversations.service import ConversationService
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import MessageId, TenantId, new_id
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
from workflows.reply_qualification.ports import ReplyMessageContent

from .message_content_reader import project_reply_content
from .reply_current_access import CurrentReplyAccess


class _Tenant:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        self.tenant_id = tenant_id

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if ctx.tenant_id != self.tenant_id or ctx.tool_id != MANIFEST.tool_id:
            return CheckRejection(
                "tenant", "tenant:resource_binding", "回复消息绑定拒绝"
            )
        return None


class GatewayReplyContentReader:
    """qualify ∩ Inbox evidence；借用原 store，读取与审计仍由原 Gateway 完成。"""

    def __init__(
        self,
        tenant_id: TenantId,
        sessions: async_sessionmaker[AsyncSession],
        conversations: ConversationService,
        store: BoundedRawArtifactStore,
        access: CurrentReplyAccess,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._access = access
        handler = InboxEvidenceHandler(tenant_id, conversations, store)
        registry = ToolRegistry()
        registry.register(MANIFEST, handler)
        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]  # 原 registry 协变契约
            {
                "tenant": _Tenant(tenant_id),
                "permission": PermissionCheck(handler.allowed),
            },
            lambda tenant: cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(sessions, tenant, now=now),
            ),
            lease_duration=timedelta(seconds=60),
            lease_owner="scheduler-reply-evidence",
            now=now,
            id_factory=new_id,
        )
        self._reader = ToolGatewayInboxEvidenceReader(gateway, handler)

    async def load(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> ReplyMessageContent:
        """当前 active boss 先授权，成功 ledger 与读后重核后才取得本次 bytes。"""
        actor = await self._access.actor(tenant_id)
        raw = await self._reader.read(tenant_id, message_id, actor=actor)
        return project_reply_content(raw, max_subject_chars=4096, max_body_chars=65536)
