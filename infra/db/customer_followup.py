"""本人客户当前身份/归属 guard 与真实 active 开发任务 ID 读取。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.customer_followup import CustomerContext
from infra.db.prospect_inventory_tables import CandidateAllocationRow
from infra.db.tables import EmployeeRow, OutreachCampaignRow, OwnershipLockRow
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    CandidateAllocationId,
    EmployeeId,
    TenantId,
    UserId,
)


class PostgresCustomerContextReader:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId
    ) -> None:
        self._factory, self._tenant = factory, tenant_id

    def _check(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant:
            raise PermissionDenied("客户企业范围不一致")

    @asynccontextmanager
    async def guard(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        allocation_id: CandidateAllocationId | None,
    ) -> AsyncIterator[CustomerContext]:
        """NO KEY UPDATE兼容Outreach外键；共享归属锁防转派但不阻止读。"""
        self._check(tenant_id)
        async with self._factory() as session, session.begin():
            employee = (
                await session.scalars(
                    select(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == tenant_id,
                        EmployeeRow.employee_id == employee_id,
                    )
                    .with_for_update(key_share=True)
                )
            ).one_or_none()
            if employee is None:
                raise PermissionDenied("当前员工不可跟进客户")
            row = (
                None
                if allocation_id is None
                else (
                    await session.scalars(
                        select(CandidateAllocationRow)
                        .where(
                            CandidateAllocationRow.tenant_id == tenant_id,
                            CandidateAllocationRow.allocation_id == allocation_id,
                        )
                        .with_for_update(read=True)
                    )
                ).one_or_none()
            )
            allocation = None
            owner = None
            if row is not None:
                # 与库存公开读取采用同一快照转换，SQL列是归属的事实来源。
                from infra.db.prospect_inventory import PostgresInventoryService

                allocation = PostgresInventoryService._allocation(row)
                if row.account_id is not None:
                    lock = (
                        await session.scalars(
                            select(OwnershipLockRow)
                            .where(
                                OwnershipLockRow.tenant_id == tenant_id,
                                OwnershipLockRow.account_id == row.account_id,
                            )
                            .with_for_update(read=True)
                        )
                    ).one_or_none()
                    owner = EmployeeId(lock.owner) if lock else None
            yield CustomerContext(
                tenant_id,
                employee_id,
                UserId(employee.user_id) if employee.user_id else None,
                employee.role,
                employee.is_active,
                allocation,
                owner,
            )

    async def active_campaign_ids(self, tenant_id: TenantId) -> tuple[str, ...]:
        self._check(tenant_id)
        async with self._factory() as session:
            ids = (
                await session.scalars(
                    select(OutreachCampaignRow.campaign_id)
                    .where(
                        OutreachCampaignRow.tenant_id == tenant_id,
                        OutreachCampaignRow.state == "active",
                    )
                    .order_by(OutreachCampaignRow.campaign_id)
                    .limit(201)
                )
            ).all()
            if len(ids) > 200:
                raise ValidationError("活跃开发任务超出读取上限")
            return tuple(ids)
