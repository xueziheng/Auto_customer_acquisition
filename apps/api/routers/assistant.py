"""登录员工的私有会话 API；不在 HTTP 请求内调用模型。"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from apps.api.dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from apps.api.identity import RequestIdentity
from apps.api.middleware import ApiErrorResponse
from apps.api.validation_route import ExplicitValidationRoute
from domains.assistant.errors import AssistantConflict, AssistantNotFound
from domains.assistant.schemas import (
    AssistantActor,
    AssistantRegenerateInput,
    EmptyAssistantCommand,
    SessionView,
    TurnInput,
    TurnView,
)
from domains.assistant.service import AssistantService
from shared.authentication import AuthPrincipal
from shared.schemas.identifiers import AgentSessionId, AgentTurnId
from shared.schemas.model_invocation import ModelGenerationError

router = APIRouter(
    prefix="/agent",
    tags=["assistant"],
    route_class=ExplicitValidationRoute,
    responses={
        status: {"model": ApiErrorResponse} for status in (401, 403, 404, 409, 422, 503)
    },
)
T = TypeVar("T")


async def _safe[T](result: Awaitable[T]) -> T:
    try:
        return await result
    except AssistantNotFound:
        raise HTTPException(404) from None
    except AssistantConflict:
        raise HTTPException(409) from None
    except ModelGenerationError as error:
        raise HTTPException(
            429 if error.code == "quota" else 403 if error.code == "permission" else 503
        ) from None


async def _access(
    request: Request,
    response: Response,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> tuple[AssistantActor, AssistantService]:
    principal = getattr(request.state, "auth_principal", None)
    if (
        not isinstance(principal, AuthPrincipal)
        or principal.user_id != identity.employee.user_id
    ):
        raise HTTPException(401)
    if request.query_params:
        raise HTTPException(422)
    if dependencies.assistant is None:
        raise HTTPException(503)
    response.headers["Cache-Control"] = "no-store"
    return AssistantActor(
        tenant_id=identity.tenant_id,
        user_id=principal.user_id,
        employee_id=identity.employee.employee_id,
    ), dependencies.assistant


Access = Annotated[tuple[AssistantActor, AssistantService], Depends(_access)]


@router.post("/sessions", response_model=SessionView, status_code=201)
async def create_session(body: EmptyAssistantCommand, access: Access) -> SessionView:
    del body
    actor, service = access
    return await _safe(service.create_session(actor))


@router.get("/sessions", response_model=list[SessionView])
async def list_sessions(access: Access) -> list[SessionView]:
    actor, service = access
    return await _safe(service.list_sessions(actor))


@router.get("/sessions/{session_id}/turns", response_model=list[TurnView])
async def list_turns(session_id: str, access: Access) -> list[TurnView]:
    actor, service = access
    return await _safe(service.list_turns(actor, AgentSessionId(session_id)))


@router.post("/sessions/{session_id}/turns", response_model=TurnView, status_code=202)
async def accept_turn(session_id: str, body: TurnInput, access: Access) -> TurnView:
    actor, service = access
    return await _safe(service.accept_turn(actor, AgentSessionId(session_id), body))


@router.get("/sessions/{session_id}/turns/{turn_id}", response_model=TurnView)
async def get_turn(session_id: str, turn_id: str, access: Access) -> TurnView:
    actor, service = access
    return await _safe(
        service.get_turn(actor, AgentSessionId(session_id), AgentTurnId(turn_id))
    )


@router.post("/sessions/{session_id}/turns/{turn_id}/cancel", response_model=TurnView)
async def cancel_turn(
    session_id: str, turn_id: str, body: EmptyAssistantCommand, access: Access
) -> TurnView:
    del body
    actor, service = access
    return await _safe(
        service.cancel_turn(actor, AgentSessionId(session_id), AgentTurnId(turn_id))
    )


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/regenerate",
    response_model=TurnView,
    status_code=202,
)
async def regenerate(
    session_id: str, turn_id: str, body: AssistantRegenerateInput, access: Access
) -> TurnView:
    actor, service = access
    return await _safe(
        service.regenerate(
            actor,
            AgentSessionId(session_id),
            AgentTurnId(turn_id),
            body.idempotency_key,
        )
    )
