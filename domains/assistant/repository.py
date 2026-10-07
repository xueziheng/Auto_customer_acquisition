from collections.abc import Awaitable, Callable

"""会话持久化公开给组合层，身份包含租户且每次校验会话归属。"""

from typing import Protocol

from domains.assistant.schemas import (
    AssistantActor,
    AssistantDecision,
    ModelConfigurationSnapshot,
    ModelSettingsUpdate,
    ObjectRef,
    SessionView,
    TurnExecution,
    TurnInput,
    TurnState,
    TurnView,
)
from shared.schemas.identifiers import AgentSessionId, AgentTurnId, RunId, TenantId
from shared.schemas.model_invocation import ModelFailureCode


class AssistantRepository(Protocol):
    async def create(self, actor: AssistantActor) -> SessionView: ...
    async def sessions(self, actor: AssistantActor) -> list[SessionView]: ...
    async def accept(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        input: TurnInput,
        request_hmac: str,
        run_id: RunId,
        *,
        attempt_of: AgentTurnId | None = None,
    ) -> TurnView: ...
    async def turns(
        self, actor: AssistantActor, session_id: AgentSessionId
    ) -> list[TurnView]: ...
    async def get(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> TurnView: ...
    async def transition(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        target: TurnState,
        *,
        result: AssistantDecision | None = None,
        proposal_id: str | None = None,
        error_code: ModelFailureCode | None = None,
    ) -> TurnView: ...
    async def pending(self, tenant_id: TenantId, limit: int) -> list[TurnExecution]: ...
    async def execution(
        self, tenant_id: TenantId, turn_id: AgentTurnId
    ) -> TurnExecution: ...
    async def bind(
        self, tenant_id: TenantId, turn_id: AgentTurnId, run_id: RunId
    ) -> None: ...

    async def checkpoint(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        sequence: int,
        result: AssistantDecision,
        refs: tuple[ObjectRef, ...],
    ) -> None: ...
    async def fail_turn(
        self,
        tenant_id: TenantId,
        turn_id: AgentTurnId,
        state: TurnState,
        code: ModelFailureCode,
    ) -> None: ...


class AssistantConfigurationRepository(Protocol):
    async def get(self, tenant_id: TenantId) -> ModelConfigurationSnapshot | None: ...
    async def save(self, actor: AssistantActor, input: ModelSettingsUpdate) -> None: ...
    async def probe(
        self, actor: AssistantActor, configuration_version: str, idempotency_key: str
    ) -> TurnView: ...
    async def probe_version(self, tenant_id: TenantId, turn_id: AgentTurnId) -> str: ...
    async def complete_probe(
        self,
        actor: AssistantActor,
        turn_id: AgentTurnId,
        configuration_version: str,
        authorize: Callable[[], Awaitable[None]],
    ) -> None: ...
