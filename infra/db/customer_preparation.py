"""客户资料持久化机械适配；人工与自动编排均借用规范域服务。"""
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.demand.service import DemandService
from domains.prospecting.inventory_followup import PreparationClaim
from domains.prospecting.service import ContactValueHasher, ProspectingService
from shared.errors import ValidationError
from shared.schemas.identifiers import ProspectAccountId, TenantId


class _PreparationAccountNames:
    def __init__(self, prospecting: ProspectingService) -> None:
        self._prospecting = prospecting

    async def names_for(
        self, tenant_id: TenantId, account_ids: tuple[ProspectAccountId, ...]
    ) -> dict[ProspectAccountId, str]:
        return {
            a: (await self._prospecting.get_account(tenant_id, a)).name
            for a in account_ids
        }

    async def countries_for(
        self, tenant_id: TenantId, account_ids: tuple[ProspectAccountId, ...]
    ) -> dict[ProspectAccountId, str]:
        return {
            a: (await self._prospecting.get_account(tenant_id, a)).country
            for a in account_ids
        }

    async def domains_for(
        self, tenant_id: TenantId, account_ids: tuple[ProspectAccountId, ...]
    ) -> dict[ProspectAccountId, str]:
        result: dict[ProspectAccountId, str] = {}
        for account_id in account_ids:
            domain = (
                await self._prospecting.get_account(tenant_id, account_id)
            ).website_domain
            if domain is None:
                raise ValidationError("客户官网域名尚未确认")
            result[account_id] = domain
        return result


class PostgresCustomerPreparationTransactions:
    """组合根适配器；只借用同一会话给现有域服务，副作用串行执行。"""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        hasher: ContactValueHasher,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory, self._hasher = factory, hasher
        self._now = now or (lambda: datetime.now(UTC))

    @asynccontextmanager
    async def guard(
        self, tenant_id: TenantId
    ) -> AsyncIterator[tuple[ProspectingService, DemandService]]:
        from domains.demand.service_impl import DemandServiceImpl
        from domains.prospecting.service_impl import ProspectingServiceImpl
        from infra.db.demand_uow import BorrowedDemandUnitOfWork
        from infra.db.prospecting_uow import BorrowedProspectingUnitOfWork

        async with self._factory() as session, session.begin():
            prospecting = ProspectingServiceImpl(
                lambda t: BorrowedProspectingUnitOfWork(session, t),
                self._hasher,
                now=self._now,
            )
            demand = DemandServiceImpl(
                lambda t: BorrowedDemandUnitOfWork(session, t, now=self._now),
                now=self._now,
                account_names=_PreparationAccountNames(prospecting),
            )
            yield prospecting, cast(DemandService, demand)




class PostgresAgentPreparationTransactions:
    """证据/联系人与任务结果同事务；持当前身份和原归属共享锁直到提交。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession], hasher: ContactValueHasher,
                 *, now: Callable[[], datetime] | None = None) -> None:
        self._factory, self._hasher = factory, hasher
        self._now = now or (lambda: datetime.now(UTC))

    async def apply(self, claim: PreparationClaim, action: Callable[[ProspectingService, DemandService], Awaitable[dict[str, str]]]) -> PreparationClaim:
        from sqlalchemy import select

        from domains.demand.service_impl import DemandServiceImpl
        from domains.prospecting.service_impl import ProspectingServiceImpl
        from infra.db.customer_preparation_state import checkpoint, lock_claim
        from infra.db.demand_uow import BorrowedDemandUnitOfWork
        from infra.db.prospect_inventory_tables import CandidateAllocationRow
        from infra.db.prospecting_uow import BorrowedProspectingUnitOfWork
        from infra.db.tables import EmployeeRow, OwnershipLockRow
        from shared.errors import PermissionDenied

        tenant, auth = claim.tenant_id, claim.authorization
        async with self._factory() as session, session.begin():
            row = await lock_claim(session, tenant, claim, self._now())
            employees = (await session.scalars(select(EmployeeRow).where(
                EmployeeRow.tenant_id == tenant,
                EmployeeRow.employee_id.in_((auth.actor_employee_id, auth.owner_employee_id)),
            ).order_by(EmployeeRow.employee_id).with_for_update(read=True))).all()
            people = {p.employee_id: p for p in employees}
            actor, owner = people.get(auth.actor_employee_id), people.get(auth.owner_employee_id)
            allocation = (await session.scalars(select(CandidateAllocationRow).where(
                CandidateAllocationRow.tenant_id == tenant, CandidateAllocationRow.allocation_id == claim.allocation_id,
            ).with_for_update(read=True))).one_or_none()
            ownership = (await session.scalars(select(OwnershipLockRow).where(
                OwnershipLockRow.tenant_id == tenant,
                OwnershipLockRow.account_id == claim.result_refs.get("account_id"),
            ).with_for_update(read=True))).one_or_none()
            if (actor is None or not actor.is_active or actor.role != "boss" or actor.user_id != auth.actor_user_id
                or owner is None or not owner.is_active or owner.role not in {"boss", "manager", "sales"}
                or allocation is None or allocation.account_id != claim.result_refs.get("account_id")
                or allocation.employee_id != auth.owner_employee_id or ownership is None
                or ownership.owner != auth.owner_employee_id or ownership.locked_at != claim.ownership_locked_at):
                raise PermissionDenied("准备资料的当前归属或身份已改变")
            prospecting = ProspectingServiceImpl(lambda t: BorrowedProspectingUnitOfWork(session, t), self._hasher, now=self._now)
            demand = DemandServiceImpl(lambda t: BorrowedDemandUnitOfWork(session, t, now=self._now), now=self._now,
                                       account_names=_PreparationAccountNames(prospecting))
            refs = await action(prospecting, cast(DemandService, demand))
            return checkpoint(row, claim, stage="verify", now=self._now(), result_refs=refs)
