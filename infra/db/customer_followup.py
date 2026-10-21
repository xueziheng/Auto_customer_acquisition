"""本人客户当前身份/归属 guard 与真实 active 开发任务 ID 读取。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.prospecting.customer_followup import (
    CustomerContext,
    CustomerPreparationStatus,
)
from domains.prospecting.inventory_followup import (
    PREPARATION_BLOCK_REASONS,
    InventoryFollowupAuthorization,
)
from infra.db.prospect_inventory_tables import (
    CandidateAllocationRow,
    CustomerPreparationItemRow,
    InventoryFollowupRequestRow,
)
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
            preparation = None
            if row is not None:
                item = (await session.execute(
                    select(CustomerPreparationItemRow, InventoryFollowupRequestRow)
                    .join(InventoryFollowupRequestRow,
                        (InventoryFollowupRequestRow.tenant_id == CustomerPreparationItemRow.tenant_id) &
                        (InventoryFollowupRequestRow.key_hash == CustomerPreparationItemRow.key_hash))
                    .where(CustomerPreparationItemRow.tenant_id == tenant_id,
                           InventoryFollowupRequestRow.tenant_id == tenant_id,
                           CustomerPreparationItemRow.allocation_id == allocation_id)
                )).one_or_none()
                if item is not None:
                    task, request = item
                    authorization = InventoryFollowupAuthorization.model_validate(request.authorization)
                    preparation = CustomerPreparationStatus.model_validate({
                        "state": "blocked" if request.revoked else task.state, "stage": task.stage,
                        "blocked_reason": "authorization_revoked" if request.revoked else (task.blocked_reason if task.blocked_reason in PREPARATION_BLOCK_REASONS else None),
                        "campaign_id": authorization.scope.campaign_id,
                        "campaign_version": authorization.scope.campaign_version,
                    })
            yield CustomerContext(
                tenant_id,
                employee_id,
                UserId(employee.user_id) if employee.user_id else None,
                employee.role,
                employee.is_active,
                allocation,
                owner,
                preparation,
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
