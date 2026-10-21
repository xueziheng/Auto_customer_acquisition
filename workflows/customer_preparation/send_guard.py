"""受托入组在真正发送前重新读取当前事实，准备完成不等同永久发送授权。"""
from typing import Protocol

from domains.compliance.service import ComplianceService
from domains.employees.permissions import Actor, EmployeeScope
from domains.outreach.service import OutreachService
from domains.prospecting.inventory_followup import PreparationBinding
from domains.prospecting.service import ProspectingService
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EnrollmentId, ProspectAccountId, TenantId
from workflows.customer_preparation.authority import (
    CustomerAgentScopeAuthority,
    EmployeeScopeFactory,
)
from workflows.customer_preparation.service import require_cold_preparation_policy


class SendBindingReader(Protocol):
    async def read(self, tenant_id: TenantId, enrollment_id: EnrollmentId) -> PreparationBinding | None: ...


class CurrentCustomerPreparationSendGuard:
    def __init__(self, reader: SendBindingReader, authority: CustomerAgentScopeAuthority, employees: EmployeeScopeFactory,
                 prospecting: ProspectingService, compliance: ComplianceService, outreach: OutreachService) -> None:
        self.reader, self.authority, self.employees = reader, authority, employees
        self.prospecting, self.compliance, self.outreach = prospecting, compliance, outreach

    async def require(self, tenant_id: TenantId, enrollment_id: EnrollmentId) -> None:
        binding = await self.reader.read(tenant_id, enrollment_id)
        if binding is None:
            return
        if binding.tenant_id != tenant_id:
            raise PermissionDenied("发送准备企业范围不一致")
        await self.authority.check(binding.authorization)
        account_id = ProspectAccountId(binding.result_refs["account_id"])
        async with self.employees(tenant_id) as service:
            owner = await service.get_ownership(tenant_id, account_id,
                actor=Actor(str(binding.authorization.actor_employee_id), EmployeeScope.TENANT, "boss"))
        if owner is None or owner.owner != binding.authorization.owner_employee_id or owner.locked_at != binding.ownership_locked_at:
            raise PermissionDenied("发送准备归属已改变")
        account = await self.prospecting.get_account(tenant_id, account_id)
        if account.tenant_id != tenant_id:
            raise PermissionDenied("发送准备客户企业范围不一致")
        await require_cold_preparation_policy(binding, account.country, compliance=self.compliance, outreach=self.outreach)
