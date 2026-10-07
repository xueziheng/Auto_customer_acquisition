"""Employee / Territory / Ownership 仓储实现（infra 层）。

域模型 ↔ ORM 行转换在本文件完成；租户过滤由 ``TenantScopedRepository`` 基类注入
（构造时绑定租户，仓储永不查询绑定租户之外的行，硬边界 8）。
``count_active_accounts`` 统计当前租户 owner 对应**非终态**机会的 distinct
``account_id``（排除 won/lost），用 SQL ``COUNT(DISTINCT ...)``，无 float。
``replace`` 不 commit：更新当前锁 + 追加只增历史在同一 session 事务中，
由调用方提交/回滚——失败绝不部分提交。锁/规则的主键 ID 由本层生成
（域模型不持有 lock_id/assignment_id）。
"""
from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, Select, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from domains.employees import models
from domains.employees.errors import OwnershipConflictError
from infra.db.base import TenantScopedRepository
from infra.db.tables import (
    EmployeeRow,
    OpportunityRow,
    OwnershipLockRow,
    OwnershipTransferHistoryRow,
    TerritoryAssignmentRow,
)
from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TeamId,
    TenantId,
    UserId,
    new_id,
)


def _emp_to_row(emp: models.Employee) -> EmployeeRow:
    return EmployeeRow(
        employee_id=emp.employee_id,
        tenant_id=emp.tenant_id,
        name=emp.name,
        role=emp.role.value,
        created_at=emp.created_at,
        user_id=emp.user_id,
        team_id=emp.team_id,
        manager_id=emp.manager_id,
        languages=list(emp.languages),
        timezone=emp.timezone,
        is_active=emp.is_active,
        max_active_accounts=emp.max_active_accounts,
    )


def _row_to_emp(row: EmployeeRow) -> models.Employee:
    return models.Employee(
        employee_id=EmployeeId(row.employee_id),
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        role=models.Role(row.role),
        created_at=row.created_at,
        user_id=UserId(row.user_id) if row.user_id is not None else None,
        team_id=TeamId(row.team_id) if row.team_id is not None else None,
        manager_id=EmployeeId(row.manager_id) if row.manager_id is not None else None,
        languages=list(row.languages or []),
        timezone=row.timezone,
        is_active=row.is_active,
        max_active_accounts=row.max_active_accounts,
    )


def _territory_to_row(rule: models.TerritoryAssignment) -> TerritoryAssignmentRow:
    return TerritoryAssignmentRow(
        assignment_id=new_id("ter"),
        tenant_id=rule.tenant_id,
        employee_id=rule.employee_id,
        priority=rule.priority,
        effective_from=rule.effective_from,
        countries=list(rule.countries),
        product_categories=list(rule.product_categories),
        need_categories=list(rule.need_categories),
        buyer_types=list(rule.buyer_types),
        languages=list(rule.languages),
        manager_id=rule.manager_id,
        backup_employee_id=rule.backup_employee_id,
        effective_until=rule.effective_until,
    )


def _row_to_territory(row: TerritoryAssignmentRow) -> models.TerritoryAssignment:
    return models.TerritoryAssignment(
        tenant_id=TenantId(row.tenant_id),
        employee_id=EmployeeId(row.employee_id),
        priority=row.priority,
        effective_from=row.effective_from,
        countries=list(row.countries or []),
        product_categories=list(row.product_categories or []),
        need_categories=list(row.need_categories or []),
        buyer_types=list(row.buyer_types or []),
        languages=list(row.languages or []),
        manager_id=EmployeeId(row.manager_id) if row.manager_id is not None else None,
        backup_employee_id=EmployeeId(row.backup_employee_id) if row.backup_employee_id is not None else None,
        effective_until=row.effective_until,
    )


def _row_to_lock(row: OwnershipLockRow) -> models.OwnershipLock:
    return models.OwnershipLock(
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        owner=EmployeeId(row.owner),
        locked_at=row.locked_at,
        locked_by_rule=row.locked_by_rule,
    )


def _transfer_to_row(transfer: models.OwnershipTransfer) -> OwnershipTransferHistoryRow:
    return OwnershipTransferHistoryRow(
        transfer_id=transfer.transfer_id,
        tenant_id=transfer.tenant_id,
        account_id=transfer.account_id,
        from_owner=transfer.from_owner,
        to_owner=transfer.to_owner,
        transferred_by=transfer.transferred_by,
        transferred_at=transfer.transferred_at,
        reason=transfer.reason,
    )


def _row_to_transfer(row: OwnershipTransferHistoryRow) -> models.OwnershipTransfer:
    return models.OwnershipTransfer(
        transfer_id=row.transfer_id,
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        from_owner=EmployeeId(row.from_owner) if row.from_owner is not None else None,
        to_owner=EmployeeId(row.to_owner),
        transferred_by=EmployeeId(row.transferred_by),
        transferred_at=row.transferred_at,
        reason=row.reason,
    )


class EmployeeRepositoryImpl(TenantScopedRepository):
    """``EmployeeRepository`` 的 SQLAlchemy 实现。"""

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _scoped(self) -> Select[tuple[EmployeeRow]]:
        return self.scoped_query(EmployeeRow)

    async def add(self, employee: models.Employee) -> None:
        if employee.tenant_id != self._tenant_id:
            raise ValueError(
                f"员工租户 {employee.tenant_id} 与仓储绑定租户 {self._tenant_id} "
                "不一致：拒绝写入（硬边界 8）"
            )
        self._session.add(_emp_to_row(employee))

    async def get(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> models.Employee | None:
        if tenant_id != self._tenant_id:
            return None
        row = (
            await self._session.execute(
                self._scoped().where(EmployeeRow.employee_id == employee_id)
            )
        ).scalar_one_or_none()
        return _row_to_emp(row) if row is not None else None

    async def update(self, employee: models.Employee) -> None:
        if employee.tenant_id != self._tenant_id:
            raise ValueError(
                f"员工租户 {employee.tenant_id} 与仓储绑定租户 {self._tenant_id} "
                "不一致：拒绝写入（硬边界 8）"
            )
        row = _emp_to_row(employee)
        values = {
            col.name: getattr(row, col.name)
            for col in EmployeeRow.__table__.columns
            if col.name not in ("employee_id", "tenant_id")
        }
        await self._session.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == self._tenant_id,
                EmployeeRow.employee_id == employee.employee_id,
            )
            .values(**values)
        )

    async def list_active(self, tenant_id: TenantId) -> list[models.Employee]:
        if tenant_id != self._tenant_id:
            return []
        rows = (
            await self._session.execute(
                self._scoped().where(EmployeeRow.is_active.is_(True))
            )
        ).scalars().all()
        return [_row_to_emp(row) for row in rows]

    async def count_active_accounts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> int:
        """当前租户 owner 对应非终态机会的 distinct account_id（排除 won/lost）。"""
        if tenant_id != self._tenant_id:
            return 0
        result = await self._session.execute(
            select(func.count(func.distinct(OpportunityRow.account_id)))
            .where(
                OpportunityRow.tenant_id == self._tenant_id,
                OpportunityRow.owner == employee_id,
                OpportunityRow.state.not_in(["won", "lost"]),
            )
        )
        return cast(int, result.scalar_one())


class TerritoryRepositoryImpl(TenantScopedRepository):
    """``TerritoryRepository`` 的 SQLAlchemy 实现。

    ``list_matching`` 用 Postgres ARRAY ``@>`` 确定性查询维度；只返回有效窗口内
    规则（``effective_from <= now()`` 且 ``effective_until IS NULL 或 > now()``）；
    ``priority`` 升序、同优先按 ``employee_id`` 稳定 tie-break。
    """

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _scoped(self) -> Select[tuple[TerritoryAssignmentRow]]:
        return self.scoped_query(TerritoryAssignmentRow)

    async def add(self, assignment: models.TerritoryAssignment) -> None:
        if assignment.tenant_id != self._tenant_id:
            raise ValueError(
                f"规则租户 {assignment.tenant_id} 与仓储绑定租户 {self._tenant_id} "
                "不一致：拒绝写入（硬边界 8）"
            )
        self._session.add(_territory_to_row(assignment))

    async def list_matching(
        self,
        tenant_id: TenantId,
        country: str,
        need_category: str | None,
        buyer_type: str | None,
    ) -> list[models.TerritoryAssignment]:
        if tenant_id != self._tenant_id:
            return []
        query = self._scoped()
        if need_category is not None:
            query = query.where(TerritoryAssignmentRow.need_categories.contains([need_category]))
        if buyer_type is not None:
            query = query.where(TerritoryAssignmentRow.buyer_types.contains([buyer_type]))
        query = query.where(
            TerritoryAssignmentRow.countries.contains([country]),
            TerritoryAssignmentRow.effective_from <= func.now(),
            or_(
                TerritoryAssignmentRow.effective_until.is_(None),
                TerritoryAssignmentRow.effective_until > func.now(),
            ),
        )
        query = query.order_by(
            TerritoryAssignmentRow.priority.asc(),
            TerritoryAssignmentRow.employee_id.asc(),
        )
        rows = (await self._session.execute(query)).scalars().all()
        return [_row_to_territory(row) for row in rows]

    async def list_by_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> list[models.TerritoryAssignment]:
        if tenant_id != self._tenant_id:
            return []
        rows = (
            await self._session.execute(
                self._scoped().where(TerritoryAssignmentRow.employee_id == employee_id)
            )
        ).scalars().all()
        return [_row_to_territory(row) for row in rows]


class OwnershipRepositoryImpl(TenantScopedRepository):
    """``OwnershipRepository`` 的 SQLAlchemy 实现。

    ``try_lock`` 用 ``INSERT ... ON CONFLICT (tenant_id, account_id) DO NOTHING``：
    仅 ``UNIQUE(tenant_id, account_id)`` 冲突（rowcount==0）返回 False；owner 复合
    FK 等其余 IntegrityError 一律传播，不伪装成锁冲突。
    ``replace`` 同一 session 事务更新当前锁 + 追加只增历史，**不 commit**——
    由调用方事务管理，失败绝不部分提交。历史真实性由 UPDATE 的
    ``WHERE owner == transfer.from_owner`` + rowcount==1 强制（stale/并发无法伪造）。
    """

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(tenant_id)
        self._session = session

    def _scoped(self) -> Select[tuple[OwnershipLockRow]]:
        return self.scoped_query(OwnershipLockRow)

    async def try_lock(self, lock: models.OwnershipLock) -> bool:
        """ON CONFLICT (tenant_id, account_id) DO NOTHING；rowcount>0 才算抢到。

        只有 UNIQUE(tenant_id, account_id) 冲突返回 False；owner 复合 FK 等其他
        IntegrityError **一律传播**——不得把非法 owner 伪装成「锁冲突 False」。
        """
        if lock.tenant_id != self._tenant_id:
            return False
        result = await self._session.execute(
            insert(OwnershipLockRow)
            .values(
                lock_id=new_id("loc"),
                tenant_id=lock.tenant_id,
                account_id=lock.account_id,
                owner=lock.owner,
                locked_at=lock.locked_at,
                locked_by_rule=lock.locked_by_rule,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "account_id"])
        )
        return cast(CursorResult, result).rowcount > 0

    async def get(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> models.OwnershipLock | None:
        if tenant_id != self._tenant_id:
            return None
        row = (
            await self._session.execute(
                self._scoped().where(OwnershipLockRow.account_id == account_id)
            )
        ).scalar_one_or_none()
        return _row_to_lock(row) if row is not None else None

    async def replace(
        self,
        tenant_id: TenantId,
        new_lock: models.OwnershipLock,
        transfer: models.OwnershipTransfer,
    ) -> None:
        """原子替换归属并追加只增历史；由调用方事务提交/回滚。

        fail closed：``new_lock.tenant_id == transfer.tenant_id == 参数 == 绑定租户``，
        且 ``new_lock.account_id == transfer.account_id``；任一不符在触碰数据库前
        抛 ``ValueError``，不产生任何副作用。
        """
        if tenant_id != self._tenant_id:
            raise ValueError(
                f"转移租户 {tenant_id} 与仓储绑定租户 {self._tenant_id} "
                "不一致：拒绝写入（硬边界 8）"
            )
        if new_lock.tenant_id != self._tenant_id or transfer.tenant_id != self._tenant_id:
            raise ValueError(
                f"换锁/转移历史租户与仓储绑定租户 {self._tenant_id} 不一致："
                "拒绝写入（硬边界 8）"
            )
        if new_lock.account_id != transfer.account_id:
            raise ValueError("换锁与转移历史的 account_id 不一致：拒绝写入")
        if new_lock.owner != transfer.to_owner:
            raise ValueError("换锁 owner 与转移历史 to_owner 不一致：拒绝写入")
        # 只有 WHERE 命中「当前 owner == transfer.from_owner」且恰好更新 1 行
        # （rowcount==1）才视为有效转移：stale/concurrent 无法伪造 from_owner。
        result = await self._session.execute(
            update(OwnershipLockRow)
            .where(
                OwnershipLockRow.tenant_id == self._tenant_id,
                OwnershipLockRow.account_id == new_lock.account_id,
                OwnershipLockRow.owner == transfer.from_owner,
            )
            .values(
                owner=new_lock.owner,
                locked_at=new_lock.locked_at,
                locked_by_rule=new_lock.locked_by_rule,
            )
        )
        if cast(CursorResult, result).rowcount != 1:
            raise OwnershipConflictError("当前归属已被他人变更，拒绝过期/伪造的转移")
        self._session.add(_transfer_to_row(transfer))

    async def list_transfers(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[models.OwnershipTransfer]:
        if tenant_id != self._tenant_id:
            return []
        rows = (
            await self._session.execute(
                select(OwnershipTransferHistoryRow)
                .where(
                    OwnershipTransferHistoryRow.tenant_id == self._tenant_id,
                    OwnershipTransferHistoryRow.account_id == account_id,
                )
                .order_by(OwnershipTransferHistoryRow.transferred_at.asc())
            )
        ).scalars().all()
        return [_row_to_transfer(row) for row in rows]
