"""私有会话公共服务；当前授权和历史投影由受信装配显式注入。"""

from typing import Protocol

from domains.assistant.schemas import (
    AssistantActor,
    AssistantDecision,
    ModelSettingsUpdate,
    ModelSettingsView,
    ObjectRef,
    SessionView,
    TurnExecution,
    TurnInput,
    TurnState,
    TurnView,
)
from shared.schemas.identifiers import AgentSessionId, AgentTurnId, RunId, TenantId
from shared.schemas.model_invocation import ModelFailureCode


class AssistantAuthority(Protocol):
    async def check(self, actor: AssistantActor) -> None: ...


class AssistantInputGuard(Protocol):
    def check(self, *, subject: str | None, body: str) -> None: ...


class AssistantFingerprints(Protocol):
    def fingerprint(self, parts: tuple[bytes, ...]) -> tuple[str, str]: ...


class AssistantTurnProjector(Protocol):
    async def project(self, actor: AssistantActor, turn: TurnView) -> TurnView: ...


class AssistantService(Protocol):
    async def create_session(self, actor: AssistantActor) -> SessionView: ...
    async def list_sessions(self, actor: AssistantActor) -> list[SessionView]: ...
    async def accept_turn(
        self, actor: AssistantActor, session_id: AgentSessionId, input: TurnInput
    ) -> TurnView: ...
    async def list_turns(
        self, actor: AssistantActor, session_id: AgentSessionId
    ) -> list[TurnView]: ...
    async def get_turn(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> TurnView: ...
    async def cancel_turn(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> TurnView: ...
    async def regenerate(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        idempotency_key: str,
    ) -> TurnView: ...


class AssistantExecutionService(Protocol):
    """只注入受信 scheduler，不挂载为用户可调用的万能动作。"""

    async def pending(self, tenant_id: TenantId, limit: int) -> list[TurnExecution]: ...
    async def execution(
        self, tenant_id: TenantId, turn_id: AgentTurnId
    ) -> TurnExecution: ...
    async def bind(
        self, tenant_id: TenantId, turn_id: AgentTurnId, run_id: RunId
    ) -> None: ...
    async def deliver(
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


class AssistantModelAdministration(AssistantAuthority, Protocol):
    async def require_admin(self, actor: AssistantActor) -> None: ...


class ModelConfigurationService(Protocol):
    async def get_public(self, actor: AssistantActor) -> ModelSettingsView: ...
    async def save_nonsecret(
        self, actor: AssistantActor, input: ModelSettingsUpdate
    ) -> ModelSettingsView: ...
    async def request_probe(
        self, actor: AssistantActor, idempotency_key: str
    ) -> TurnView: ...
    async def authorize(
        self, actor: AssistantActor, configuration_version: str, *, probe: bool
    ) -> None: ...
    async def probe_version(self, tenant_id: TenantId, turn_id: AgentTurnId) -> str: ...
    async def complete_probe(
        self, actor: AssistantActor, turn_id: AgentTurnId, configuration_version: str
    ) -> None: ...
