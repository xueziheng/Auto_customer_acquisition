"""跨进程复用的授权编排，只依赖公共服务与当前事实端口。"""
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.service import EmployeeService
from domains.outreach.service import CampaignApprovalProvider, OutreachService
from domains.products.service import EnterpriseKnowledgeService
from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from domains.prospecting.inventory_followup import InventoryFollowupAuthorization
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId

EmployeeScopeFactory = Callable[[TenantId], AbstractAsyncContextManager[EmployeeService]]


class CustomerAgentScopeAuthority:
    """基于现有公共服务检查精确版本；执行阶段必须重新调用而非信任历史快照。"""

    def __init__(
        self, *, employees: EmployeeScopeFactory, lookup_actor: EmployeeActor,
        outreach: OutreachService, approvals: CampaignApprovalProvider | None,
        knowledge: EnterpriseKnowledgeService | None,
    ) -> None:
        self._employees, self._lookup = employees, lookup_actor
        self._outreach, self._approvals, self._knowledge = outreach, approvals, knowledge

    async def authorize(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        scope: InventoryFollowupScope,
        query: InventoryQuery,
    ) -> InventoryFollowupAuthorization:
        import hashlib
        import json
        from datetime import UTC, datetime

        from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
        from domains.outreach.schemas import CampaignApprovalState, CampaignState
        from domains.prospecting.inventory_followup import (
            InventoryFollowupAuthorization,
        )
        from shared.schemas.identifiers import CampaignId
        from workflows.enterprise_knowledge.confirmed_context import (
            ConfirmedContextReader,
        )

        async with self._employees(tenant_id) as service:
            employee = await service.get_employee(
                tenant_id, employee_id, actor=self._lookup
            )
            if (
                employee.tenant_id != tenant_id
                or employee.employee_id != employee_id
                or not employee.is_active
                or employee.role != "boss"
                or employee.user_id is None
            ):
                raise PermissionDenied("自动准备发起者权限已改变")
            actor = Actor(
                str(employee_id), OutreachScope(level=ScopeLevel.TENANT), "boss"
            )
            campaign = await self._outreach.get_campaign(
                tenant_id, CampaignId(scope.campaign_id), actor=actor
            )
            policy = campaign.boundary.customer_agent_policy
            if (
                campaign.tenant_id != tenant_id
                or campaign.campaign_id != scope.campaign_id
                or campaign.version != scope.campaign_version
                or campaign.state is not CampaignState.ACTIVE
                or policy is None
                or self._approvals is None
            ):
                raise PermissionDenied("自动准备需要精确已批准开发范围")
            approval = await self._approvals.get_campaign_approval(
                tenant_id, CampaignId(scope.campaign_id), scope.campaign_version
            )
            if (
                approval is None
                or approval.state is not CampaignApprovalState.APPROVED
                or approval.approval_id != campaign.approval_id
            ):
                raise PermissionDenied("自动准备范围批准已失效")
            owner = await service.get_employee(
                tenant_id,
                policy.owner_employee_id,
                actor=self._lookup,
            )
            if (
                owner.tenant_id != tenant_id
                or owner.employee_id != policy.owner_employee_id
                or not owner.is_active
                or owner.role not in {"boss", "manager", "sales"}
            ):
                raise PermissionDenied("自动准备负责人不可用")
        if (
            not query.target_countries
            or not set(query.target_countries).issubset(campaign.boundary.markets)
            or not set(query.product_tags).issubset(
                campaign.boundary.allowed_categories
            )
        ):
            raise PermissionDenied("搜索条件超出已批准开发范围")
        if not policy.confirmed_refs or self._knowledge is None:
            raise PermissionDenied("自动准备需补充明确确认资料并重新批准")
        await ConfirmedContextReader(self._knowledge).read(
            tenant_id, employee_id, policy.confirmed_refs
        )
        if query.confirmed_refs:
            if self._knowledge is None:
                raise PermissionDenied("企业确认资料不可用")
            await ConfirmedContextReader(
                self._knowledge
            ).read(tenant_id, employee_id, query.confirmed_refs)
        return InventoryFollowupAuthorization(
            tenant_id=tenant_id,
            actor_employee_id=employee_id,
            actor_user_id=employee.user_id,
            owner_employee_id=policy.owner_employee_id,
            scope=scope,
            policy_hash=hashlib.sha256(
                json.dumps(
                    policy.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            authorized_at=datetime.now(UTC),
            query=query,
        )
