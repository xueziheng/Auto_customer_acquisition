"""Deal Cost & Quote：Phase 1 人工成本录入与只读报价检查。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from domains.costing.permissions import CostingActor, CostingScope
from domains.costing.schemas import (
    CostItemCreate,
    CostSheetCreate,
    CostSheetCreated,
    CostSheetView,
    QuoteReadiness,
    QuoteReadinessCheck,
)
from domains.costing.service import CostingService
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import CostSheetId, OpportunityId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_OPPORTUNITY_ID = re.compile(r"opp_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_COST_SHEET_ID = re.compile(r"cost_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_COSTING_ROLES = frozenset({"boss", "product", "sourcing", "finance"})


@dataclass(frozen=True)
class _CostingContext:
    identity: RequestIdentity
    actor: CostingActor


async def require_costing_context(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> _CostingContext:
    """第一道固定角色门；域服务仍执行第二道 typed action gate。"""
    if identity.employee.role not in _COSTING_ROLES:
        raise PermissionDenied("Phase 1 成本 API 授权拒绝")
    return _CostingContext(
        identity=identity,
        actor=CostingActor(
            actor_id=str(identity.employee.employee_id),
            role=identity.employee.role,
            scope=CostingScope.TENANT,
        ),
    )


def _service(dependencies: ConfiguredApiDependencies) -> CostingService:
    if dependencies.costing is None:
        raise TransientError("成本服务未配置")
    return dependencies.costing


def _opportunity_id(value: str) -> OpportunityId:
    if _OPPORTUNITY_ID.fullmatch(value) is None:
        raise ValidationError("机会标识无效")
    return OpportunityId(value)


def _cost_sheet_id(value: str) -> CostSheetId:
    if _COST_SHEET_ID.fullmatch(value) is None:
        raise ValidationError("成本表标识无效")
    return CostSheetId(value)


@router.post(
    "/opportunities/{opportunity_id}/cost-sheets",
    response_model=CostSheetCreated,
    status_code=201,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def create_cost_sheet(
    opportunity_id: str,
    body: CostSheetCreate,
    context: Annotated[_CostingContext, Depends(require_costing_context)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CostSheetCreated:
    created = await _service(dependencies).create_sheet(
        context.identity.tenant_id,
        _opportunity_id(opportunity_id),
        body,
        actor=context.actor,
    )
    return CostSheetCreated(cost_sheet_id=str(created))


@router.get(
    "/opportunities/{opportunity_id}/cost-sheets",
    response_model=list[CostSheetView],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_cost_sheets(
    opportunity_id: str,
    context: Annotated[_CostingContext, Depends(require_costing_context)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> list[CostSheetView]:
    return await _service(dependencies).list_versions(
        context.identity.tenant_id,
        _opportunity_id(opportunity_id),
        actor=context.actor,
    )


@router.get(
    "/cost-sheets/{cost_sheet_id}",
    response_model=CostSheetView,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def get_cost_sheet(
    cost_sheet_id: str,
    context: Annotated[_CostingContext, Depends(require_costing_context)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CostSheetView:
    return await _service(dependencies).get_sheet(
        context.identity.tenant_id,
        _cost_sheet_id(cost_sheet_id),
        actor=context.actor,
    )


@router.post(
    "/cost-sheets/{cost_sheet_id}/items",
    status_code=204,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def add_cost_item(
    cost_sheet_id: str,
    body: CostItemCreate,
    context: Annotated[_CostingContext, Depends(require_costing_context)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> Response:
    await _service(dependencies).add_item(
        context.identity.tenant_id,
        _cost_sheet_id(cost_sheet_id),
        body,
        actor=context.actor,
    )
    return Response(status_code=204)


@router.post(
    "/cost-sheets/{cost_sheet_id}/readiness",
    response_model=QuoteReadiness,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def assess_quote_readiness(
    cost_sheet_id: str,
    body: QuoteReadinessCheck,
    context: Annotated[_CostingContext, Depends(require_costing_context)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> QuoteReadiness:
    return await _service(dependencies).assess_for_quote(
        context.identity.tenant_id,
        _cost_sheet_id(cost_sheet_id),
        tuple(body.expected_item_types),
        actor=context.actor,
    )
