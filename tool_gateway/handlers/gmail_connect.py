"""网页 OAuth 一次性交换插件；不改变 Gateway 核心及正式发送门禁。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

from connectors.gmail.web_oauth import GmailWebOAuth
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
    tool_id="email.gmail.connect", version="v1",
    description="交换当前员工明确发起且完成 Google 同意的单次网页授权",
    risk_level=RiskLevel.MEDIUM, cost_class=CostClass.FREE,
    requires_approval=False, idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("mailbox:connect",),
    checks=("tenant", "permission", "idempotency", "rate_limit"),
    input_schema={"type": "object", "properties": {"operation_id": {"type": "string"}},
                  "required": ["operation_id"], "additionalProperties": False},
    output_schema={"type": "object", "properties": {"provider_ref": {"type": "string"}},
                   "required": ["provider_ref"], "additionalProperties": False},
)


@dataclass(frozen=True, repr=False)
class GmailExchange:
    tenant_id: TenantId
    employee_id: str
    operation_id: str
    email: str
    code: SecretStr
    verifier: SecretStr
    state: SecretStr
    destination: Path


class GmailConnectHandler:
    def __init__(self, request: GmailExchange, connector: GmailWebOAuth,
                 fingerprints: HmacFingerprintProvider):
        self.request, self.connector, self.fingerprints = request, connector, fingerprints

    async def prepare(self, ctx: ToolCallContext,
                      preflight: object | None = None) -> PreparedToolCall:
        request = self.request
        if (ctx.tenant_id != request.tenant_id or ctx.user_id != request.employee_id
                or dict(ctx.params) != {"operation_id": request.operation_id}):
            raise ValidationError("邮箱授权调用不匹配")
        digest, version = self.fingerprints.fingerprint((
            str(ctx.tenant_id).encode(), request.employee_id.encode(),
            request.operation_id.encode(),
        ))
        return PreparedToolCall(digest, version, {"operation_id": request.operation_id}, request)

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> dict[str, SafeScalar]:
        request = prepared.payload
        if not isinstance(request, GmailExchange) or request.tenant_id != tenant_id or request is not self.request:
            raise ValidationError("邮箱授权租户无效")
        await asyncio.to_thread(self.connector.exchange, request.code, request.verifier,
                                request.state, request.destination, request.email)
        return {"provider_ref": request.operation_id}
