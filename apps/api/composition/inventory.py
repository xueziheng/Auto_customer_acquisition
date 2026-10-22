"""把企业员工公共服务适配为库存域的第二道当前授权。"""

from domains.prospecting.inventory_access import InventoryAccessScope, ScopedInventory
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
