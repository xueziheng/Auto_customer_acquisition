"""回复能力身份必须先匹配受托范围，不能借用聊天或研究身份。"""

import pytest

from domains.employees.permissions import Actor, EmployeeScope
from shared.schemas.model_invocation import InvocationIdentity, ModelGenerationError


class UnreachablePorts:
    """越权身份在任何业务数据读取之前拒绝。"""

    def __getattr__(self, name):
        pytest.fail(f"越权身份触发了端口读取：{name}")


@pytest.mark.parametrize(
    "patch",
    [
        {"tenant_id": "tn_other"},
        {"employee_id": "emp_other"},
        {"capability": "research"},
        {"capability": "business_read"},
        {"turn_id": "atr_chat"},
        {"sequence": 1},
    ],
)
async def test_wrong_reply_identity_is_rejected_before_reading_binding(patch):
    from apps.scheduler_worker.reply_model_binding import ReplyModelAuthority

    ports = UnreachablePorts()
    authority = ReplyModelAuthority(
        ports,
        ports,
        Actor("system:reply-model", EmployeeScope.SYSTEM, "system"),
        ports,
        tenant_id="tn_reply",
        employee_id="emp_reply",
        model="test-model",
    )
    identity = InvocationIdentity.model_validate(
        {
            "tenant_id": "tn_reply",
            "user_id": "usr_reply",
            "employee_id": "emp_reply",
            "run_id": "run_reply",
            "capability": "reply_qualification",
            "configuration_version": "reply-v1",
            "sequence": 0,
            **patch,
        }
    )
    with pytest.raises(ModelGenerationError) as caught:
        await authority.check(identity)
    assert caught.value.code == "permission"
