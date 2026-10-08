"""平台控制租户的授权存储与固定企业只读统计，不提供业务写入入口。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.organization.platform_access import (
    EnterpriseDirectoryEntry,
    EnterpriseOverview,
    PlatformIdentity,
    PlatformMember,
)
from infra.db.tables import (
    AuthAccountRow,
    EmployeeRow,
    OpportunityRow,
    PlatformAccessAuditRow,
    PlatformAdminGrantRow,
    PlatformEnterpriseRow,
    ProspectAccountRow,
    ValidatedNeedRow,
    WorkflowRunRow,
)
from shared.authentication import AuthPrincipal
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId, new_id


@dataclass(frozen=True, repr=False)
class EnterpriseReaderBinding:
    """可信装配绑定固定企业引擎；不可从 HTTP 参数构造。"""
    tenant_id: TenantId
    engine: AsyncEngine


class PostgresPlatformAccess:
    """仅操作控制租户的 grant、目录和审计；不持有企业引擎。"""

    def __init__(
        self, factory: async_sessionmaker[AsyncSession], control_tenant: TenantId,
    ) -> None:
        self._factory = factory
        self._tenant = control_tenant

    async def _authorize(
        self, session: AsyncSession, principal: AuthPrincipal, *, lock: bool = False,
    ) -> PlatformIdentity:
        if principal.tenant_id != self._tenant:
            raise PermissionDenied("平台权限不足")
        statement = (
            select(PlatformAdminGrantRow, EmployeeRow.name, AuthAccountRow.username)
            .join(EmployeeRow, (
                (EmployeeRow.tenant_id == PlatformAdminGrantRow.tenant_id)
                & (EmployeeRow.employee_id == PlatformAdminGrantRow.employee_id)
                & (EmployeeRow.user_id == PlatformAdminGrantRow.user_id)
            ))
            .join(AuthAccountRow, (
                (AuthAccountRow.tenant_id == PlatformAdminGrantRow.tenant_id)
                & (AuthAccountRow.employee_id == PlatformAdminGrantRow.employee_id)
            ))
            .where(
                PlatformAdminGrantRow.tenant_id == self._tenant,
                PlatformAdminGrantRow.employee_id == principal.employee_id,
                PlatformAdminGrantRow.user_id == principal.user_id,
                PlatformAdminGrantRow.enabled.is_(True),
                EmployeeRow.tenant_id == self._tenant,
                EmployeeRow.is_active.is_(True),
                AuthAccountRow.tenant_id == self._tenant,
                AuthAccountRow.enabled.is_(True),
            )
        )
        if lock:
            statement = statement.with_for_update(read=True, of=PlatformAdminGrantRow)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise PermissionDenied("平台权限不足")
        return PlatformIdentity(username=row[2], display_name=row[1])

    async def authorize(self, principal: AuthPrincipal) -> PlatformIdentity:
        """不缓存授权，停用 grant 或账号后下个请求立即拒绝。"""
        async with self._factory() as session:
            return await self._authorize(session, principal)

    async def list_enterprises(self) -> tuple[EnterpriseDirectoryEntry, ...]:
        """目录本身属于控制租户；目标企业仅是数据字段。"""
        async with self._factory() as session:
            rows = (await session.scalars(
                select(PlatformEnterpriseRow)
                .where(PlatformEnterpriseRow.tenant_id == self._tenant)
                .order_by(PlatformEnterpriseRow.name, PlatformEnterpriseRow.enterprise_tenant_id)
            )).all()
            return tuple(EnterpriseDirectoryEntry(
                tenant_id=TenantId(row.enterprise_tenant_id),
                name=row.name, enabled=row.enabled,
            ) for row in rows)

    async def record_overview(
        self, principal: AuthPrincipal, enterprises: tuple[EnterpriseOverview, ...],
    ) -> None:
        """最终授权复核与审计同事务；撤权失败或审计失败不返回企业信息。"""
        async with self._factory.begin() as session:
            await self._authorize(session, principal, lock=True)
            # 空目录也记录一次查看；非空逐企业记录，禁止记录业务明文。
            for enterprise in enterprises or (None,):
                session.add(PlatformAccessAuditRow(
                    tenant_id=self._tenant, audit_id=new_id("aud"),
                    actor_employee_id=principal.employee_id,
                    actor_user_id=principal.user_id,
                    target_tenant_id=enterprise.tenant_id if enterprise else None,
                    action="overview", available=enterprise.available if enterprise else True,
                    created_at=datetime.now(UTC),
                ))


class PostgresEnterpriseOverviewReader:
    """仅使用绑定企业引擎，在 PostgreSQL 只读快照中执行白名单查询。"""

    def __init__(self, binding: EnterpriseReaderBinding) -> None:
        self._binding = binding

    async def read(self, enterprise: EnterpriseDirectoryEntry) -> EnterpriseOverview:
        """每个业务查询均显式带企业过滤；读取失败返回 unknown。"""
        if enterprise.tenant_id != self._binding.tenant_id:
            raise PermissionDenied("平台企业范围无效")
        try:
            async with self._binding.engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="REPEATABLE READ")
                async with connection.begin():
                    await connection.execute(text("SET TRANSACTION READ ONLY"))
                    await connection.execute(text("SET LOCAL statement_timeout = '5000'"))
                    rows = (await connection.execute(
                        select(EmployeeRow.name, EmployeeRow.role)
                        .where(
                            EmployeeRow.tenant_id == self._binding.tenant_id,
                            EmployeeRow.is_active.is_(True),
                        )
                        .order_by(EmployeeRow.name, EmployeeRow.employee_id)
                    )).all()
                    customers = await connection.scalar(select(func.count()).select_from(
                        ProspectAccountRow
                    ).where(ProspectAccountRow.tenant_id == self._binding.tenant_id))
                    needs = await connection.scalar(select(func.count()).select_from(
                        ValidatedNeedRow
                    ).where(ValidatedNeedRow.tenant_id == self._binding.tenant_id))
                    opportunities = await connection.scalar(select(func.count()).select_from(
                        OpportunityRow
                    ).where(OpportunityRow.tenant_id == self._binding.tenant_id))
                    tasks = await connection.scalar(select(func.count()).select_from(
                        WorkflowRunRow
                    ).where(
                        WorkflowRunRow.tenant_id == self._binding.tenant_id,
                        WorkflowRunRow.status == "running",
                    ))
                    return EnterpriseOverview(
                        **enterprise.model_dump(), available=True,
                        active_members=len(rows),
                        admins=sum(row.role == "boss" for row in rows),
                        employees=sum(row.role == "sales" for row in rows),
                        customers=int(customers or 0), validated_needs=int(needs or 0),
                        opportunities=int(opportunities or 0), active_tasks=int(tasks or 0),
                        members=tuple(PlatformMember(
                            name=row.name, role="admin" if row.role == "boss" else "employee",
                        ) for row in rows if row.role in {"boss", "sales"}),
                    )
        except SQLAlchemyError:
            return EnterpriseOverview(**enterprise.model_dump(), available=False)
