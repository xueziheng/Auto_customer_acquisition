"""历史内容依赖闭包：撤权、删除或版本变化后不复用派生结论。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from agent_runtime.assistant.reads import (
    PRODUCT_REF,
    AssistantReadPort,
    CurrentAssistantIdentity,
)
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from domains.assistant.schemas import (
    AssistantActor,
    AuthorizedFragment,
    Explanation,
    ObjectRef,
    ReadRequest,
    TurnView,
)
from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import AgentSessionId, AgentTurnId


class HistoryPort(Protocol):
    async def turns(
        self, actor: AssistantActor, session_id: AgentSessionId
    ) -> list[TurnView]: ...


def filter_fragments(
    fragments: Sequence[AuthorizedFragment], visible_refs: frozenset[ObjectRef]
) -> tuple[AuthorizedFragment, ...]:
    return tuple(f for f in fragments if set(f.dependencies).issubset(visible_refs))


def dependencies(turn: TurnView) -> set[ObjectRef]:
    refs = {*turn.object_refs, *turn.context_refs}
    if isinstance(turn.result, Explanation):
        for f in turn.result.fragments:
            refs.update(f.dependencies)
    if isinstance(turn.result, ReadRequest):
        refs.update(turn.result.refs)
    return refs


class HistoryProjector:
    def __init__(
        self,
        identity: CurrentAssistantIdentity,
        reads: AssistantReadPort,
        history: HistoryPort,
    ) -> None:
        self._identity, self._reads, self._history = identity, reads, history

    async def project(self, actor: AssistantActor, turn: TurnView) -> TurnView:
        role, _ = await self._identity.resolve(actor)
        history = await self._history.turns(actor, turn.session_id)
        # 必须能确定从第一轮起的依赖；仓储在超过上限时失败而不静默丢失早期限制。
        refs: set[ObjectRef] = set()
        for prior in history:
            refs.update(dependencies(prior))
            if prior.turn_id == turn.turn_id:
                break
        refs.update(dependencies(turn))
        try:
            for ref in refs:
                current = await self._reads.read(actor, ref)
                if not any(
                    r.kind == ref.kind
                    and r.object_id == ref.object_id
                    and (ref.version is None or r.version == ref.version)
                    for r in current.dependencies
                ):
                    raise ValidationError("来源已经变化")
        except TradeOSError:
            return turn.model_copy(
                update={
                    "input_text": "",
                    "object_refs": (),
                    "context_refs": (),
                    "result": None,
                    "proposal_id": None,
                    "content_hidden": True,
                    "can_view_run": False,
                }
            )
        return turn.model_copy(
            update={"can_view_run": role == "boss" and not turn.content_hidden}
        )


@dataclass(frozen=True)
class AssistantContext:
    actor: AssistantActor
    role: str
    capabilities: frozenset[str]
    fragments: tuple[AuthorizedFragment, ...]
    turns: tuple[TurnView, ...]
    trimmed_count: int
    configuration_version: str

    def payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "capabilities": sorted(self.capabilities),
            "configuration_version": self.configuration_version,
            "untrusted_history": [
                t.model_dump(
                    mode="json", exclude={"run_id", "proposal_id", "error_code"}
                )
                for t in self.turns
            ],
            "untrusted_sources": [f.model_dump(mode="json") for f in self.fragments],
            "trimmed_count": self.trimmed_count,
        }


class AssistantContextBuilder:
    def __init__(
        self,
        identity: CurrentAssistantIdentity,
        reads: AssistantReadPort,
        history: HistoryPort,
        projector: HistoryProjector,
        *,
        configuration_version: str,
        max_bytes: int,
    ) -> None:
        self._identity, self._reads, self._history, self._projector = (
            identity,
            reads,
            history,
            projector,
        )
        self._configuration_version, self._max_bytes = configuration_version, max_bytes

    async def build(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> AssistantContext:
        role, capabilities = await self._identity.resolve(actor)
        turns: list[TurnView] = []
        hidden = 0
        refs: set[ObjectRef] = {PRODUCT_REF}
        found = False
        for original in await self._history.turns(actor, session_id):
            if original.turn_kind != "conversation":
                continue
            turn = await self._projector.project(actor, original)
            if turn.content_hidden:
                hidden += 1
            else:
                turns.append(turn)
                refs.update(dependencies(turn))
            if turn.turn_id == turn_id:
                found = True
                if turn.content_hidden:
                    raise ValidationError("当前上下文来源不可见")
                break
        if not found:
            raise ValidationError("当前上下文轮次不可用")
        fragments = tuple(
            [
                await self._reads.read(actor, ref)
                for ref in sorted(
                    refs, key=lambda r: (r.kind, r.object_id, r.version or "")
                )
            ]
        )
        context = AssistantContext(
            actor,
            role,
            capabilities,
            fragments,
            tuple(turns),
            hidden,
            self._configuration_version,
        )
        payload = json.dumps(context.payload(), ensure_ascii=False, sort_keys=True)
        CredentialMarkerGuard().check(subject=None, body=payload)
        if len(payload.encode()) > self._max_bytes:
            raise ValidationError("上下文超限，请新建会话并明确研究范围及限制")
        return context
