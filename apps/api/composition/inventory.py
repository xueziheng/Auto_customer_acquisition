"""把企业员工公共服务适配为库存域的第二道当前授权。"""

from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from domains.prospecting.inventory_access import InventoryAccessScope, ScopedInventory
from domains.prospecting.inventory_followup import InventoryFollowupAuthorization
from domains.prospecting.service import InventoryService
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId

from ..dependencies import ConfiguredApiDependencies
from ..identity import RequestIdentity


class CurrentInventoryAuthority:
    def __init__(
        self, dependencies: ConfiguredApiDependencies, identity: RequestIdentity
    ) -> None:
        self._dependencies, self._identity = dependencies, identity

    async def current_scope(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> InventoryAccessScope:
        if (
            tenant_id != self._identity.tenant_id
            or employee_id != self._identity.employee.employee_id
        ):
            raise PermissionDenied("候选访问身份不一致")
        async with self._dependencies.employees(tenant_id) as service:
            current = await service.get_employee(
                tenant_id, employee_id, actor=self._dependencies.employee_lookup_actor
            )
            if (
                current.tenant_id != tenant_id
                or current.employee_id != employee_id
                or current.user_id != self._identity.employee.user_id
                or not current.is_active
                or current.role not in {"boss", "manager", "sales"}
            ):
                raise PermissionDenied("候选员工权限已改变")
            owners = None
            if current.role != "boss":
                owners = {employee_id}
            return InventoryAccessScope(
                tenant_id,
                employee_id,
                frozenset(owners) if owners is not None else None,
            )


def bind_inventory(
    repository: InventoryService,
    dependencies: ConfiguredApiDependencies,
    identity: RequestIdentity,
) -> InventoryService:
    """每个请求新建绑定门面，不在应用状态缓存某员工的权限快照。"""
    return ScopedInventory(
        repository,
        CurrentInventoryAuthority(dependencies, identity),
        identity.tenant_id,
        identity.employee.employee_id,
    )


class CurrentCustomerAgentAuthority:
    """基于现有公共服务检查精确版本；执行阶段必须重新调用而非信任历史快照。"""

    def __init__(self, dependencies: ConfiguredApiDependencies) -> None:
        self._dependencies = dependencies

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

        from apps.composition_support.campaign_approval_reader import (
            CurrentCampaignApprovalReader,
        )
        from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
        from domains.outreach.schemas import CampaignApprovalState, CampaignState
        from domains.prospecting.inventory_followup import (
            InventoryFollowupAuthorization,
        )
        from shared.schemas.identifiers import CampaignId
        from workflows.enterprise_knowledge.confirmed_context import (
            ConfirmedContextReader,
        )

        dependencies = self._dependencies
        async with dependencies.employees(tenant_id) as service:
            employee = await service.get_employee(
                tenant_id, employee_id, actor=dependencies.employee_lookup_actor
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
            campaign = await dependencies.outreach.get_campaign(
                tenant_id, CampaignId(scope.campaign_id), actor=actor
            )
            policy = campaign.boundary.customer_agent_policy
            if (
                campaign.tenant_id != tenant_id
                or campaign.campaign_id != scope.campaign_id
                or campaign.version != scope.campaign_version
                or campaign.state is not CampaignState.ACTIVE
                or policy is None
                or dependencies.approvals is None
            ):
                raise PermissionDenied("自动准备需要精确已批准开发范围")
            approval = await CurrentCampaignApprovalReader(
                tenant_id, dependencies.approvals, now=lambda: datetime.now(UTC)
            ).get_campaign_approval(
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
                actor=dependencies.employee_lookup_actor,
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
        if not policy.confirmed_refs or dependencies.enterprise_knowledge is None:
            raise PermissionDenied("自动准备需补充明确确认资料并重新批准")
        await ConfirmedContextReader(dependencies.enterprise_knowledge.service).read(
            tenant_id, employee_id, policy.confirmed_refs
        )
        if query.confirmed_refs:
            if dependencies.enterprise_knowledge is None:
                raise PermissionDenied("企业确认资料不可用")
            await ConfirmedContextReader(
                dependencies.enterprise_knowledge.service
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
