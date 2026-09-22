"""模型网关消费当前会话身份及配置；不从模型请求中解析授权。"""

from domains.assistant.schemas import AssistantActor
from domains.assistant.service import (
    AssistantExecutionService,
    ModelConfigurationService,
)
from shared.errors import PermissionDenied
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError


class AssistantModelAuthority:
    def __init__(
        self,
        configuration: ModelConfigurationService,
        execution: AssistantExecutionService,
    ) -> None:
        self._configuration, self._execution = configuration, execution

    async def check(self, identity: InvocationIdentity) -> None:
        actor = AssistantActor(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            employee_id=identity.employee_id,
        )
        try:
            await self._configuration.authorize(
                actor,
                identity.configuration_version,
                probe=identity.capability == "model_probe",
            )
        except PermissionDenied:
            raise ModelGenerationError("permission") from None
        if identity.turn_id is not None:
            execution = await self._execution.execution(
                identity.tenant_id, identity.turn_id
            )
            if (
                execution.actor != actor
                or execution.turn.run_id != identity.run_id
                or execution.turn.state != "running"
            ):
                raise ModelGenerationError("permission")
            if (execution.turn.turn_kind == "model_probe") != (
                identity.capability == "model_probe"
            ):
                raise ModelGenerationError("permission")
