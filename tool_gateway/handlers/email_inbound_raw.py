"""待核对原件独立工具；当前人工权限、精确Raw引用、有界字节一次领取。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

from artifact_store.store import BoundedRawArtifactStore, RawArtifactKind
from shared.schemas.email_inbound import MIME_BYTES, ArchivedInboundRaw, InboundError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
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
)


class ReviewRawAuthorization(Protocol):
    async def authorize_raw(
        self, tenant: TenantId, employee: EmployeeId, review_id: str
    ) -> ArchivedInboundRaw: ...


class Invoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


@dataclass(frozen=True)
class _Result:
    tenant: TenantId
    employee: EmployeeId
    review_id: str
    content: bytes = field(repr=False)
    reference: ArchivedInboundRaw


@dataclass(frozen=True, repr=False)
class _Entry:
    owner: asyncio.Task[Any]
    handle: str
    result: _Result


class InboundRawSlot:
    def __init__(self) -> None:
        self._entry: ContextVar[_Entry | None] = ContextVar("inbound_raw", default=None)

    def discard(self) -> None:
        entry = self._entry.get()
        if entry is not None and entry.owner is asyncio.current_task():
            self._entry.set(None)

    def put(self, result: _Result) -> str:
        owner = asyncio.current_task()
        entry = self._entry.get()
        if owner is None or (entry is not None and entry.owner is owner):
            raise InboundError()
        handle = new_id("irb")
        self._entry.set(_Entry(owner, handle, result))
        return handle

    def take(self, handle: object) -> _Result:
        entry = self._entry.get()
        if entry is None or entry.owner is not asyncio.current_task():
            raise InboundError()
        self.discard()
        if entry.handle != handle:
            raise InboundError()
        return entry.result


RAW_MANIFEST = ToolManifest(
    tool_id="email.inbound.raw.read",
    version="v1",
    description="授权下载待核对邮件原件",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("email:inbound_review",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "required": ["review_id"],
        "properties": {"review_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["provider_ref"],
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class _Payload:
    tenant: TenantId
    employee: EmployeeId
    review_id: str


class EmailInboundRawHandler:
    def __init__(
        self,
        authorization: ReviewRawAuthorization,
        store: BoundedRawArtifactStore,
        slot: InboundRawSlot,
    ):
        self._authorization, self._store, self._slot = authorization, store, slot

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        if (
            set(ctx.params) != {"review_id"}
            or not isinstance(ctx.params["review_id"], str)
            or re.fullmatch(r"irv_[0-7][0-9A-HJKMNP-TV-Z]{25}", ctx.params["review_id"])
            is None
        ):
            raise InboundError()
        review = ctx.params["review_id"]
        payload = _Payload(ctx.tenant_id, EmployeeId(str(ctx.user_id)), review)
        digest = hashlib.sha256(
            f"{ctx.tenant_id}:{ctx.user_id}:{review}".encode()
        ).hexdigest()
        return PreparedToolCall(digest, "v1", {"review_id": review}, payload)

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, SafeScalar]:
        p = prepared.payload
        if not isinstance(p, _Payload) or p.tenant != tenant_id:
            raise InboundError()
        try:
            raw = await self._authorization.authorize_raw(
                tenant_id, p.employee, p.review_id
            )
            meta, content = await self._store.get_bounded(
                tenant_id, raw.artifact_id, maximum_bytes=MIME_BYTES
            )
            if (
                meta.kind is not RawArtifactKind.EMAIL_RAW
                or meta.mime_type != "message/rfc822"
                or meta.tenant_id != tenant_id
                or str(meta.artifact_id) != raw.artifact_id
                or meta.content_hash != raw.content_hash
                or meta.size_bytes != raw.size_bytes
                or len(content) != raw.size_bytes
                or hashlib.sha256(content).hexdigest() != raw.content_hash
            ):
                raise InboundError()
            if (
                await self._authorization.authorize_raw(
                    tenant_id, p.employee, p.review_id
                )
                != raw
            ):
                raise InboundError()
            return {
                "provider_ref": self._slot.put(
                    _Result(tenant_id, p.employee, p.review_id, content, raw)
                )
            }
        except BaseException:
            self._slot.discard()
            raise


class ToolGatewayInboundRawReader:
    def __init__(
        self,
        gateway: Invoker,
        slot: InboundRawSlot,
        authorization: ReviewRawAuthorization,
    ):
        self._gateway, self._slot, self._authorization = gateway, slot, authorization

    async def read(
        self, tenant_id: TenantId, employee_id: EmployeeId, review_id: str
    ) -> bytes:
        try:
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id=tenant_id,
                    user_id=UserId(str(employee_id)),
                    tool_id=RAW_MANIFEST.tool_id,
                    params={"review_id": review_id},
                )
            )
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED
                )
            raw = self._slot.take((result.output or {}).get("provider_ref"))
            if (raw.tenant, raw.employee, raw.review_id) != (
                tenant_id,
                employee_id,
                review_id,
            ):
                raise InboundError()
            if (
                await self._authorization.authorize_raw(
                    tenant_id, employee_id, review_id
                )
                != raw.reference
            ):
                raise InboundError()
            return raw.content
        finally:
            self._slot.discard()
