"""单副本 scheduler 内认领会话意图；使用同一规范 engine。"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from domains.assistant.service import AssistantExecutionService
from shared.errors import ValidationError
from shared.schemas.identifiers import TenantId
from workflows.assistant.ports import AssistantLifecycle
from workflows.engine.runner import StepStatus, WorkflowEngine


class AssistantDispatcher:
    def __init__(
        self, service: AssistantExecutionService, engine: WorkflowEngine
    ) -> None:
        self._service, self._engine = service, engine

    async def dispatch(self, tenant_id: TenantId, limit: int) -> int:
        count = 0
        for execution in await self._service.pending(tenant_id, limit):
            turn = execution.turn
            existing = await self._engine.get_run(tenant_id, turn.run_id)
            if existing is not None and existing.status in {
                StepStatus.FAILED,
                StepStatus.CANCELLED,
                StepStatus.COMPLETED,
            }:
                await self._service.fail_turn(
                    tenant_id, turn.turn_id, "unknown", "unknown"
                )
                continue
            if existing is None:
                run_id = await self._engine.start_once(
                    tenant_id,
                    turn.run_id,
                    "assistant",
                    str(turn.turn_id),
                    {"turn_id": str(turn.turn_id)},
                )
            else:
                if (
                    existing.workflow_type != "assistant"
                    or existing.subject_ref != turn.turn_id
                    or existing.context.get("turn_id") != turn.turn_id
                ):
                    raise ValidationError("会话运行身份不匹配")
                run_id = existing.run_id
            if run_id != turn.run_id:
                raise ValidationError("会话运行身份不匹配")
            await self._service.bind(tenant_id, turn.turn_id, run_id)
            count += 1
        return count


class AssistantDriver:
    def __init__(
        self,
        dispatcher: AssistantDispatcher,
        tenant_id: TenantId,
        limit: int,
        lifecycle: AssistantLifecycle | None = None,
    ) -> None:
        self._dispatcher, self._tenant_id, self._limit = dispatcher, tenant_id, limit
        self._lifecycle = lifecycle

    async def start_heartbeat(self, guard: Callable[[], Awaitable[None]]) -> None:
        if self._lifecycle is not None:
            await self._lifecycle.start_heartbeat(guard)

    async def stop_heartbeat(self) -> None:
        if self._lifecycle is not None:
            await self._lifecycle.stop_heartbeat()

    async def scan_once(self) -> int:
        if self._lifecycle is not None:
            await self._lifecycle.heartbeat()
        return await self._dispatcher.dispatch(self._tenant_id, self._limit)


class ExistingActivation(Protocol):
    async def activate(self) -> None: ...


class AssistantRuntimeActivation:
    def __init__(
        self, existing: ExistingActivation | None, lifecycle: AssistantLifecycle
    ) -> None:
        self._existing, self._lifecycle = existing, lifecycle

    async def activate(self) -> None:
        if self._existing is not None:
            await self._existing.activate()
        await self._lifecycle.startup()
