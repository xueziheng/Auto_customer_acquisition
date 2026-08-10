"""CRM & Opportunities 的机会 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, TypeAdapter

from domains.opportunities.errors import HandoffAlreadyAcceptedError
from domains.opportunities.permissions import OpportunityAction
from domains.opportunities.schemas import (
    HandoffPacketView,
    HandoffQueueItemView,
    OpportunityCreateRequest,
    OpportunityView,
    ValidatedNeedEvidence,
)
from domains.opportunities.service import LossReason, OpportunityState
from shared.schemas.identifiers import HandoffId, OpportunityId

from ..composition.opportunity_intake import (
    create_opportunity_from_validated_need,
)
from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    require_opportunity_action,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

_CRM_ROLES = frozenset({"sales", "manager", "boss"})
_BOSS_ROLE = frozenset({"boss"})


class OpportunityIntakeBody(BaseModel):
    """仅承载两个机会域 DTO 的 HTTP 嵌套，不复制业务字段。"""

    model_config = ConfigDict(extra="forbid")

    request: OpportunityCreateRequest
    evidence: ValidatedNeedEvidence


class OpportunityTransitionBody(BaseModel):
    """状态转换的 typed 请求体。"""

    model_config = ConfigDict(extra="forbid")

    target: OpportunityState


class OpportunityMarkLostBody(BaseModel):
    """失败原因与可选补充说明的 typed 请求体。"""

    model_config = ConfigDict(extra="forbid")

    reason: LossReason
    detail: str | None = None


_opportunity_intake_adapter = TypeAdapter(OpportunityIntakeBody)


async def _parse_opportunity_intake(request: Request) -> OpportunityIntakeBody:
    """从原始 JSON 经 TypeAdapter 解析，保留 Money 的 JSON string 边界。"""
    return _opportunity_intake_adapter.validate_json(await request.body())

router = APIRouter()


@router.post(
    "/opportunities",
    response_model=OpportunityView,
    status_code=status.HTTP_201_CREATED,
    responses={204: {"description": "未通过机会硬门槛"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/OpportunityIntakeBody"}
                }
            },
        }
    },
)
async def create_opportunity(
    body: Annotated[OpportunityIntakeBody, Depends(_parse_opportunity_intake)],
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.OPPORTUNITY_CREATE, allowed_roles=_BOSS_ROLE
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> OpportunityView | Response:
    """从已验证需求创建机会；硬门槛未通过时返回无内容。"""
    result = await create_opportunity_from_validated_need(
        opportunities=dependencies.opportunities,
        employee_services=dependencies.employees,
        identity=identity,
        request=body.request,
        evidence=body.evidence,
    )
    if result is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result


@router.get("/opportunities", response_model=list[OpportunityView])
async def list_opportunities(
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.OPPORTUNITY_LIST, allowed_roles=_CRM_ROLES
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    states: Annotated[list[OpportunityState] | None, Query()] = None,
    limit: Annotated[int, Query(gt=0)] = 50,
) -> list[OpportunityView]:
    """以请求身份的精确机会 scope 列出机会。"""
    return await dependencies.opportunities.list_opportunities(
        identity.tenant_id,
        identity.opportunity_actor,
        scope=identity.opportunity_actor.scope,
        states=states,
        limit=limit,
    )


@router.get("/opportunities/{opportunity_id}", response_model=OpportunityView)
async def get_opportunity(
    opportunity_id: str,
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.OPPORTUNITY_READ, allowed_roles=_CRM_ROLES
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> OpportunityView:
    """读取机会公共视图（含 provenance 摘要）。"""
    return await dependencies.opportunities.get(
        identity.tenant_id,
        OpportunityId(opportunity_id),
        actor=identity.opportunity_actor,
    )


@router.post("/opportunities/{opportunity_id}/transition")
async def transition_opportunity(
    opportunity_id: str,
    body: OpportunityTransitionBody,
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.OPPORTUNITY_TRANSITION, allowed_roles=_CRM_ROLES
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> None:
    """请求机会域执行受状态机约束的转换。"""
    await dependencies.opportunities.transition(
        identity.tenant_id,
        OpportunityId(opportunity_id),
        body.target,
        actor=identity.opportunity_actor,
    )


@router.post("/opportunities/{opportunity_id}/mark-lost")
async def mark_opportunity_lost(
    opportunity_id: str,
    body: OpportunityMarkLostBody,
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.OPPORTUNITY_MARK_LOST, allowed_roles=_CRM_ROLES
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> None:
    """以已断言员工身份确认并记录机会失败。"""
    await dependencies.opportunities.mark_lost(
        identity.tenant_id,
        OpportunityId(opportunity_id),
        body.reason,
        actor=identity.opportunity_actor,
        confirmed_by=identity.employee.employee_id,
        confirmed_at=datetime.now(UTC),
        detail=body.detail,
    )


@router.get("/handoffs", response_model=list[HandoffQueueItemView])
async def list_pending_handoffs(
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.HANDOFF_QUEUE_READ,
                allowed_roles=_CRM_ROLES,
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(gt=0)] = 50,
) -> list[HandoffQueueItemView]:
    """按机会域给出的最久等待优先顺序返回待接管队列。"""
    return await dependencies.opportunities.list_pending_handoffs(
        identity.tenant_id,
        identity.opportunity_actor,
        limit=limit,
    )


@router.get("/handoffs/{handoff_id}", response_model=HandoffPacketView)
async def get_handoff_packet(
    handoff_id: str,
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.HANDOFF_READ,
                allowed_roles=_CRM_ROLES,
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> HandoffPacketView:
    """返回机会域组装的完整接管包。"""
    return await dependencies.opportunities.get_handoff_packet(
        identity.tenant_id,
        HandoffId(handoff_id),
        actor=identity.opportunity_actor,
    )


@router.post(
    "/handoffs/{handoff_id}/accept",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={
        409: {
            "model": ApiErrorResponse,
            "description": "接管已被接受",
        }
    },
)
async def accept_handoff(
    handoff_id: str,
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.HANDOFF_ACCEPT,
                allowed_roles=_CRM_ROLES,
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> Response:
    """以已断言员工身份原子接受接管；并发失败只返回固定安全冲突。"""
    try:
        await dependencies.opportunities.accept_handoff(
            identity.tenant_id,
            HandoffId(handoff_id),
            identity.employee.employee_id,
            actor=identity.opportunity_actor,
        )
    except HandoffAlreadyAcceptedError:
        payload = ApiErrorResponse(
            code="handoff_already_accepted",
            message="接管已被接受",
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=payload.model_dump(mode="json"),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/analytics/loss-reasons",
    response_model=dict[str, dict[str, int]],
)
async def loss_reason_breakdown(
    identity: Annotated[
        RequestIdentity,
        Depends(
            require_opportunity_action(
                OpportunityAction.LOSS_REASON_READ,
                allowed_roles=_BOSS_ROLE,
            )
        ),
    ],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    since_days: Annotated[int, Query(gt=0)] = 30,
) -> dict[str, dict[str, int]]:
    """返回机会域已完成租户级 ABAC 的失败原因二维聚合。"""
    return await dependencies.opportunities.loss_reason_breakdown(
        identity.tenant_id,
        actor=identity.opportunity_actor,
        since_days=since_days,
    )
