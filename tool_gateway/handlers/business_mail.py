"""普通来信具名只读插件：EXECUTING后读取并归档，仅返回task-owned句柄。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Protocol, cast

from connectors.qq_mail.business_inbound import content_hash
from shared.business_mail import (
    ArchivedBusinessMail,
    BusinessArchivedPage,
    BusinessFetchPage,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.email_inbound import MIME_BYTES, ArchivedInboundRaw
from shared.schemas.identifiers import TenantId, UserId, new_id
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
    ToolRegistry,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolGateway,
    ToolInvocationState,
    _GatewayRegistry,
)
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory

MANIFEST = ToolManifest(
    tool_id="email.business.fetch",
    version="v1",
    description="读取当前负责人QQ邮箱的一页普通业务来信",
    risk_level=RiskLevel.LOW,
    cost_class=CostClass.FREE,
    requires_approval=False,
    idempotency=IdempotencyRequirement.NONE,
    required_permissions=("email:business_read",),
    checks=("tenant", "permission"),
    input_schema={
        "type": "object",
        "properties": {"mailbox_id": {"type": "string"}},
        "required": ["mailbox_id"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"provider_ref": {"type": "string"}},
        "required": ["provider_ref"],
        "additionalProperties": False,
    },
)


class BusinessRawArchiver(Protocol):
    async def archive(
        self, tenant_id: TenantId, raw_mime: bytes, *, maximum_bytes: int
    ) -> ArchivedInboundRaw: ...


class BusinessPageReader(Protocol):
    async def fetch(
        self, *, uidvalidity: int | None, after_uid: int, before_uid: int | None = None
    ) -> BusinessFetchPage: ...


class BusinessMailSlot:
    def __init__(self) -> None:
        self.entry: tuple[str, BusinessArchivedPage] | None = None
        self.owner: object | None = None

    def put(self, page: BusinessArchivedPage) -> str:
        if self.entry is not None:
            raise ValidationError("业务来信结果槽已占用")
        handle = new_id("bpg")
        self.entry = (handle, page)
        self.owner = asyncio.current_task()
        return handle

    def take(self, handle: str) -> BusinessArchivedPage:
        if (
            self.owner is not asyncio.current_task()
            or self.entry is None
            or self.entry[0] != handle
        ):
            raise ValidationError("业务来信结果不可领取")
        result = self.entry[1]
        self.clear()
        return result

    def clear(self) -> None:
        if self.owner is not None and self.owner is not asyncio.current_task():
            raise ValidationError("业务来信结果槽不属于当前任务")
        self.entry, self.owner = None, None


class _Check:
    def __init__(
        self,
        name: str,
        tenant: TenantId,
        user: UserId,
        mailbox: str,
        authorize: Callable[[], Awaitable[None]],
    ) -> None:
        self.name, self.tenant, self.user, self.mailbox, self.authorize = (
            name,
            tenant,
            user,
            mailbox,
            authorize,
        )

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        if (
            ctx.tenant_id != self.tenant
            or ctx.user_id != self.user
            or dict(ctx.params) != {"mailbox_id": self.mailbox}
        ):
            return CheckRejection(self.name, "business_mail:scope", "普通来信范围不符")
        await self.authorize()
        return None


class BusinessMailFetchHandler:
    def __init__(
        self,
        *,
        tenant: TenantId,
        mailbox: str,
        uidvalidity: int | None,
        after_uid: int,
        before_uid: int | None,
        reader: Callable[[], BusinessPageReader],
        archiver: BusinessRawArchiver,
        authorize: Callable[[], Awaitable[None]],
        slot: BusinessMailSlot,
        fingerprints: HmacFingerprintProvider,
    ) -> None:
        self.tenant, self.mailbox, self.validity, self.after = (
            tenant,
            mailbox,
            uidvalidity,
            after_uid,
        )
        self.before = before_uid
        self.reader, self.archiver, self.authorize, self.slot, self.fingerprints = (
            reader,
            archiver,
            authorize,
            slot,
            fingerprints,
        )

    async def prepare(
        self, ctx: ToolCallContext, preflight: object | None
    ) -> PreparedToolCall:
        if ctx.tenant_id != self.tenant or dict(ctx.params) != {
            "mailbox_id": self.mailbox
        }:
            raise PermissionDenied("普通收件范围无效")
        digest, version = self.fingerprints.fingerprint(
            (
                self.tenant.encode(),
                self.mailbox.encode(),
                str(self.validity).encode(),
                str(self.after).encode(),
            )
        )
        return PreparedToolCall(digest, version, {"mailbox_id": self.mailbox}, None)

    async def execute(
        self, tenant_id: TenantId, prepared: PreparedToolCall
    ) -> dict[str, str]:
        if tenant_id != self.tenant:
            raise PermissionDenied("普通收件范围无效")
        await self.authorize()
        page = await self.reader().fetch(
            uidvalidity=self.validity, after_uid=self.after, before_uid=self.before
        )
        await self.authorize()
        archived = []
        for message in page.messages:
            await self.authorize()
            meta = await self.archiver.archive(
                tenant_id, message.raw_mime, maximum_bytes=MIME_BYTES
            )
            if meta.content_hash != hashlib.sha256(message.raw_mime).hexdigest():
                raise ValidationError("普通来信原件校验失败")
            archived.append(
                ArchivedBusinessMail(
                    uidvalidity=message.uidvalidity,
                    uid=message.uid,
                    sender=message.sender,
                    subject=message.subject,
                    body_text=message.body_text,
                    message_header=message.message_header,
                    received_at=message.received_at,
                    parse_failure=message.parse_failure,
                    artifact_id=str(meta.artifact_id),
                    source_kind=message.source_kind,
                    source_sha256=meta.content_hash,
                    mime_sha256=meta.content_hash
                    if message.source_kind == "full_mime"
                    else None,
                    content_sha256=content_hash(message),
                )
            )
        await self.authorize()
        return {
            "provider_ref": self.slot.put(
                BusinessArchivedPage(
                    uidvalidity=page.uidvalidity,
                    after_uid=page.after_uid,
                    next_uid=page.next_uid,
                    snapshot_end_uid=page.snapshot_end_uid,
                    messages=tuple(archived),
                )
            )
        }


class GatewayBusinessMailReader:
    """每次请求独立registry/slot；任何失败或取消都会清槽，不从账本恢复正文。"""

    def __init__(
        self,
        *,
        tenant: TenantId,
        user: UserId,
        mailbox: str,
        reader: Callable[[], BusinessPageReader],
        archiver: BusinessRawArchiver,
        authorize: Callable[[], Awaitable[None]],
        fingerprints: HmacFingerprintProvider,
        ledger_factory: ToolGatewayUnitOfWorkFactory,
        lease_owner: str,
    ) -> None:
        self.tenant, self.user, self.mailbox = tenant, user, mailbox
        self.reader, self.archiver, self.authorize = reader, archiver, authorize
        self.fingerprints, self.ledger, self.lease_owner = (
            fingerprints,
            ledger_factory,
            lease_owner,
        )

    async def fetch(
        self, *, uidvalidity: int | None, after_uid: int, before_uid: int | None = None
    ) -> BusinessArchivedPage:
        slot = BusinessMailSlot()
        registry = ToolRegistry()
        registry.register(
            MANIFEST,
            BusinessMailFetchHandler(
                tenant=self.tenant,
                mailbox=self.mailbox,
                uidvalidity=uidvalidity,
                after_uid=after_uid,
                before_uid=before_uid,
                reader=self.reader,
                archiver=self.archiver,
                authorize=self.authorize,
                slot=slot,
                fingerprints=self.fingerprints,
            ),
        )
        gateway = ToolGateway(
            cast(_GatewayRegistry, registry),
            {
                name: _Check(name, self.tenant, self.user, self.mailbox, self.authorize)
                for name in MANIFEST.checks
            },
            self.ledger,
            lease_duration=timedelta(seconds=180),
            lease_owner=self.lease_owner,
            id_factory=new_id,
        )
        try:
            result = await gateway.invoke(
                ToolCallContext(
                    tenant_id=self.tenant,
                    user_id=self.user,
                    tool_id=MANIFEST.tool_id,
                    params={"mailbox_id": self.mailbox},
                )
            )
            if result.status is not ToolCallStatus.SUCCEEDED:
                raise ValidationError("普通来信读取未完成，原游标保留")
            await self.authorize()
            handle = (result.output or {}).get("provider_ref")
            if not isinstance(handle, str):
                raise ValidationError("普通来信读取结果无效")
            return slot.take(handle)
        finally:
            slot.clear()
