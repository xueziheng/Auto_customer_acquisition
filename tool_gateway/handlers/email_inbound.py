"""独立入站读取插件：Gateway内归档，只向受信调用者交付一次typed页。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from connectors.gmail.inbound import GmailInboundReader
from connectors.gmail.inbound_cursor import decode_cursor
from shared.email_inbound import InboundContentGuard, InboundRawArchiver
from shared.errors import ValidationError
from shared.schemas.email_inbound import (
    CANDIDATE_BYTES,
    MIME_BYTES,
    PAGE_ITEMS,
    ArchivedInboundItem,
    ArchivedInboundPage,
    InboundDisposition,
    InboundError,
    InboundRoute,
)
from shared.schemas.identifiers import TenantId, UserId
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.email_inbound_slots import InboundPageSlot
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

MANIFEST = ToolManifest(
    tool_id="email.inbound.fetch",
    version="v1",
    description="读取并归档一页邮件技术候选",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("email:inbound_read",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "required": ("mailbox_alias", "cursor", "page_limit"),
        "properties": {
            "mailbox_alias": {"type": "string"},
            "cursor": {"type": "string"},
            "page_limit": {"type": "integer", "minimum": 1, "maximum": PAGE_ITEMS},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ("provider_ref",),
        "properties": {"provider_ref": {"type": "string"}},
        "additionalProperties": False,
    },
    redact_fields=("cursor",),
)


@dataclass(frozen=True, repr=False)
class _Payload:
    tenant_id: TenantId
    mailbox_alias: str
    cursor: str
    page_limit: int


class EmailInboundFetchHandler:
    """prepare不构造Connector、不取凭证，execute才调用受信factory。"""

    def __init__(
        self,
        route: InboundRoute,
        reader_factory: Callable[[], GmailInboundReader],
        archiver: InboundRawArchiver,
        guard: InboundContentGuard,
        slot: InboundPageSlot,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        self._route = route
        self._factory = reader_factory
        self._archiver = archiver
        self._guard = guard
        self._slot = slot
        self._fingerprints = fingerprints

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del preflight
        if set(ctx.params) != {"mailbox_alias", "cursor", "page_limit"}:
            raise InboundError()
        alias, cursor, limit = (
            ctx.params.get(name) for name in ("mailbox_alias", "cursor", "page_limit")
        )
        if (
            ctx.tenant_id != self._route.tenant_id
            or alias != self._route.mailbox_alias
            or not isinstance(cursor, str)
            or type(limit) is not int
            or not 1 <= limit <= PAGE_ITEMS
        ):
            raise InboundError()
        decode_cursor(cursor, self._route)
        digest, version = self._fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                str(alias).encode(),
                cursor.encode(),
                str(limit).encode(),
            )
        )
        return PreparedToolCall(
            digest,
            version,
            {"mailbox_alias": alias, "page_limit": limit, "has_cursor": True},
            _Payload(ctx.tenant_id, str(alias), cursor, limit),
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, SafeScalar]:
        payload = prepared.payload
        if (
            not isinstance(payload, _Payload)
            or tenant_id != payload.tenant_id
            or tenant_id != self._route.tenant_id
        ):
            raise InboundError()
        try:
            reader = self._factory()
            if (
                not isinstance(reader, GmailInboundReader)
                or reader.route != self._route
            ):
                raise InboundError()
            page = await reader.fetch_inbound_page(
                payload.mailbox_alias, payload.cursor, payload.page_limit
            )
            if (
                page.route != self._route
                or page.starting_cursor != payload.cursor
                or len(page.items) > payload.page_limit
            ):
                raise InboundError()
            decode_cursor(page.next_cursor, self._route)
            items = []
            for item in page.items:
                raw = None
                if item.raw_mime is not None:
                    raw = await self._archiver.archive(
                        tenant_id, item.raw_mime, maximum_bytes=MIME_BYTES
                    )
                    if raw.tenant_id != tenant_id:
                        raise InboundError()
                disposition = item.disposition
                try:
                    self._guard.check(subject=item.subject, body=item.guard_body)
                    self._guard.check(subject=item.subject, body=item.body)
                except ValidationError:
                    disposition = InboundDisposition.CREDENTIAL_MARKER
                if (
                    disposition is InboundDisposition.CANDIDATE
                    and len((item.subject or "").encode()) + len(item.body.encode())
                    > CANDIDATE_BYTES
                ):
                    disposition = InboundDisposition.TEXT_TOO_LARGE
                items.append(
                    ArchivedInboundItem(
                        provider_ref_digest=item.provider_ref_digest,
                        disposition=disposition,
                        external_message_id=item.external_message_id,
                        in_reply_to=item.in_reply_to,
                        sent_at=item.sent_at,
                        parser_version=item.parser_version,
                        raw=raw,
                    )
                )
            archived = ArchivedInboundPage(
                route=self._route,
                starting_cursor=payload.cursor,
                next_cursor=page.next_cursor,
                items=tuple(items),
            )
            return {"provider_ref": self._slot.put(archived)}
        except BaseException:
            self._slot.discard_all()
            raise


class _Invoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class ToolGatewayEmailInboundReader:
    """只在真实SUCCEEDED领取；完成ledger失败或取消均丢弃本task槽。"""

    def __init__(
        self,
        gateway: _Invoker,
        slot: InboundPageSlot,
        user_id: UserId,
        route: InboundRoute,
    ) -> None:
        self._gateway, self._slot, self._user_id, self._route = (
            gateway,
            slot,
            user_id,
            route,
        )

    async def fetch(
        self, tenant_id: TenantId, mailbox_alias: str, cursor: str, page_limit: int
    ) -> ArchivedInboundPage:
        try:
            if (
                tenant_id != self._route.tenant_id
                or mailbox_alias != self._route.mailbox_alias
            ):
                raise InboundError()
            result = await self._gateway.invoke(
                ToolCallContext(
                    tenant_id=tenant_id,
                    user_id=self._user_id,
                    tool_id=MANIFEST.tool_id,
                    params={
                        "mailbox_alias": mailbox_alias,
                        "cursor": cursor,
                        "page_limit": page_limit,
                    },
                )
            )
            if (
                not isinstance(result, ToolCallResult)
                or result.status is not ToolCallStatus.SUCCEEDED
            ):
                raise ToolGatewayError(
                    result.error_category
                    if isinstance(result, ToolCallResult) and result.error_category
                    else ToolErrorCategory.UNEXPECTED,
                    retry_after_seconds=result.retry_after_seconds
                    if isinstance(result, ToolCallResult)
                    else None,
                )
            handle = (
                None if result.output is None else result.output.get("provider_ref")
            )
            if not isinstance(handle, str):
                raise InboundError()
            page = self._slot.take(handle)
            if (
                page.route != self._route
                or page.starting_cursor != cursor
                or len(page.items) > page_limit
            ):
                raise InboundError()
            decode_cursor(page.next_cursor, self._route)
            return page
        finally:
            self._slot.discard_all()
