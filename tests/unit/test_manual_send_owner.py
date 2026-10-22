"""接手客户的员工可使用老板创建的任务；原创建人身份不授予客户权限。"""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from apps.api.composition.runtime import ResolvedManualSendGateway
from tool_gateway.pipeline import ToolCallContext


@pytest.mark.parametrize("actor,allowed", [("emp_new", True), ("emp_old", False)])
async def test_current_owner_can_send_boss_campaign(actor, allowed):
    class Employees:
        async def get_employee(self, tenant, employee, *, actor):
            return SimpleNamespace(employee_id=employee, is_active=True, role="sales")
        async def get_ownership(self, tenant, account, *, actor):
            assert tenant == "tn_test" and account == "acc_test"
            raise AssertionError("SYSTEM must not call the boss-only ownership reader")

    @asynccontextmanager
    async def employees(tenant):
        yield Employees()

    class ScopedGateway(ResolvedManualSendGateway):
        async def _current_owner(self, account_id):
            assert account_id == "acc_test"
            return "emp_new"

    gateway = ScopedGateway(None, None, employees, None, "tn_test")
    binding = SimpleNamespace(campaign_created_by="emp_old", account_id="acc_test")
    ctx = ToolCallContext(tenant_id="tn_test", user_id=actor, tool_id="email.send", params={})
    assert await gateway._authorized(ctx, binding) is allowed
