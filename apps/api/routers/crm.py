"""CRM & Opportunities 的机会 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, TypeAdapter

from domains.opportunities.permissions import OpportunityAction
from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    OpportunityView,
    ValidatedNeedEvidence,
)
from domains.opportunities.service import LossReason, OpportunityState
from shared.schemas.identifiers import OpportunityId

from ..composition.opportunity_intake import (
    create_opportunity_from_validated_need,
)
from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    require_opportunity_action,
)
from ..identity import RequestIdentity

_CRM_ROLES = frozenset({"sales", "manager", "boss"})


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
                OpportunityAction.OPPORTUNITY_CREATE, allowed_roles=_CRM_ROLES
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
