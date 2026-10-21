"""库存的当前员工授权门面；存储租户绑定不能代替员工授权。"""

from dataclasses import dataclass
from typing import Protocol

from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    AcquisitionJobId,
    CandidateAllocationId,
    EmployeeId,
    TenantId,
)

from .inventory import (
    AcquisitionJob,
    CandidateAllocation,
    InventoryQuery,
    InventoryResult,
    InventoryService,
    InventoryStatus,
    InventoryTransferRequest,
)
from .inventory_followup import InventoryFollowupScope


@dataclass(frozen=True)
class InventoryAccessScope:
    """受信身份适配器提供当前范围；None仅表示已核实公司老板。"""

    tenant_id: TenantId
    employee_id: EmployeeId
    allowed_owners: frozenset[EmployeeId] | None


class InventoryAuthority(Protocol):
    async def current_scope(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> InventoryAccessScope:
        """重新读取在职状态和角色；老板全公司，其他员工仅本人，失败拒绝。"""
        ...


class ScopedInventory:
    """固定当前员工，每个公开方法都再次校验身份；后台仅用专用任务端口。"""

    def __init__(
        self,
        repository: InventoryService,
        authority: InventoryAuthority,
        tenant_id: TenantId,
        employee_id: EmployeeId,
    ) -> None:
        self._repository, self._authority = repository, authority
        self._tenant, self._employee = tenant_id, employee_id

    async def _authorize(self, tenant_id: TenantId) -> InventoryAccessScope:
        if tenant_id != self._tenant:
            raise PermissionDenied("候选租户访问被拒绝")
        scope = await self._authority.current_scope(tenant_id, self._employee)
        if scope.tenant_id != self._tenant or scope.employee_id != self._employee:
            raise PermissionDenied("候选身份绑定不一致")
        if (
            scope.allowed_owners is not None
            and self._employee not in scope.allowed_owners
        ):
            raise PermissionDenied("候选员工访问被拒绝")
        return scope

    async def _owners(
        self, tenant_id: TenantId, requested: tuple[EmployeeId, ...] | None
    ) -> tuple[EmployeeId, ...] | None:
        scope = await self._authorize(tenant_id)
        if scope.allowed_owners is not None and (
            requested is None or not set(requested).issubset({self._employee})
        ):
            raise PermissionDenied("候选归属范围被拒绝")
        return requested

    async def _boss(self, tenant_id: TenantId) -> None:
        """None仅由当前身份适配器授予老板，不接受历史角色快照。"""
        if (await self._authorize(tenant_id)).allowed_owners is not None:
            raise PermissionDenied("仅企业管理员可搜索、分配或读取公司任务")

    async def status(self, tenant_id: TenantId) -> InventoryStatus:
        await self._boss(tenant_id)
        return await self._repository.status(tenant_id)

    async def request(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        query: InventoryQuery,
        count: int,
        request_key: str,
        *,
        followup_scope: InventoryFollowupScope | None = None,
    ) -> InventoryResult:
        await self._boss(tenant_id)
        if employee_id != self._employee:
            raise PermissionDenied("不能冒用其他员工领取")
        if followup_scope is not None:
            return await self._repository.request(
                tenant_id, employee_id, query, count, request_key,
                followup_scope=followup_scope,
            )
        return await self._repository.request(
            tenant_id, employee_id, query, count, request_key
        )

    async def list_allocations(
        self,
        tenant_id: TenantId,
        allowed_owner_ids: tuple[EmployeeId, ...] | None,
        limit: int,
        cursor: str | None = None,
        *,
        unassigned_only: bool = False,
    ) -> tuple[CandidateAllocation, ...]:
        owners = await self._owners(tenant_id, allowed_owner_ids)
        if unassigned_only:
            await self._boss(tenant_id)
            return await self._repository.list_allocations(
                tenant_id, owners, limit, cursor, unassigned_only=True
            )
        return await self._repository.list_allocations(tenant_id, owners, limit, cursor)

    async def get_allocation(
        self, tenant_id: TenantId, allocation_id: CandidateAllocationId
    ) -> CandidateAllocation | None:
        """精确授权当前取得记录；未分配记录不属于任何普通员工。"""
        scope = await self._authorize(tenant_id)
        value = await self._repository.get_allocation(tenant_id, allocation_id)
        if value is not None and (
            value.tenant_id != self._tenant
            or (
                scope.allowed_owners is not None and value.employee_id != self._employee
            )
        ):
            return None
        return value

    async def assign(
        self,
        tenant_id: TenantId,
        allocation_ids: tuple[CandidateAllocationId, ...],
        employee_id: EmployeeId,
        *,
        acting_employee_id: EmployeeId,
    ) -> tuple[CandidateAllocation, ...]:
        """调用身份固定为当前老板；目标资格还须在存储事务内核验。"""
        await self._boss(tenant_id)
        if acting_employee_id != self._employee:
            raise PermissionDenied("不能冒用其他员工分配")
        return await self._repository.assign(
            tenant_id,
            allocation_ids,
            employee_id,
            acting_employee_id=self._employee,
        )

    async def transfer(
        self,
        tenant_id: TenantId,
        allocation_id: CandidateAllocationId,
        command: InventoryTransferRequest,
        *,
        acting_employee_id: EmployeeId,
    ) -> CandidateAllocation:
        """转交与首次分配独立授权，不放宽首次分配的冲突保护。"""
        await self._boss(tenant_id)
        if acting_employee_id != self._employee:
            raise PermissionDenied("不能冒用其他员工转交")
        return await self._repository.transfer(
            tenant_id, allocation_id, command, acting_employee_id=self._employee
        )

    async def list_jobs(
        self,
        tenant_id: TenantId,
        allowed_owner_ids: tuple[EmployeeId, ...] | None,
        limit: int,
    ) -> tuple[AcquisitionJob, ...]:
        await self._boss(tenant_id)
        return await self._repository.list_jobs(tenant_id, allowed_owner_ids, limit)

    async def get_job(
        self, tenant_id: TenantId, job_id: AcquisitionJobId
    ) -> AcquisitionJob | None:
        await self._boss(tenant_id)
        return await self._repository.get_job(tenant_id, job_id)

    async def set_daily_limit(self, tenant_id: TenantId, daily_limit: int) -> None:
        raise PermissionDenied("企业员工不能修改平台交付额度")
