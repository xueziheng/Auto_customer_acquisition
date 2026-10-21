"""发送适配保留原permission先后顺序，当前政策故障不得触发下游。"""
from types import SimpleNamespace

import pytest

from tool_gateway.checks.customer_preparation import CustomerPreparationPermissionCheck
from tool_gateway.pipeline import CheckRejection


@pytest.mark.parametrize("original_denied,guard_error", [(False,False),(False,True),(True,False)])
async def test_named_permission_adapter_preserves_order(original_denied, guard_error):
    trace = []
    rejection = CheckRejection("permission", "permission:original", "原权限拒绝")
    class Original:
        async def check(self, ctx, state):
            trace.append("original")
            return rejection if original_denied else None
    class Guard:
        async def require(self, tenant, enrollment):
            trace.append("cold_policy")
            if guard_error:
                raise RuntimeError("private provider error")
    result = await CustomerPreparationPermissionCheck(Original(),Guard()).check(SimpleNamespace(tenant_id="tenant", params={"enrollment_id":"enrollment"}), object())
    assert trace == (["original"] if original_denied else ["original","cold_policy"])
    assert (result is None) == (not original_denied and not guard_error)
    if guard_error:
        assert result.rule == "permission:customer_preparation"
