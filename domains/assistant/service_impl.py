"""输入先检查再接纳；终态重生成是独立尝试，不批准或重放业务动作。"""

from domains.assistant.errors import AssistantConflict
from domains.assistant.repository import AssistantRepository
from domains.assistant.schemas import (
    AssistantActor,
    AssistantDecision,
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
    AssistantTurnProjector,
)
from shared.schemas.identifiers import (
    AgentSessionId,
    AgentTurnId,
    RunId,
    TenantId,
    new_id,
)
from shared.schemas.model_invocation import ModelFailureCode


class AssistantServiceImpl:
    def __init__(
        self,
        repository: AssistantRepository,
        authority: AssistantAuthority,
        guard: AssistantInputGuard,
        fingerprints: AssistantFingerprints,
        projector: AssistantTurnProjector,
    ) -> None:
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
        return await self._repo.transition(actor, session_id, turn_id, "cancelled")

    async def regenerate(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        idempotency_key: str,
    ) -> TurnView:
        await self._authority.check(actor)
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
        return await self._repo.accept(
            actor, session_id, input, digest, RunId(new_id("run")), attempt_of=turn_id
        )

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
