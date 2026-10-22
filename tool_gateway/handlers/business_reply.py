"""单封明确授权的内部询盘追问，HIGH六阶段，不借用私人QQ测试grant。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from connectors.qq_mail.business_reply import QQReplyEnvelope
from domains.conversations.business_reply import BODY, BusinessReplyScope
from shared.errors import ValidationError
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    IdempotencyRequirement,
    RiskLevel,
    ToolManifest,
)
from tool_gateway.pipeline import CheckRejection, PreparedToolCall

STAGES = (
    "tenant",
    "permission",
    "suppression",
    "approval",
    "idempotency",
    "rate_limit",
)
MANIFEST = ToolManifest(
    tool_id="email.business.reply",
    version="v1",
    description="精确内部测试询盘单封追问",
    risk_level=RiskLevel.HIGH,
    cost_class=CostClass.LOW,
    requires_approval=True,
    idempotency=IdempotencyRequirement.REQUIRED,
    required_permissions=("business_mail:reply",),
    checks=STAGES,
    input_schema={
        "type": "object",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "required": ["provider_ref", "already_existed"],
        "properties": {
            "provider_ref": {"type": "string"},
            "already_existed": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
)


def matches(ctx, scope):
    return (
        ctx.tenant_id == scope.actor.tenant_id
        and ctx.user_id == scope.actor.employee_id
        and ctx.tool_id == MANIFEST.tool_id
        and ctx.idempotency_key == scope.key
        and ctx.approval_ref == scope.message_id
        and ctx.run_id is None
        and ctx.campaign_ref is None
        and dict(ctx.params) == {"message_id": scope.message_id}
    )


class BusinessReplyPolicy(Protocol):
    async def check(self, stage: str, scope: BusinessReplyScope) -> bool: ...


class BusinessReplyCheck:
    def __init__(self, name, scope, policy, *, now=lambda: datetime.now(UTC)):
        self.name, self.scope, self.policy, self.now = name, scope, policy, now

    async def check(self, ctx, state):
        del state
        if (
            not matches(ctx, self.scope)
            or not self.scope.approved_at <= self.now() < self.scope.expires_at
            or not await self.policy.check(self.name, self.scope)
        ):
            if self.name == "rate_limit":
                raise ToolGatewayError(ToolErrorCategory.PERMISSION_DENIED)
            return CheckRejection(
                self.name, "business_reply:denied", "当前询盘回复授权不满足"
            )
        return None


@dataclass(frozen=True, repr=False)
class Payload:
    tenant_id: str
    request: QQReplyEnvelope


class BusinessReplyHandler:
    def __init__(self, scope, fingerprints: HmacFingerprintProvider, connector_factory):
        self.scope, self.fingerprints, self.connector_factory = (
            scope,
            fingerprints,
            connector_factory,
        )

    def envelope(self):
        s = self.scope
        digest, version = self.fingerprints.fingerprint(
            tuple(v.encode() for v in ("business-reply-v1", s.digest, s.key, BODY))
        )
        return (
            QQReplyEnvelope(
                s.sender,
                s.recipient,
                "Re: [TEST-04] YJ-168 purchase inquiry",
                BODY,
                f"businessreply.{digest}@messages.tradeos.invalid",
                f"businessreply.{digest}",
                s.original_message_id,
            ),
            digest,
            version,
        )

    async def prepare(self, ctx, preflight=None):
        del preflight
        if not matches(ctx, self.scope):
            raise ValidationError("询盘回复调用与授权不匹配")
        request, digest, version = self.envelope()
        return PreparedToolCall(
            digest,
            version,
            {"message_id": self.scope.message_id},
            Payload(ctx.tenant_id, request),
        )

    async def _dispatch(self, tenant_id, prepared, *, reconcile):
        if (
            not isinstance(prepared.payload, Payload)
            or prepared.payload.tenant_id != tenant_id
        ):
            raise ValidationError("询盘回复材料绑定不匹配")
        # 工厂必须在EXECUTING持久化后才可取凭证；再次重核当前来源与授权。
        connector = await self.connector_factory()
        result = await (
            connector.reconcile_once(prepared.payload.request)
            if reconcile
            else connector.send_once(prepared.payload.request)
        )
        return {
            "provider_ref": result.provider_ref,
            "already_existed": result.already_existed,
        }

    async def execute(self, tenant_id, prepared):
        return await self._dispatch(tenant_id, prepared, reconcile=False)

    async def reconcile(self, tenant_id, prepared):
        return await self._dispatch(tenant_id, prepared, reconcile=True)
