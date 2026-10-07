"""邮件反馈只读工具：typed page 仅经一次性进程内 handle 交给可信 worker。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from connectors.gmail.client import GmailConnector, SecretResolver
from shared.errors import ValidationError
from shared.schemas.email_feedback import EmailFeedbackPage
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
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
    tool_id="email.feedback.fetch",
    version="v1",
    description="读取一页 provider-neutral 邮件投递反馈",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("email:feedback_read",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "required": ("mailbox_alias", "page_limit"),
        "properties": {
            "mailbox_alias": {"type": "string"},
            "cursor": {"type": ("string", "null")},
            "page_limit": {"type": "integer", "minimum": 1, "maximum": 100},
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

_MAILBOX_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_HANDLE_RE = re.compile(r"fpg_[0-7][0-9A-HJKMNP-TV-Z]{25}")


@runtime_checkable
class ToolEmailFeedbackReader(Protocol):
    async def fetch(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        cursor: str | None,
        page_limit: int,
    ) -> EmailFeedbackPage: ...


@runtime_checkable
class _ProviderEmailFeedbackReader(Protocol):
    async def fetch(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        cursor: str | None,
        page_limit: int,
    ) -> EmailFeedbackPage: ...


@runtime_checkable
class _ToolGatewayInvoker(Protocol):
    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult: ...


class _GmailProviderEmailFeedbackReader:
    """判权通过后才构造并配置 Gmail；凭证不进入调用上下文。"""

    def __init__(
        self,
        connector_factory: Callable[[TenantId], GmailConnector],
        secret_resolver: SecretResolver,
    ) -> None:
        if not callable(connector_factory) or not isinstance(
            secret_resolver, SecretResolver
        ):
            raise ValidationError("feedback reader dependency 无效")
        self._connector_factory = connector_factory
        self._secret_resolver = secret_resolver

    async def fetch(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        cursor: str | None,
        page_limit: int,
    ) -> EmailFeedbackPage:
        connector = self._connector_factory(tenant_id)
        if not isinstance(connector, GmailConnector):
            raise ValidationError("feedback connector 无效")
        await connector.configure(self._secret_resolver)
        return await connector.fetch_feedback_page(
            mailbox_alias,
            cursor,
            page_limit,
        )


class FeedbackPageSlot:
    """容量为一的进程内交接槽；handle 读取一次即删除。"""

    def __init__(self) -> None:
        self._entry: tuple[str, EmailFeedbackPage] | None = None

    @property
    def is_empty(self) -> bool:
        return self._entry is None

    def put(self, page: EmailFeedbackPage) -> str:
        if not isinstance(page, EmailFeedbackPage) or self._entry is not None:
            raise ValidationError("feedback page slot 无效")
        handle = new_id("fpg")
        self._entry = (handle, page)
        return handle

    def take(self, handle: str) -> EmailFeedbackPage:
        if (
            not isinstance(handle, str)
            or _HANDLE_RE.fullmatch(handle) is None
            or self._entry is None
            or self._entry[0] != handle
        ):
            raise ValidationError("feedback page handle 无效")
        page = self._entry[1]
        self._entry = None
        return page

    def discard_all(self) -> None:
        self._entry = None


class ToolGatewayEmailFeedbackReader:
    """worker-facing reader；Gateway 成功后领取 page，其他路径清槽。"""

    def __init__(
        self,
        gateway: _ToolGatewayInvoker,
        slot: FeedbackPageSlot,
        user_id: UserId,
    ) -> None:
        if (
            not isinstance(gateway, _ToolGatewayInvoker)
            or not isinstance(slot, FeedbackPageSlot)
            or not isinstance(user_id, str)
            or not user_id
        ):
            raise ValidationError("feedback tool reader dependency 无效")
        self._gateway = gateway
        self._slot = slot
        self._user_id = user_id

    async def fetch(
        self,
        tenant_id: TenantId,
        mailbox_alias: str,
        cursor: str | None,
        page_limit: int,
    ) -> EmailFeedbackPage:
        try:
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
            if not isinstance(result, ToolCallResult):
                raise ValidationError("feedback tool result 无效")
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ToolGatewayError(
                    result.error_category or ToolErrorCategory.UNEXPECTED,
                    retry_after_seconds=result.retry_after_seconds,
                )
            handle = None if result.output is None else result.output.get("provider_ref")
            if not isinstance(handle, str):
                raise ValidationError("feedback tool result 无效")
            return self._slot.take(handle)
        except BaseException:
            self._slot.discard_all()
            raise


@dataclass(frozen=True, repr=False)
class _FeedbackPayload:
    tenant_id: TenantId
    mailbox_alias: str
    cursor: str | None = field(repr=False)
    page_limit: int


class EmailFeedbackFetchHandler:
    """准备阶段只做安全投影；执行阶段返回一次性 page handle。"""

    def __init__(
        self,
        reader: _ProviderEmailFeedbackReader,
        slot: FeedbackPageSlot,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        if (
            not isinstance(reader, _ProviderEmailFeedbackReader)
            or not isinstance(slot, FeedbackPageSlot)
            or not isinstance(fingerprints, HmacFingerprintProvider)
        ):
            raise ValidationError("feedback handler dependency 无效")
        self._reader = reader
        self._slot = slot
        self._fingerprints = fingerprints

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        del preflight
        if set(ctx.params) != {"mailbox_alias", "cursor", "page_limit"}:
            raise ValidationError("feedback fetch params 无效")
        mailbox_alias = ctx.params.get("mailbox_alias")
        cursor = ctx.params.get("cursor")
        page_limit = ctx.params.get("page_limit")
        if (
            not isinstance(mailbox_alias, str)
            or _MAILBOX_RE.fullmatch(mailbox_alias) is None
            or (
                cursor is not None
                and (
                    not isinstance(cursor, str)
                    or not 1 <= len(cursor) <= 32768
                    or cursor != cursor.strip()
                    or any(
                        unicodedata.category(char).startswith("C") for char in cursor
                    )
                )
            )
            or not isinstance(page_limit, int)
            or isinstance(page_limit, bool)
            or not 1 <= page_limit <= 100
        ):
            raise ValidationError("feedback fetch params 无效")
        cursor_bytes = cursor.encode("utf-8") if cursor is not None else b""
        fingerprint, version = self._fingerprints.fingerprint(
            (
                str(ctx.tenant_id).encode(),
                mailbox_alias.encode(),
                cursor_bytes,
                str(page_limit).encode(),
            )
        )
        return PreparedToolCall(
            fingerprint,
            version,
            {
                "mailbox_alias": mailbox_alias,
                "page_limit": page_limit,
                "has_cursor": cursor is not None,
            },
            _FeedbackPayload(
                ctx.tenant_id,
                mailbox_alias,
                cursor,
                page_limit,
            ),
        )

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, SafeScalar]:
        payload = prepared.payload
        if not isinstance(payload, _FeedbackPayload) or payload.tenant_id != tenant_id:
            raise ValidationError("feedback fetch payload 无效")
        try:
            page = await self._reader.fetch(
                tenant_id,
                payload.mailbox_alias,
                payload.cursor,
                payload.page_limit,
            )
            if not isinstance(page, EmailFeedbackPage):
                raise ValidationError("feedback reader result 无效")
            handle = self._slot.put(page)
        except BaseException:
            self._slot.discard_all()
            raise
        return {"provider_ref": handle}
