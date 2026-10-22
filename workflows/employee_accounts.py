"""管理员创建普通员工：当前身份复核与原子账号写入编排。"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from pydantic import SecretStr

from domains.employees.permissions import StandardAuditLogger
from domains.employees.schemas import EmployeeView
from domains.employees.service import validate_employee_account_creation
from shared.authentication import AuthenticationInputInvalid, normalize_login_username
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId


class AccountCreationTransaction(Protocol):
    async def requester(self, employee_id: EmployeeId) -> EmployeeView | None: ...

    async def insert(
        self, *, username: str, name: str, password: SecretStr, manager: EmployeeId
    ) -> EmployeeView: ...


AccountCreationFactory = Callable[
    [], AbstractAsyncContextManager[AccountCreationTransaction]
]


class EmployeeAccountCreation:
    """只绑定一个业务企业，不注册为 Agent 工具，不提供提权或密码读取。"""

    def __init__(
        self, tenant_id: TenantId, transaction: AccountCreationFactory
    ) -> None:
        self._tenant_id = tenant_id
        self._transaction = transaction

    async def create(
        self,
        *,
        tenant_id: TenantId,
        actor_id: EmployeeId,
        user_id: UserId | None,
        username: str,
        name: str,
        password: SecretStr,
    ) -> EmployeeView:
        if tenant_id != self._tenant_id or user_id is None:
            raise PermissionDenied("员工开户企业或身份无效")
        try:
            username = normalize_login_username(username)
        except AuthenticationInputInvalid:
            raise ValidationError("登录名无效") from None
        async with self._transaction() as transaction:
            requester = await transaction.requester(actor_id)
            validate_employee_account_creation(
                tenant_id, requester=requester, name=name, role="sales"
            )
            if requester is None or requester.user_id != user_id:
                raise PermissionDenied("员工开户身份已变更")
            StandardAuditLogger().log(
                actor=str(actor_id),
                action="account_create",
                tenant_id=tenant_id,
                scope="tenant",
                rule="current-active-boss:ordinary-employee-only",
            )
            return await transaction.insert(
                username=username,
                name=name.strip(),
                password=password,
                manager=actor_id,
            )
