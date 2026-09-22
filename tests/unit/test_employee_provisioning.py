"""可信初始化复用员工域的角色与经理事实校验；不授予运行时权限。"""

import pytest

from domains.employees.schemas import EmployeeView
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId


def test_valid_employee_provisioning():
    from domains.employees.service import validate_employee_provisioning

    for role in (
        "boss",
        "manager",
        "sales",
        "sourcing",
        "product",
        "finance",
        "viewer",
    ):
        validate_employee_provisioning(
            TenantId("t"), name="员工", role=role, manager=None
        )
    for role in ("boss", "manager"):
        validate_employee_provisioning(
            TenantId("t"),
            name="员工",
            role="sales",
            manager=EmployeeView(
                employee_id=EmployeeId("m"),
                tenant_id=TenantId("t"),
                name="经理",
                role=role,
            ),
        )


@pytest.mark.parametrize(
    "fault", ["role", "name", "foreign", "inactive", "sales_manager"]
)
def test_invalid_employee_provisioning(fault):
    from domains.employees.service import validate_employee_provisioning

    manager = EmployeeView(
        employee_id=EmployeeId("m"),
        tenant_id=TenantId("other" if fault == "foreign" else "t"),
        name="经理",
        role="sales" if fault == "sales_manager" else "manager",
        is_active=fault != "inactive",
    )
    with pytest.raises(ValidationError):
        validate_employee_provisioning(
            TenantId("t"),
            name=" " if fault == "name" else "员工",
            role="invalid" if fault == "role" else "sales",
            manager=manager,
        )
