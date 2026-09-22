"""会话流程的受信公开依赖，不持有密钥或跨进程实例。"""

from dataclasses import dataclass
from typing import Protocol

from agent_runtime.assistant.context import AssistantContextBuilder
from agent_runtime.assistant.proposal import ResearchProposalBuilder
from agent_runtime.assistant.reads import AssistantReadPort, CurrentAssistantIdentity
from domains.assistant.service import (
    AssistantExecutionService,
    AssistantFingerprints,
    AssistantService,
    ModelConfigurationService,
)
from domains.directives.service import DirectiveService
from shared.schemas.model_invocation import ModelGenerationPort


class AssistantRuntimeService(AssistantService, AssistantExecutionService, Protocol):
    pass


class AssistantLifecycle(Protocol):
    async def startup(self) -> None: ...
    async def heartbeat(self) -> None: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class AssistantRuntimePorts:
    assistant_service: AssistantRuntimeService
    context_builder: AssistantContextBuilder
    read_port: AssistantReadPort
    model_generator: ModelGenerationPort
    proposal_builder: ResearchProposalBuilder
    directive_service: DirectiveService
    current_identity: CurrentAssistantIdentity
    fingerprints: AssistantFingerprints
    model: str
    configuration_version: str
    max_output_tokens: int
    configuration_service: ModelConfigurationService | None = None
    lifecycle: AssistantLifecycle | None = None
