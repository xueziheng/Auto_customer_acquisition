"""机会创建 HTTP 场景的薄编排。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    OpportunityView,
    ValidatedNeedEvidence,
)
from domains.opportunities.service import OpportunityService
from shared.schemas.identifiers import ProspectAccountId

from ..dependencies import EmployeeServiceScope

if TYPE_CHECKING:
    from ..identity import RequestIdentity


async def create_opportunity_from_validated_need(
    *,
    opportunities: OpportunityService,
    employee_services: EmployeeServiceScope,
    identity: RequestIdentity,
    request: OpportunityCreateRequest,
    evidence: ValidatedNeedEvidence,
) -> OpportunityView | None:
    """按既定顺序把已验证需求转为机会并完成首次归属。

    机会域先决定是否通过硬门槛；返回 ``None`` 时绝不创建员工服务 scope，
    以免为不存在的机会建立归属锁。通过后由员工域解析 owner，再交回机会域
    分配并读取最终公共视图。业务规则均留在两个域服务内。
    """
    opportunity_id = await opportunities.create_from_need(
        identity.tenant_id,
        request,
        evidence,
        actor=identity.opportunity_actor,
    )
    if opportunity_id is None:
        return None

    async with employee_services(identity.tenant_id) as employees:
        ownership = await employees.resolve_owner(
            identity.tenant_id,
            ProspectAccountId(request.account_id),
            actor=identity.employee_actor,
            country=request.country,
            need_category=request.product_category,
        )
    await opportunities.assign(
        identity.tenant_id,
        opportunity_id,
        ownership.owner,
        identity.employee.employee_id,
        actor=identity.opportunity_actor,
    )
    return await opportunities.get(
        identity.tenant_id,
        opportunity_id,
        actor=identity.opportunity_actor,
    )
