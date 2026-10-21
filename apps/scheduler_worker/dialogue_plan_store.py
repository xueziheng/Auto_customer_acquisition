"""普通计划原件写入经独立Gateway工具；正文不进入ledger或外部凭证。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from artifact_store.store import (
    GeneratedArtifactKind,
    GeneratedArtifactMeta,
    GeneratedArtifactStore,
)
from domains.conversations.dialogue_execution import DialogueExecutionClaim
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    IdempotencyKey,
    RunId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.idempotency import IdempotencyCheck
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
)
from tool_gateway.repository import ToolGatewayUnitOfWork

MANIFEST = ToolManifest(tool_id='artifact.dialogue.plan', version='v1', description='保存获准普通回复的不可变计划',
    risk_level=RiskLevel.MEDIUM, cost_class=CostClass.LOW, requires_approval=False,
    required_permissions=('dialogue:plan',), idempotency=IdempotencyRequirement.REQUIRED,
    checks=('tenant','permission','idempotency'),
    input_schema={'type':'object','properties':{'message_id':{'type':'string'}},'required':['message_id'],'additionalProperties':False},
    output_schema={'type':'object','properties':{'provider_ref':{'type':'string'}},'required':['provider_ref'],'additionalProperties':False})


@dataclass
class _Write:
    claim: DialogueExecutionClaim
    user: UserId
    content: bytes = field(repr=False)
    store: GeneratedArtifactStore
    check_current: Callable[[], Awaitable[None]]
    fingerprints: HmacFingerprintProvider

    async def prepare(self, ctx: ToolCallContext, preflight: object | None = None) -> PreparedToolCall:
        fingerprint, version = self.fingerprints.fingerprint((str(self.claim.tenant_id).encode(), self.claim.run_id.encode(), self.content))
        return PreparedToolCall(request_fingerprint=fingerprint, fingerprint_version=version,
            audit_projection={'message_id':str(self.claim.source_message_id)}, payload=None)

    async def execute(self, tenant: TenantId, prepared: PreparedToolCall) -> dict[str, str]:
        if tenant != self.claim.tenant_id:
            raise ValidationError('普通计划租户不匹配')
        await self.check_current()
        meta = await self.store.put(tenant, GeneratedArtifactKind.DIALOGUE_PLAN, self.content,
            'application/vnd.tradeos.dialogue-plan+json', workflow_run_id=RunId(self.claim.run_id),
            subject_ref=str(self.claim.source_message_id), sequence_number=self.claim.model_sequence + 1,
            idempotency_key=IdempotencyKey(f'{self.claim.source_message_id}:{self.claim.model_sequence + 1}:dialogue_plan'),
            generated_by='ordinary_dialogue_v1')
        await self.check_current()
        return {'provider_ref':str(meta.artifact_id)}


class _Check:
    def __init__(self, name: str, write: _Write) -> None:
        self.name, self.write = name, write

    async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
        if (ctx.tenant_id != self.write.claim.tenant_id or ctx.user_id != self.write.user
            or ctx.params != {'message_id':str(self.write.claim.source_message_id)}):
            return CheckRejection(self.name, 'dialogue:scope', '普通计划范围无效')
        await self.write.check_current()
        return None


class GatewayDialoguePlanStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], store: GeneratedArtifactStore,
                 fingerprints: HmacFingerprintProvider) -> None:
        self.sessions, self.store, self.fingerprints = sessions, store, fingerprints

    async def put(self, claim: DialogueExecutionClaim, user: UserId, content: bytes,
                  check_current: Callable[[], Awaitable[None]]) -> GeneratedArtifactMeta:
        write = _Write(claim, user, content, self.store, check_current, self.fingerprints)
        registry = ToolRegistry()
        registry.register(MANIFEST, write)
        gateway = ToolGateway(registry, # type: ignore[arg-type]
            {'tenant':_Check('tenant',write), 'permission':_Check('permission',write), 'idempotency':IdempotencyCheck()},
            lambda tenant: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self.sessions, tenant)),
            lease_duration=timedelta(seconds=60), lease_owner='ordinary-plan-artifact', id_factory=new_id)
        result = await gateway.invoke(ToolCallContext(tenant_id=claim.tenant_id, user_id=user,
            run_id=RunId(claim.run_id), tool_id=MANIFEST.tool_id,
            params={'message_id':str(claim.source_message_id)},
            idempotency_key=IdempotencyKey(f'{claim.source_message_id}:{claim.model_sequence + 1}:dialogue_plan')))
        if result.output is None or not isinstance(result.output.get('provider_ref'),str):
            raise ValidationError('普通计划保存未完成')
        return await self.store.get_meta(claim.tenant_id, ArtifactId(str(result.output['provider_ref'])))
