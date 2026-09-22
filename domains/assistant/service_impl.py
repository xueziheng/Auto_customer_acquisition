"""输入先检查再接纳；终态重生成是独立尝试，不批准或重放业务动作。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from domains.assistant.errors import AssistantConflict
from domains.assistant.repository import (
    AssistantConfigurationRepository,
    AssistantRepository,
)
from domains.assistant.schemas import (
    AssistantActor,
    AssistantDecision,
    ModelConfigurationSnapshot,
    ModelSettingsUpdate,
    ModelSettingsView,
    ObjectRef,
    SessionView,
    TurnExecution,
    TurnInput,
    TurnState,
    TurnView,
)
from domains.assistant.service import (
    AssistantAuthority,
    AssistantFingerprints,
    AssistantInputGuard,
    AssistantModelAdministration,
    AssistantTurnProjector,
)
from shared.schemas.identifiers import (
    AgentSessionId,
    AgentTurnId,
    RunId,
    TenantId,
    new_id,
)
from shared.schemas.model_invocation import ModelFailureCode, ModelGenerationError


class AssistantServiceImpl:
    def __init__(
        self,
        repository: AssistantRepository,
        authority: AssistantAuthority,
        guard: AssistantInputGuard,
        fingerprints: AssistantFingerprints,
        projector: AssistantTurnProjector,
        generation_guard: AssistantAuthority | None = None,
    ) -> None:
        self._generation_guard = generation_guard
        (
            self._repo,
            self._authority,
            self._guard,
            self._fingerprints,
            self._projector,
        ) = repository, authority, guard, fingerprints, projector

    async def create_session(self, actor: AssistantActor) -> SessionView:
        await self._authority.check(actor)
        return await self._repo.create(actor)

    async def list_sessions(self, actor: AssistantActor) -> list[SessionView]:
        await self._authority.check(actor)
        return await self._repo.sessions(actor)

    async def accept_turn(
        self, actor: AssistantActor, session_id: AgentSessionId, input: TurnInput
    ) -> TurnView:
        await self._authority.check(actor)
        if self._generation_guard is not None:
            await self._generation_guard.check(actor)
        self._guard.check(subject=None, body=input.text)
        digest, _ = self._fingerprints.fingerprint(
            (
                actor.model_dump_json().encode(),
                str(session_id).encode(),
                input.model_dump_json().encode(),
            )
        )
        turn = await self._repo.accept(
            actor, session_id, input, digest, RunId(new_id("run"))
        )
        return await self._projector.project(actor, turn)

    async def list_turns(
        self, actor: AssistantActor, session_id: AgentSessionId
    ) -> list[TurnView]:
        await self._authority.check(actor)
        return [
            await self._projector.project(actor, turn)
            for turn in await self._repo.turns(actor, session_id)
        ]

    async def get_turn(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> TurnView:
        await self._authority.check(actor)
        return await self._projector.project(
            actor, await self._repo.get(actor, session_id, turn_id)
        )

    async def cancel_turn(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> TurnView:
        await self._authority.check(actor)
        return await self._projector.project(
            actor, await self._repo.transition(actor, session_id, turn_id, "cancelled")
        )

    async def regenerate(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        idempotency_key: str,
    ) -> TurnView:
        await self._authority.check(actor)
        if self._generation_guard is not None:
            await self._generation_guard.check(actor)
        original = await self.get_turn(actor, session_id, turn_id)
        if original.state not in {"failed", "unknown"} or original.content_hidden:
            raise AssistantConflict()
        input = TurnInput(
            text=original.input_text,
            object_refs=original.object_refs,
            idempotency_key=idempotency_key,
        )
        self._guard.check(subject=None, body=input.text)
        digest, _ = self._fingerprints.fingerprint(
            (
                actor.model_dump_json().encode(),
                str(session_id).encode(),
                input.model_dump_json().encode(),
                str(turn_id).encode(),
            )
        )
        regenerated = await self._repo.accept(
            actor, session_id, input, digest, RunId(new_id("run")), attempt_of=turn_id
        )
        return await self._projector.project(actor, regenerated)

    async def pending(self, tenant_id: TenantId, limit: int) -> list[TurnExecution]:
        """仅供规范 scheduler 的租户扫描，不暴露到 HTTP。"""
        return await self._repo.pending(tenant_id, limit)

    async def execution(
        self, tenant_id: TenantId, turn_id: AgentTurnId
    ) -> TurnExecution:
        """仅供受信编排读取绑定身份；后续仍必须重新授权。"""
        return await self._repo.execution(tenant_id, turn_id)

    async def bind(
        self, tenant_id: TenantId, turn_id: AgentTurnId, run_id: RunId
    ) -> None:
        await self._repo.bind(tenant_id, turn_id, run_id)

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
    ) -> TurnView:
        """受信编排交付已校验结果；取消或终态不允许迟到覆盖。"""
        await self._authority.check(actor)
        return await self._repo.transition(
            actor,
            session_id,
            turn_id,
            target,
            result=result,
            proposal_id=proposal_id,
            error_code=error_code,
        )

    async def checkpoint(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        sequence: int,
        result: AssistantDecision,
        refs: tuple[ObjectRef, ...],
    ) -> None:
        """授权后保存已校验中间结果，崩溃恢复不再调用模型。"""
        await self._authority.check(actor)
        await self._repo.checkpoint(actor, session_id, turn_id, sequence, result, refs)

    async def fail_turn(
        self,
        tenant_id: TenantId,
        turn_id: AgentTurnId,
        state: TurnState,
        code: ModelFailureCode,
    ) -> None:
        """仅供受信后台失败关闭；员工撤权后仍可隐藏结果，不能产生成功内容。"""
        await self._repo.fail_turn(tenant_id, turn_id, state, code)


class ModelConfigurationServiceImpl:
    """非秘密配置与显式探测；验证只适用于仍匹配的活跃进程版本。"""

    def __init__(
        self,
        repository: AssistantConfigurationRepository,
        authority: AssistantModelAdministration,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._repository, self._authority, self._now = repository, authority, now

    def view(self, snapshot: ModelConfigurationSnapshot | None) -> ModelSettingsView:
        if snapshot is None:
            return ModelSettingsView(status="missing")
        runtime = snapshot.runtime
        ttl = timedelta(seconds=max(90, snapshot.limits.timeout_seconds * 2))
        alive = all(
            heartbeat is not None and timedelta(0) <= self._now() - heartbeat <= ttl
            for heartbeat in (runtime.api_heartbeat_at, runtime.scheduler_heartbeat_at)
        )
        matches = (
            runtime.api_version
            == runtime.scheduler_version
            == snapshot.configuration_version
        )
        enabled = alive and matches and snapshot.model_data_export_enabled
        status: Literal[
            "missing", "pending_restart", "unverified", "verified", "failed"
        ] = "pending_restart" if not matches else "unverified"
        if enabled:
            if runtime.failure_code is not None:
                status = "failed"
            elif runtime.verified_at is not None:
                status = "verified"
        return ModelSettingsView(
            model=snapshot.model,
            configuration_version=snapshot.configuration_version,
            status=status,
            verified_at=runtime.verified_at,
            failure_code=runtime.failure_code,
            limits=snapshot.limits,
            can_probe=enabled,
            worker_available=alive,
            model_data_export_enabled=snapshot.model_data_export_enabled,
            probe_turn_id=runtime.probe_turn_id,
            probe_state=runtime.probe_state,
        )

    async def get_public(self, actor: AssistantActor) -> ModelSettingsView:
        await self._authority.require_admin(actor)
        return self.view(await self._repository.get(actor.tenant_id))

    async def save_nonsecret(
        self, actor: AssistantActor, input: ModelSettingsUpdate
    ) -> ModelSettingsView:
        await self._authority.require_admin(actor)
        await self._repository.save(actor, input)
        return await self.get_public(actor)

    async def request_probe(
        self, actor: AssistantActor, idempotency_key: str
    ) -> TurnView:
        snapshot = await self.get_public(actor)
        if not snapshot.can_probe or snapshot.configuration_version is None:
            raise ModelGenerationError("configuration")
        return await self._repository.probe(
            actor, snapshot.configuration_version, idempotency_key
        )

    async def authorize(
        self, actor: AssistantActor, configuration_version: str, *, probe: bool
    ) -> None:
        await self._authority.check(actor)
        if probe:
            await self._authority.require_admin(actor)
        view = self.view(await self._repository.get(actor.tenant_id))
        if (
            view.configuration_version != configuration_version
            or not view.can_probe
            or (not probe and view.status != "verified")
        ):
            raise ModelGenerationError("configuration")

    async def probe_version(self, tenant_id: TenantId, turn_id: AgentTurnId) -> str:
        return await self._repository.probe_version(tenant_id, turn_id)

    async def complete_probe(
        self, actor: AssistantActor, turn_id: AgentTurnId, configuration_version: str
    ) -> None:
        await self.authorize(actor, configuration_version, probe=True)
        await self._repository.complete_probe(
            actor,
            turn_id,
            configuration_version,
            lambda: self.authorize(actor, configuration_version, probe=True),
        )
