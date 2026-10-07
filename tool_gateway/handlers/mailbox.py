"""全邮箱只读工具；原件只经当前任务的一次性槽交给受信同步消费者。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.mailbox import MailboxFailure, MailboxPage
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, SafeScalar, ToolCallContext

MANIFEST = ToolManifest(
    tool_id="email.mailbox.fetch",
    version="v1",
    description="读取本人邮箱的一页历史或增量邮件",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("email:mailbox_read",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "properties": {
            "mailbox_id": {"type": "string"},
            "cursor": {"type": ["string", "null"]},
        },
        "required": ["mailbox_id", "cursor"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"provider_ref": {"type": "string"}},
        "required": ["provider_ref"],
        "additionalProperties": False,
    },
    redact_fields=("cursor",),
)


class PageReader(Protocol):
    async def fetch(self, cursor: str | None) -> MailboxPage: ...


class MailboxSlot:
    def __init__(self) -> None:
        self._value: ContextVar[tuple[str, MailboxPage | MailboxFailure] | None] = (
            ContextVar("mailbox_page", default=None)
        )

    def put(self, page: MailboxPage | MailboxFailure) -> str:
        if self._value.get() is not None:
            raise ValidationError("邮箱结果槽未清理")
        handle = new_id("mpg")
        self._value.set((handle, page))
        return handle

    def take(self, handle: str) -> MailboxPage | MailboxFailure:
        value = self._value.get()
        self.clear()
        if value is None or value[0] != handle:
            raise ValidationError("邮箱结果不可用")
        return value[1]

    def clear(self) -> None:
        self._value.set(None)

    def failure(self) -> MailboxFailure | None:
        value = self._value.get()
        self.clear()
        return value[1] if value and isinstance(value[1], MailboxFailure) else None


@dataclass(frozen=True, repr=False)
class Payload:
    tenant_id: TenantId
    cursor: str | None = field(repr=False)


class MailboxFetchHandler:
    def __init__(
        self,
        reader: PageReader,
        slot: MailboxSlot,
        fingerprints: HmacFingerprintProvider,
    ):
        self.reader, self.slot, self.fingerprints = reader, slot, fingerprints

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        cursor = ctx.params.get("cursor")
        if (
            set(ctx.params) != {"mailbox_id", "cursor"}
            or not isinstance(ctx.params["mailbox_id"], str)
            or (
                cursor is not None
                and (not isinstance(cursor, str) or len(cursor) > 32768)
            )
        ):
            raise ValidationError("邮箱同步参数无效")
        fingerprint, version = self.fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                ctx.params["mailbox_id"].encode(),
                (cursor or "").encode(),
            )
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {"mailbox_id": ctx.params["mailbox_id"]},
            Payload(ctx.tenant_id, cursor),
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, SafeScalar]:
        payload = prepared.payload
        if not isinstance(payload, Payload) or payload.tenant_id != tenant_id:
            raise ValidationError("邮箱同步租户无效")
        try:
            return {
                "provider_ref": self.slot.put(await self.reader.fetch(payload.cursor))
            }
        except MailboxFailure as error:
            self.slot.put(error)
            retryable = error.code in {"rate_limited", "provider_unavailable"}
            category = {
                "rate_limited": ToolErrorCategory.RATE_LIMITED,
                "authorization_required": ToolErrorCategory.PROVIDER_AUTH_REQUIRED,
                "provider_unavailable": ToolErrorCategory.PROVIDER_TRANSIENT,
            }.get(error.code, ToolErrorCategory.PROVIDER_PERMANENT)
            raise ToolGatewayError(
                category, retry_after_seconds=error.retry_after if retryable else None
            ) from None
