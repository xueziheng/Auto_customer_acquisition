"""Message原件插件：受信调用snapshot，当前权限前后复核，一次性有界bytes交接。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from artifact_store.store import BoundedRawArtifactStore, RawArtifactKind
from domains.conversations.schemas import InboxEvidenceRef
from domains.conversations.service import ConversationService, InboxActor
from shared.errors import PermissionDenied
from shared.schemas.email_inbound import MIME_BYTES, InboundError
from shared.schemas.identifiers import ArtifactId, MessageId, TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    PreparedToolCall,
    SafeScalar,
    ToolCallContext,
    ToolCallResult,
    ToolInvocationState,
)

MANIFEST = ToolManifest(
    tool_id="inbox.message.evidence.read",
    version="v1",
    description="按当前客户负责人授权下载消息原件",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("inbox:evidence_read",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["provider_ref"],
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
)


class Invoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


@dataclass(frozen=True, repr=False)
class _Call:
    task: asyncio.Task[Any]
    actor: InboxActor
    message_id: MessageId
    handle: str


@dataclass(frozen=True, repr=False)
class _Result:
    call: _Call
    reference: InboxEvidenceRef
    content: bytes = field(repr=False)


class InboxEvidenceHandler:
    """槽位绑定task，不继承给子task，也不把actor/正文写入Gateway参数或账本。"""

    def __init__(
        self,
        tenant: TenantId,
        conversations: ConversationService,
        store: BoundedRawArtifactStore,
    ):
        self.tenant, self.conversations, self.store = tenant, conversations, store
        self._call: ContextVar[_Call | None] = ContextVar(
            "inbox_evidence_call", default=None
        )
        self._result: ContextVar[_Result | None] = ContextVar(
            "inbox_evidence_result", default=None
        )

    def current(self) -> _Call:
        call = self._call.get()
        if call is None or call.task is not asyncio.current_task():
            raise PermissionDenied("收件箱访问拒绝")
        return call

    async def allowed(self, ctx: ToolCallContext, state: ToolInvocationState) -> bool:
        call = self.current()
        if (
            ctx.tenant_id != self.tenant
            or ctx.user_id != call.actor.employee_id
            or state.manifest.tool_id != MANIFEST.tool_id
            or dict(ctx.params) != {"message_id": call.message_id}
        ):
            return False
        await self.conversations.get_message_evidence(
            self.tenant, call.message_id, actor=call.actor
        )
        return True

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        call = self.current()
        if (
            ctx.tenant_id != self.tenant
            or ctx.user_id != call.actor.employee_id
            or dict(ctx.params) != {"message_id": call.message_id}
            or re.fullmatch(r"msg_[0-7][0-9A-HJKMNP-TV-Z]{25}", call.message_id) is None
        ):
            raise PermissionDenied("收件箱访问拒绝")
        digest = hashlib.sha256(
            f"{self.tenant}:{ctx.user_id}:{call.message_id}".encode()
        ).hexdigest()
        return PreparedToolCall(digest, "v1", {"message_id": call.message_id}, call)

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, SafeScalar]:
        call = self.current()
        if tenant_id != self.tenant or prepared.payload is not call:
            raise PermissionDenied("收件箱访问拒绝")
        reference = await self.conversations.get_message_evidence(
            tenant_id, call.message_id, actor=call.actor
        )
        meta, content = await self.store.get_bounded(
            tenant_id, ArtifactId(reference.artifact_id), maximum_bytes=MIME_BYTES
        )
        if (
            meta.tenant_id != tenant_id
            or str(meta.artifact_id) != reference.artifact_id
            or meta.kind is not RawArtifactKind.EMAIL_RAW
            or meta.mime_type != "message/rfc822"
            or not 0 < len(content) <= MIME_BYTES
            or len(content) != meta.size_bytes
            or hashlib.sha256(content).hexdigest() != meta.content_hash
        ):
            raise InboundError()
        if (
            await self.conversations.get_message_evidence(
                tenant_id, call.message_id, actor=call.actor
            )
            != reference
        ):
            raise PermissionDenied("收件箱访问拒绝")
        self._result.set(_Result(call, reference, content))
        return {"provider_ref": call.handle}


class ToolGatewayInboxEvidenceReader:
    def __init__(self, gateway: Invoker, handler: InboxEvidenceHandler):
        self._gateway, self._handler = gateway, handler

    async def read(
        self, tenant_id: TenantId, message_id: MessageId, *, actor: InboxActor
    ) -> bytes:
        """本次快照只活在调用上下文，最终复核后领取并无条件清空。"""
        task = asyncio.current_task()
        if (
            task is None
            or self._handler._call.get() is not None
            or tenant_id != self._handler.tenant
        ):
            raise PermissionDenied("收件箱访问拒绝")
        await self._handler.conversations.get_message_evidence(
            tenant_id, message_id, actor=actor
        )
        call = _Call(task, actor, message_id, new_id("ieb"))
        token = self._handler._call.set(call)
        try:
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id=tenant_id,
                    user_id=UserId(actor.employee_id),
                    tool_id=MANIFEST.tool_id,
                    params={"message_id": message_id},
                )
            )
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED
                )
            entry = self._handler._result.get()
            if (
                entry is None
                or entry.call is not call
                or (result.output or {}).get("provider_ref") != call.handle
            ):
                raise InboundError()
            if (
                await self._handler.conversations.get_message_evidence(
                    tenant_id, message_id, actor=actor
                )
                != entry.reference
            ):
                raise PermissionDenied("收件箱访问拒绝")
            return entry.content
        finally:
            self._handler._result.set(None)
            self._handler._call.reset(token)
