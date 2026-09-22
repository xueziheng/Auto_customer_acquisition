"""员工会话公开 DTO；正文有独立归属与当前权限投影。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import (
    AgentSessionId,
    AgentTurnId,
    EmployeeId,
    RunId,
    TenantId,
    UserId,
)
from shared.schemas.model_invocation import ModelFailureCode


class AssistantDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class AssistantActor(AssistantDTO):
    tenant_id: TenantId = Field(min_length=1)
    user_id: UserId = Field(min_length=1)
    employee_id: EmployeeId = Field(min_length=1)


class ObjectRef(AssistantDTO):
    kind: Literal["need", "opportunity", "handoff", "run", "product_doc"]
    object_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    version: str | None = Field(default=None, max_length=128)


class AssistantReadQuery(AssistantDTO):
    kind: Literal["need", "opportunity", "handoff", "run"]
    limit: int = Field(strict=True, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=1024, repr=False)


class AuthorizedFragment(AssistantDTO):
    text: str = Field(min_length=1, max_length=10000, repr=False)
    dependencies: tuple[ObjectRef, ...] = Field(max_length=50)
    source_turn_ids: tuple[AgentTurnId, ...] = Field(default=(), max_length=50)


class SourcedField(AssistantDTO):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=4000, repr=False)
    source_turn_id: AgentTurnId | None = None
    policy_version: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def one_source(self) -> SourcedField:
        if bool(self.source_turn_id) == bool(self.policy_version):
            raise ValueError("字段必须且只能有一个来源")
        return self


class Clarification(AssistantDTO):
    kind: Literal["clarify"] = "clarify"
    questions: tuple[str, ...] = Field(min_length=1, max_length=10, repr=False)
    missing_fields: tuple[str, ...] = Field(max_length=30)


class ReadRequest(AssistantDTO):
    kind: Literal["read"] = "read"
    refs: tuple[ObjectRef, ...] = Field(default=(), max_length=10)
    query: AssistantReadQuery | None = None

    @model_validator(mode="after")
    def one_read(self) -> ReadRequest:
        if bool(self.refs) == bool(self.query):
            raise ValueError("必须指定对象或查询之一")
        return self


class Explanation(AssistantDTO):
    kind: Literal["explain"] = "explain"
    fragments: tuple[AuthorizedFragment, ...] = Field(min_length=1, max_length=20)


class ResearchDraft(AssistantDTO):
    kind: Literal["research"] = "research"
    fields: tuple[SourcedField, ...] = Field(min_length=1, max_length=40)


AssistantDecision = Annotated[
    Clarification | ReadRequest | Explanation | ResearchDraft,
    Field(discriminator="kind"),
]
TurnState = Literal[
    "queued",
    "running",
    "awaiting_input",
    "proposal_ready",
    "completed",
    "blocked",
    "failed",
    "unknown",
    "cancelled",
]


class TurnInput(AssistantDTO):
    text: str = Field(min_length=1, max_length=10000, repr=False)
    object_refs: tuple[ObjectRef, ...] = Field(default=(), max_length=10)
    idempotency_key: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )


class SessionView(AssistantDTO):
    session_id: AgentSessionId
    created_at: datetime
    version: int


class TurnView(AssistantDTO):
    turn_id: AgentTurnId
    session_id: AgentSessionId
    run_id: RunId
    state: TurnState
    created_at: datetime
    input_text: str = Field(default="", repr=False)
    object_refs: tuple[ObjectRef, ...] = ()
    result: AssistantDecision | None = Field(default=None, repr=False)
    attempt_of: AgentTurnId | None = None
    proposal_id: str | None = None
    error_code: ModelFailureCode | None = None
    content_hidden: bool = False
    turn_kind: Literal["conversation", "model_probe"] = "conversation"


class TurnExecution(AssistantDTO):
    actor: AssistantActor
    turn: TurnView
    dispatch_state: Literal["pending", "bound"]
