"""租户绑定开户事务：复用现有认证、锁顺序、强密码和账号唯一约束。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.employees.schemas import EmployeeView
from infra.authentication.service import PostgresAuthentication
from infra.db.tables import AuthAccountRow, EmployeeRow
from shared.authentication import AuthenticationInputInvalid
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
from workflows.employee_accounts import AccountCreationTransaction


class _Transaction:
    def __init__(
        self, session: AsyncSession, tenant: TenantId, auth: PostgresAuthentication
    ) -> None:
        self._session, self._tenant, self._auth = session, tenant, auth

    async def requester(self, employee_id: EmployeeId) -> EmployeeView | None:
        # 与登录、账号停用共享相同的租户桶 -> 账号 -> 员工锁顺序。
        await self._auth.lock_account_management(self._session)
        account = await self._session.scalar(
            select(AuthAccountRow)
            .where(
                AuthAccountRow.tenant_id == self._tenant,
                AuthAccountRow.employee_id == employee_id,
            )
            .with_for_update()
        )
        if account is None or not account.enabled:
            return None
        row = await self._session.scalar(
            select(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == self._tenant,
                EmployeeRow.employee_id == employee_id,
            )
            .with_for_update(read=True)
        )
        if row is None:
            return None
        return EmployeeView(
            tenant_id=self._tenant,
            employee_id=EmployeeId(row.employee_id),
            user_id=UserId(row.user_id) if row.user_id else None,
            name=row.name,
            role=row.role,
            is_active=row.is_active,
        )

    async def insert(
        self,
        *,
        username: str,
        name: str,
        password: SecretStr,
        manager: EmployeeId,
    ) -> EmployeeView:
        # 重复用户名拒绝，绝不重置、重启用或接管已存在的账号。
        exists = await self._session.scalar(
            select(AuthAccountRow.username).where(
                AuthAccountRow.tenant_id == self._tenant,
                AuthAccountRow.username == username,
            )
        )
        if exists is not None:
            raise ValidationError("登录名已存在，请核对团队列表")
        employee_id, user_id = EmployeeId(new_id("emp")), UserId(new_id("usr"))
        self._session.add(
            EmployeeRow(
                tenant_id=self._tenant,
                employee_id=employee_id,
                user_id=user_id,
                name=name,
                role="sales",
                manager_id=manager,
                is_active=True,
            )
        )
        await self._session.flush()
        await self._auth.create_account(
            username, password, employee_id, session=self._session
        )
        return EmployeeView(
            tenant_id=self._tenant,
            employee_id=employee_id,
            user_id=user_id,
            name=name,
            role="sales",
            manager_id=manager,
        )


class PostgresAccountCreation:
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId) -> None:
        self._factory, self._tenant = factory, tenant_id
        self._auth = PostgresAuthentication(factory, tenant_id)

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AccountCreationTransaction]:
        try:
            async with self._factory.begin() as session:
                yield _Transaction(session, self._tenant, self._auth)
        except (SQLAlchemyError, AuthenticationInputInvalid):
            raise ValidationError("账号创建未完成，请核对输入和团队列表") from None
