"""普通线程回复独立HIGH插件，保留原六阶段及EXECUTING前持久幂等。"""
from dataclasses import dataclass, field
from typing import Protocol

from connectors.gmail.client import GmailSendResult
from shared.errors import ValidationError
from shared.schemas.business_reply import SMTPReplyRequest
from shared.schemas.identifiers import EmployeeId, MessageId, TenantId, UserId
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.errors import DeliveryCertainty, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.manifest import (
    CostClass,
    HighRiskStageProfile,
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

MANIFEST = ToolManifest(tool_id='email.dialogue.send', version='v1', description='在精确获批普通对话范围内回复当前客户线程',
    risk_level=RiskLevel.HIGH, cost_class=CostClass.LOW, requires_approval=False,
    idempotency=IdempotencyRequirement.REQUIRED, required_permissions=('email:ordinary_reply',),
    high_risk_stage_profile=HighRiskStageProfile.CUSTOMER_OUTBOUND,
    checks=('tenant','permission','suppression','approval','idempotency','rate_limit'),
    input_schema={'type':'object','properties':{'message_id':{'type':'string'},'attempt_id':{'type':'string'},'lease_token':{'type':'string'}},
        'required':['message_id','attempt_id','lease_token'],'additionalProperties':False},
    output_schema={'type':'object','properties':{'provider_ref':{'type':'string'}},'required':['provider_ref'],'additionalProperties':False})


@dataclass(frozen=True)
class DialogueDelivery:
    tenant_id: TenantId
    message_id: MessageId
    attempt_id: str
    employee_id: EmployeeId
    user_id: UserId
    idempotency_key: str
    plan_sha256: str
    lease_token: str
    request: SMTPReplyRequest = field(repr=False)


class ThreadReplyConnector(Protocol):
    async def send_once(self, request: SMTPReplyRequest) -> GmailSendResult: ...
    async def reconcile_once(self, request: SMTPReplyRequest) -> GmailSendResult: ...


class DialogueDeliveryPorts(Protocol):
    async def current(self, tenant: TenantId, message: MessageId, attempt: str, lease_token: str) -> DialogueDelivery:
        """每次重读实际范围与不可变计划；不解析凭证，不创建外部连接。"""
        ...
    async def reserve(self, delivery: DialogueDelivery) -> None:
        """同attempt预占Campaign总量与身份额度，身份明确for_cold_outreach=False；unknown不退款。"""
        ...
    async def started(self, delivery: DialogueDelivery) -> None:
        """执行前持久CAS fencing；未知/已开始不得再次派发。"""
        ...
    async def connector(self, delivery: DialogueDelivery) -> ThreadReplyConnector:
        """仅执行或核对阶段解析本人精确连接，不支持的provider明确拒绝。"""
        ...
    async def sent(self, delivery: DialogueDelivery, provider_ref: str) -> None:
        """同源发送结果与轮计数幂等提交；失败不允许换key重发。"""
        ...


async def _current(ports: DialogueDeliveryPorts, ctx: ToolCallContext) -> DialogueDelivery:
    if set(ctx.params) != {'message_id', 'attempt_id', 'lease_token'}:
        raise ValidationError('普通回复参数无效')
    message, attempt, token = ctx.params['message_id'], ctx.params['attempt_id'], ctx.params['lease_token']
    if not isinstance(message, str) or not isinstance(attempt, str) or not isinstance(token, str):
        raise ValidationError('普通回复身份无效')
    result = await ports.current(ctx.tenant_id, MessageId(message), attempt, token)
    if (result.tenant_id != ctx.tenant_id or result.message_id != message or result.attempt_id != attempt
        or ctx.user_id != result.user_id or ctx.idempotency_key != result.idempotency_key):
        raise ValidationError('普通回复范围不匹配')
    return result


class DialogueSendCheck:
    def __init__(self, name: str, ports: DialogueDeliveryPorts) -> None:
        self.name, self.ports = name, ports

    async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
        delivery = await _current(self.ports, ctx)
        state.preflight = delivery
        if self.name == 'rate_limit':
            await self.ports.reserve(delivery)
        return None


def dialogue_checks(ports: DialogueDeliveryPorts) -> dict[str, DialogueSendCheck | IdempotencyCheck]:
    """不改核心管线；每个业务阶段重新取得同一精确授权交集。"""
    return {name: IdempotencyCheck() if name == 'idempotency' else DialogueSendCheck(name, ports) for name in MANIFEST.checks}


class DialogueSendHandler:
    def __init__(self, ports: DialogueDeliveryPorts, fingerprints: HmacFingerprintProvider) -> None:
        self.ports, self.fingerprints = ports, fingerprints

    async def prepare(self, ctx: ToolCallContext, preflight: object | None = None) -> PreparedToolCall:
        delivery = await _current(self.ports, ctx)
        fingerprint, version = self.fingerprints.fingerprint((b'ordinary-dialogue-v1', str(delivery.tenant_id).encode(),
            delivery.attempt_id.encode(), delivery.plan_sha256.encode(), delivery.request.content_hash.encode(),
            delivery.request.deterministic_message_id.encode(), delivery.request.idempotency_header.encode()))
        return PreparedToolCall(request_fingerprint=fingerprint, fingerprint_version=version,
            audit_projection={'attempt_id':delivery.attempt_id, 'message_id':str(delivery.message_id)}, payload=delivery)

    async def execute(self, tenant_id: TenantId, prepared: PreparedToolCall) -> dict[str, str]:
        return await self._deliver(tenant_id, prepared, reconcile=False)

    async def reconcile(self, tenant_id: TenantId, prepared: PreparedToolCall) -> dict[str, str]:
        return await self._deliver(tenant_id, prepared, reconcile=True)

    async def _deliver(self, tenant: TenantId, prepared: PreparedToolCall, *, reconcile: bool) -> dict[str, str]:
        delivery = prepared.payload
        if not isinstance(delivery, DialogueDelivery) or delivery.tenant_id != tenant:
            raise ValidationError('普通回复执行范围无效')
        current = await self.ports.current(tenant, delivery.message_id, delivery.attempt_id, delivery.lease_token)
        if current != delivery:
            raise ValidationError('普通回复发送材料已改变')
        if not reconcile:
            await self.ports.started(delivery)
        connector = await self.ports.connector(delivery)
        result = await (connector.reconcile_once(delivery.request) if reconcile else connector.send_once(delivery.request))
        if result.certainty is not DeliveryCertainty.SENT or not result.provider_ref:
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
        try:
            await self.ports.sent(delivery, result.provider_ref)
        except Exception:  # noqa: BLE001 -- SMTP已接受，任何持久化错误只允许核对。
            raise ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED) from None
        return {'provider_ref':result.provider_ref}
