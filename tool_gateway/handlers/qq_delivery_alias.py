"""QQ 收件方原件工具；独立来源、实际读取员工和原件标识进入审计。"""
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Protocol

from connectors._deadline_process import PreparationDeadline
from connectors.qq_mail.delivery_collect import DeliveryReadRequest
from connectors.qq_mail.sent_alias import SentAliasEvidence, verify_sent_alias
from shared.email_inbound import InboundRawArchiver
from shared.schemas.identifiers import TenantId, UserId
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import (
    CheckRejection,
    PreparedToolCall,
    ToolCallContext,
    ToolInvocationState,
)

MANIFEST = ToolManifest(
    tool_id="email.qq.delivery_alias.read", version="v1",
    description="从已授权自有收件邮箱读取单 UID 投递原件并核对唯一发送尝试",
    risk_level=RiskLevel.LOW, cost_class=CostClass.FREE, requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("email:inbound_read",),
    checks=("tenant", "permission", "idempotency"),
    input_schema={"type":"object", "required":("attempt_id",),
        "properties":{"attempt_id":{"type":"string"}}, "additionalProperties":False},
    output_schema={"type":"object", "required":("provider_ref",),
        "properties":{"provider_ref":{"type":"string"}}, "additionalProperties":False},
    redact_fields=(),
)


class DeliveryAliasCollector(Protocol):
    async def collect(self, request: DeliveryReadRequest) -> SentAliasEvidence: ...


class DeliveryAliasCheck:
    def __init__(self, name: str, tenant: TenantId, user: UserId, attempt: str,
                 authorize: Callable[[], Awaitable[bool]]) -> None:
        self.name, self.tenant, self.user, self.attempt, self.authorize = name, tenant, user, attempt, authorize

    async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
        if (ctx.tenant_id != self.tenant or ctx.user_id != self.user
            or ctx.tool_id != MANIFEST.tool_id or ctx.params != {"attempt_id":self.attempt}
            or (self.name == "permission" and not await self.authorize())):
            return CheckRejection(self.name, self.name+":qq_delivery_binding", "QQ 采集授权不匹配")
        return None


class DeliveryAliasReadHandler:
    def __init__(self, *, tenant: TenantId, user: UserId, attempt: str,
                 request: DeliveryReadRequest, authorization_binding: bytes,
                 authorize: Callable[[], Awaitable[bool]], collector: DeliveryAliasCollector,
                 archiver: InboundRawArchiver, fingerprints: HmacFingerprintProvider,
                 deadline: PreparationDeadline | None = None) -> None:
        self.tenant, self.user, self.attempt, self.request = tenant, user, attempt, request
        self.authorize, self.collector, self.archiver = authorize, collector, archiver
        self.fingerprints, self.binding = fingerprints, authorization_binding
        self.deadline = deadline

    async def prepare(self, ctx: ToolCallContext, preflight: object | None) -> PreparedToolCall:
        if (preflight is not None or ctx.tool_id != MANIFEST.tool_id
            or ctx.tenant_id != self.tenant or ctx.user_id != self.user
            or ctx.params != {"attempt_id":self.attempt} or not await self.authorize()):
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        digest, version = self.fingerprints.fingerprint((self.tenant.encode(), self.user.encode(),
            self.attempt.encode(), self.binding,
            json.dumps(asdict(self.request),sort_keys=True,separators=(",",":")).encode()))
        return PreparedToolCall(digest,version,{"attempt_id":self.attempt},self.request)

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> dict[str, str]:
        if tenant_id != self.tenant or prepared.payload != self.request:
            raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
        try:
            async with asyncio.timeout(self.deadline.remaining() if self.deadline is not None
                                       else self.request.timeout_seconds):
                if not await self.authorize():
                    raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
                evidence = await self.collector.collect(self.request)
                if evidence.source != "controlled_recipient_imap":
                    raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
                verified = verify_sent_alias((evidence.raw_headers,),source="controlled_recipient_imap",
                    canonical_header=self.request.canonical_header,
                    idempotency_header=self.request.idempotency_header,
                    sender=self.request.sender,recipient=self.request.recipient)
                if verified != evidence or not await self.authorize():
                    raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
                archived = await self.archiver.archive(tenant_id,evidence.raw_headers,
                    maximum_bytes=self.request.header_bytes)
                if not await self.authorize():
                    raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
                return {"provider_ref":str(archived.artifact_id)}
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 外部错误只保留固定永久失败
            # 包括结果未知的邮箱/证据 IO；不代表未执行，原键终止，不重采或换预算。
            raise ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT) from None
