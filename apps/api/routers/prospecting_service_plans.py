"""三档服务的只读权益、显式研究排程与可追溯证据归类；不提供自助赋权。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from domains.employees.permissions import EmployeeAction
from domains.prospecting.service import (
    ProspectingServicePlanStore,
    ScreeningEvidenceBody,
    ServicePlanScheduleBody,
    ServicePlanStatusView,
    tier_policy,
)
from shared.errors import TransientError
from workflows.demand_discovery.screening_application import (
    ProspectScreeningApplication,
    ScreeningDetailView,
    ServicePlanScheduleApplication,
)

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
    require_employee_action,
)
from ..identity import RequestIdentity

router = APIRouter(
    dependencies=[
        Depends(
            require_employee_action(
                EmployeeAction.OWNERSHIP_READ, allowed_roles=frozenset({"boss"})
            )
        )
    ]
)
_write_gate = Depends(
    require_employee_action(
        EmployeeAction.OWNERSHIP_LOCK, allowed_roles=frozenset({"boss"})
    )
)
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]



def _store(dependencies: ConfiguredApiDependencies) -> ProspectingServicePlanStore:
    if dependencies.prospecting_service_plans is None:
        raise TransientError("获客服务计划尚未配置")
    return dependencies.prospecting_service_plans


@router.get("/service-plan", response_model=ServicePlanStatusView)
async def get_service_plan(
    identity: Identity, dependencies: Dependencies
) -> ServicePlanStatusView:
    return ServicePlanStatusView(
        catalog=[tier_policy(tier) for tier in ("basic", "standard", "advanced")],
        plan=await _store(dependencies).get(identity.tenant_id),
    )


@router.put(
    "/service-plan/schedule",
    response_model=ServicePlanStatusView,
    dependencies=[_write_gate],
)
async def configure_service_schedule(
    body: ServicePlanScheduleBody,
    identity: Identity,
    dependencies: Dependencies,
) -> ServicePlanStatusView:
    """经原授权入口配置已确认目标；实际周期仍由统一任务重核。"""
    await ServicePlanScheduleApplication(_store(dependencies), dependencies.directives).configure_target(
        identity.tenant_id, employee_id=identity.employee.employee_id,
        user_id=identity.employee.user_id, body=body,
        knowledge=dependencies.enterprise_knowledge.service if dependencies.enterprise_knowledge else None)
    return await get_service_plan(identity, dependencies)


def _screening(dependencies: ConfiguredApiDependencies) -> ProspectScreeningApplication:
    return ProspectScreeningApplication(
        dependencies.prospecting,
        dependencies.screening_evidence,
        dependencies.prospecting_service_plans,
    )


@router.get("/accounts/{account_id}/screening", response_model=ScreeningDetailView)
async def get_screening(
    account_id: str, identity: Identity, dependencies: Dependencies
) -> ScreeningDetailView:
    result = await _screening(dependencies).get(identity.tenant_id, account_id)
    return ScreeningDetailView(
        assessment=result.assessment,
        evidence=list(result.evidence),
        eligible_signals=list(result.eligible_signals),
    )


@router.post(
    "/accounts/{account_id}/screening/evidence",
    response_model=ScreeningDetailView,
    dependencies=[_write_gate],
)
async def record_screening_evidence(
    account_id: str,
    body: ScreeningEvidenceBody,
    identity: Identity,
    dependencies: Dependencies,
) -> ScreeningDetailView:
    """人工仅归类已关联的原始信号；来源、摘录与确认身份都由服务端绑定。"""
    result = await _screening(dependencies).record(
        identity.tenant_id,
        account_id,
        reviewer=identity.employee.employee_id,
        kind=body.kind,
        signal_id=body.signal_id,
        occurred_at=body.occurred_at,
    )
    return ScreeningDetailView(
        assessment=result.assessment,
        evidence=list(result.evidence),
        eligible_signals=list(result.eligible_signals),
    )
