"""本人阿里邮箱认证插件；只验证连接，不发送邮件。"""
from dataclasses import dataclass, field
from typing import Protocol

from domains.conversations.aliyun_connection import AliyunActor
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import PreparedToolCall, SafeScalar, ToolCallContext

MANIFEST = ToolManifest(
    tool_id="email.aliyun.connect", version="v1",
    description="验证当前员工明确授权的阿里企业邮箱SMTP和IMAP连接，不发信",
    risk_level=RiskLevel.MEDIUM, cost_class=CostClass.FREE,
    requires_approval=False, idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("mailbox:connect",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={"type": "object", "properties": {"operation_id": {"type": "string"}},
                  "required": ["operation_id"], "additionalProperties": False},
    output_schema={"type": "object", "properties": {"provider_ref": {"type": "string"}},
                   "required": ["provider_ref"], "additionalProperties": False},
)


class MailAuthenticator(Protocol):
    async def authenticate(self) -> None: ...


@dataclass(frozen=True)
class AliyunConnectHandler:
    actor: AliyunActor
    operation_id: str
    connector: MailAuthenticator = field(repr=False)
    fingerprints: HmacFingerprintProvider = field(repr=False)

    async def prepare(self, ctx: ToolCallContext, preflight: object | None = None) -> PreparedToolCall:
        if (ctx.tenant_id != self.actor.tenant_id or ctx.user_id != self.actor.employee_id
            or dict(ctx.params) != {"operation_id": self.operation_id}):
            raise ValidationError("邮箱授权范围无效")
        digest, version = self.fingerprints.fingerprint((
            str(ctx.tenant_id).encode(), str(ctx.user_id).encode(), self.operation_id.encode(),
        ))
        return PreparedToolCall(digest, version, {"operation_id": self.operation_id}, self)

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> dict[str, SafeScalar]:
        if tenant_id != self.actor.tenant_id or prepared.payload is not self:
            raise ValidationError("邮箱授权范围无效")
        await self.connector.authenticate()
        return {"provider_ref": self.operation_id}
