"""受信普通来源选择当前owner；所有真实provider轮共用持久对话预算。"""
import json
from collections.abc import Awaitable, Callable
from typing import Literal

from domains.assistant.schemas import AssistantActor
from domains.conversations.dialogue_source import DialogueSourceReader
from infra.db.dialogue_model_budget import SqlDialogueModelBudget
from shared.schemas.identifiers import MessageId, TenantId
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError
from workflows.reply_qualification.dialogue_authority import (
    CurrentOrdinaryDialogueAccess,
)


class OrdinaryReplyModelAccess:
    def __init__(self, sources: DialogueSourceReader, access: CurrentOrdinaryDialogueAccess) -> None:
        self.sources, self.access = sources, access

    async def actor(self, tenant: TenantId, message: MessageId) -> AssistantActor | None:
        source = await self.sources.for_message(tenant, message)
        if source is None:
            return None
        current = await self.access.current(tenant, message, source.owner_employee_id)
        return AssistantActor(tenant_id=tenant, employee_id=current.owner.actor.employee_id,
            user_id=current.owner.user_id)


class DialogueProviderBudget:
    """每轮provider真正派发前预占；重建对象从round0开始也不能重放旧轮。"""
    def __init__(self, access: CurrentOrdinaryDialogueAccess, budget: SqlDialogueModelBudget,
                 identity: InvocationIdentity, message: MessageId, model: str) -> None:
        self.access, self.budget, self.identity, self.message, self.model = access, budget, identity, message, model
        self.round, self.stopped = 0, False

    async def run(self, body: dict, dispatch: Callable[[dict], Awaitable[dict]], *,
                  output_key: Literal['max_tokens', 'max_output_tokens'] = 'max_output_tokens') -> dict:
        if self.stopped:
            raise ModelGenerationError('permission')
        try:
            current = await self.access.current(self.identity.tenant_id, self.message, self.identity.employee_id)
            output = body.get(output_key)
            if (current.owner.user_id != self.identity.user_id or body.get('model') != self.model
                or type(output) is not int or output < 1):
                raise ModelGenerationError('permission')
            if len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode()) > current.policy.max_input_bytes:
                raise ModelGenerationError('invalid_request')
            await self.budget.reserve(current.owner.source, self.identity, provider_round=self.round,
                model=self.model, maximum_calls=current.policy.max_model_calls, message_id=self.message)
            self.round += 1
            await self.access.require(self.identity.tenant_id, self.message, self.identity.employee_id)
            bound = min(output, current.policy.max_output_tokens)
            result = await dispatch({**body, output_key: bound})
            await self.access.require(self.identity.tenant_id, self.message, self.identity.employee_id)
            return result
        except BaseException:
            self.stopped = True
            raise
