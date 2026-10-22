"""普通对话实际Codex/Gateway模型装配；平台任务pin与每provider轮scope预算同时执行。"""
from collections.abc import Callable
from datetime import timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.composition_support.model import configured_codex_provider
from apps.scheduler_worker.adapters.dialogue_model_access import DialogueProviderBudget
from infra.db.dialogue_model_budget import SqlDialogueModelBudget
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.standalone.settings import StandaloneModelSettings
from infra.tenant_model import resolve_task_model
from shared.schemas.identifiers import MessageId
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationPort
from tool_gateway.checks.model import CurrentModelAuthority
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import GatewayModelGenerator, ModelProvider
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.reply_qualification.dialogue_authority import (
    CurrentOrdinaryDialogueAccess,
)


class OrdinaryDialogueModels:
    """只有内部受信调用身份可构造；provider只在Gateway执行时创建。"""
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: StandaloneModelSettings,
                 access: CurrentOrdinaryDialogueAccess, authority: CurrentModelAuthority,
                 fingerprints: HmacFingerprintProvider, owner: str,
                 provider_factory: Callable[[DialogueProviderBudget, InvocationIdentity], ModelProvider] | None = None) -> None:
        self.sessions, self.settings, self.access, self.authority = sessions, settings, access, authority
        self.fingerprints, self.owner, self.provider_factory = fingerprints, owner, provider_factory

    async def select(self, identity: InvocationIdentity, message: MessageId) -> tuple[str, ModelGenerationPort]:
        await self.authority.check(identity)
        current = await self.access.current(identity.tenant_id, message, identity.employee_id)
        selected = resolve_task_model(self.settings, identity)
        bounded = selected.model_copy(update={'limits': selected.limits.model_copy(update={
            'max_input_bytes': min(selected.limits.max_input_bytes, current.policy.max_input_bytes),
            'timeout_seconds': min(selected.limits.timeout_seconds, 60),
            'max_output_tokens': min(selected.limits.max_output_tokens, current.policy.max_output_tokens),
        })})
        budget = DialogueProviderBudget(self.access, SqlDialogueModelBudget(self.sessions, identity.tenant_id), identity, message, selected.model)
        generator = GatewayModelGenerator(authority=self.authority, usage=SqlModelUsageRepository(self.sessions),
            provider_factory=lambda: self.provider_factory(budget, identity) if self.provider_factory else configured_codex_provider(bounded, identity, round_budget=budget),
            limits=bounded.limits, model=selected.model, provider=selected.provider,
            configuration_version=selected.configuration_version,
            ledger_factory=lambda tenant: cast(ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self.sessions, tenant)),
            fingerprints=self.fingerprints, lease_owner=self.owner,
            lease_duration=timedelta(seconds=bounded.limits.timeout_seconds + 30))
        return selected.model, generator
