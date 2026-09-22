"""EmployeeService 实现（八级分配解析 + 归属锁 + 转移历史 + 服务层授权）。

依赖注入：
- ``employees``/``territories``/``ownership``：三个存储 Protocol。
- ``now``：时钟（默认注入，测试可冻结）。
- ``manager_pool``：经理池——注入的 ``EmployeeId`` 序列；第 8 级**只选其中
  经员工仓储验证 active 且 MANAGER/BOSS 者**（非法/停用成员跳过），绝不随机。
- ``count_active_accounts``：第 6 级工作量数据源（活跃客户数）。
- ``authorizer``：``EmployeeAuthorizer``，所有公开读写先判权（默认拒绝）；
  判权通过或拒绝都写授权审计（拒绝时 ``rule="deny"``）。
- ``audit``：``AuditLogger``，仅 actor/action/tenant/scope/rule，无敏感值。

硬边界：只依赖 ``shared.*`` 与本域内部模块；不 import 其他 domains/*。
所有返回类型都是 ``schemas.py`` 公共 View，不暴露内部 models。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from domains.employees.errors import (
    EmployeeNotFoundError,
    NoAssignmentRuleError,
    NoOwnershipLockError,
)
from domains.employees.models import (
    Employee,
    OwnershipLock,
    OwnershipTransfer,
    Role,
    TerritoryAssignment,
)
from domains.employees.permissions import (
    Actor,
    AuditLogger,
    EmployeeAction,
    EmployeeAuthorizer,
)
from domains.employees.repository import (
    EmployeeRepository,
    OwnershipRepository,
    TerritoryRepository,
)
from domains.employees.schemas import (
    EmployeeView,
    OwnershipLockView,
    TerritoryAssignmentView,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TenantId,
    new_id,
)


class EmployeeServiceImpl:
    """员工服务实现：八级分配（首命中即停）+ 归属锁原子化 + 转移只增历史。"""

    def __init__(
        self,
        *,
        employees: EmployeeRepository,
        territories: TerritoryRepository,
        ownership: OwnershipRepository,
        now: Callable[[], datetime],
        manager_pool: Callable[[TenantId], Sequence[EmployeeId]],
        count_active_accounts: Callable[[TenantId, EmployeeId], Awaitable[int]],
        authorizer: EmployeeAuthorizer,
        audit: AuditLogger,
    ) -> None:
        self._employees = employees
        self._territories = territories
        self._ownership = ownership
        self._now = now
        self._manager_pool = manager_pool
        self._count_active_accounts = count_active_accounts
        self._authorizer = authorizer
        self._audit = audit

    # --- 授权与审计 -----------------------------------------------------------

    def _authorize(
        self, actor: Actor, action: EmployeeAction, tenant_id: TenantId
    ) -> str:
        """判权并写授权审计；返回判权所用规则标识。

        拒绝（``PermissionDenied``）时同样写审计，``rule="deny"``，
        不记录任何业务/异常内容。
        """
        try:
            rule = self._authorizer.require(actor, action, actor.scope, tenant_id)
        except PermissionDenied:
            self._audit.log(
                actor=actor.actor_id,
                action=action.value,
                tenant_id=tenant_id,
                scope=actor.scope.value,
                rule="deny",
            )
            raise
        self._audit.log(
            actor=actor.actor_id,
            action=action.value,
            tenant_id=tenant_id,
            scope=actor.scope.value,
            rule=rule,
        )
        return rule

    # --- 视图转换 -------------------------------------------------------------

    @staticmethod
    def _to_employee_view(emp: Employee) -> EmployeeView:
        return EmployeeView(
            employee_id=emp.employee_id,
            tenant_id=emp.tenant_id,
            name=emp.name,
            role=emp.role.value,
            user_id=emp.user_id,
            team_id=emp.team_id,
            manager_id=emp.manager_id,
            languages=list(emp.languages),
            timezone=emp.timezone,
            is_active=emp.is_active,
            max_active_accounts=emp.max_active_accounts,
        )

    @staticmethod
    def _to_lock_view(lock: OwnershipLock) -> OwnershipLockView:
        return OwnershipLockView(
            tenant_id=lock.tenant_id,
            account_id=lock.account_id,
            owner=lock.owner,
            locked_at=lock.locked_at,
            locked_by_rule=lock.locked_by_rule,
        )

    @staticmethod
    def _to_assignment_view(a: TerritoryAssignment) -> TerritoryAssignmentView:
        return TerritoryAssignmentView(
            tenant_id=a.tenant_id,
            employee_id=a.employee_id,
            priority=a.priority,
            effective_from=a.effective_from,
            countries=list(a.countries),
            product_categories=list(a.product_categories),
            need_categories=list(a.need_categories),
            buyer_types=list(a.buyer_types),
            languages=list(a.languages),
            manager_id=a.manager_id,
            backup_employee_id=a.backup_employee_id,
            effective_until=a.effective_until,
        )

    # --- resolve_owner --------------------------------------------------------

    async def resolve_owner(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        actor: Actor,
        country: str,
        need_category: str | None = None,
        buyer_type: str | None = None,
        language: str | None = None,
        timezone: str | None = None,
        boss_override: EmployeeId | None = None,
    ) -> OwnershipLockView:
        self._authorize(actor, EmployeeAction.OWNERSHIP_LOCK, tenant_id)
        # 已有锁幂等：不重复分配，直接返回既有归属。
        existing = await self._ownership.get(tenant_id, account_id)
        if existing is not None:
            return self._to_lock_view(existing)
        owner, level = await self._resolve(
            tenant_id,
            account_id,
            country,
            need_category,
            buyer_type,
            language,
            timezone,
            boss_override,
        )
        lock = OwnershipLock(
            tenant_id=tenant_id,
            account_id=account_id,
            owner=owner,
            locked_at=self._now(),
            locked_by_rule=level,
        )
        if await self._ownership.try_lock(lock):
            return self._to_lock_view(lock)
        # 并发被抢：返回胜者（幂等）。胜者必然存在（try_lock 失败说明已有锁）。
        winner = await self._ownership.get(tenant_id, account_id)
        if winner is None:
            raise NoOwnershipLockError("并发上锁失败且查不到既有归属")
        return self._to_lock_view(winner)

    async def _resolve(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        country: str,
        need_category: str | None,
        buyer_type: str | None,
        language: str | None,
        timezone: str | None,
        boss_override: EmployeeId | None,
    ) -> tuple[EmployeeId, str]:
        """八级字典序解析；首命中即停。全部不中抛 ``NoAssignmentRuleError``。"""
        # 1 老板手动指定
        if boss_override is not None:
            boss = await self._employees.get(tenant_id, boss_override)
            if boss is not None and boss.is_active:
                return boss_override, "1"
        # 2 已有客户历史负责人 = 按 transferred_at 升序历史最后一条的 to_owner
        history = sorted(
            await self._ownership.list_transfers(tenant_id, account_id),
            key=lambda t: t.transferred_at,
        )
        if history:
            last = history[-1]
            hist_emp = await self._employees.get(tenant_id, last.to_owner)
            if hist_emp is not None and hist_emp.is_active:
                return last.to_owner, "2"
        # 预取各维度命中规则（L3/L4 与 L5/L6/L7 的并集共用；不再用 AND 查询当 backup 集合）
        need_rules = (
            await self._territories.list_matching(
                tenant_id, country, need_category, None
            )
            if need_category is not None
            else None
        )
        buyer_rules = (
            await self._territories.list_matching(tenant_id, country, None, buyer_type)
            if buyer_type is not None
            else None
        )
        # 3 国家 + 需求类别
        if need_rules is not None:
            cand = await self._pick_from_rules(tenant_id, need_rules)
            if cand is not None:
                return cand, "3"
        # 4 国家 + 客户类型
        if buyer_rules is not None:
            cand = await self._pick_from_rules(tenant_id, buyer_rules)
            if cand is not None:
                return cand, "4"
        # 匹配规则并集（country+need 与 country+buyer 的确定性并集；维度都空时按 country-only）
        matched_union = await self._matched_union(
            tenant_id, country, need_category, buyer_type, need_rules, buyer_rules
        )
        backup_ids = {
            r.backup_employee_id
            for r in matched_union
            if r.backup_employee_id is not None
        }
        pool_ids = set(self._manager_pool(tenant_id))
        # 5 语言和时区：仅 active SALES 常规池（排除 backup 与池成员，避免提前抢占 L7/8）；
        #   有输入时不猜测，候选必须同时满足所有已提供条件
        if language is not None or timezone is not None:
            cand = await self._pick_by_profile(
                tenant_id, language, timezone, backup_ids, pool_ids
            )
            if cand is not None:
                return cand, "5"
        # 6 当前员工工作量：仅 active SALES 常规池，
        #   排除当前匹配规则的 backup 与注入经理池成员
        active_sales = [
            e.employee_id
            for e in await self._employees.list_active(tenant_id)
            if e.role == Role.SALES
            and e.employee_id not in backup_ids
            and e.employee_id not in pool_ids
        ]
        if active_sales:
            cand = await self._pick_least_loaded(tenant_id, active_sales)
            return cand, "6"
        # 7 备用员工（并集规则里取 active 的 backup）
        for r in matched_union:
            if r.backup_employee_id is not None:
                backup = await self._employees.get(tenant_id, r.backup_employee_id)
                if backup is not None and backup.is_active:
                    return r.backup_employee_id, "7"
        # 8 经理待分配池：仅注入池中经员工仓储验证 active 且 MANAGER/BOSS 者
        pool_candidates: list[EmployeeId] = []
        for eid in pool_ids:
            emp = await self._employees.get(tenant_id, eid)
            if (
                emp is not None
                and emp.is_active
                and emp.role in (Role.MANAGER, Role.BOSS)
            ):
                pool_candidates.append(eid)
        if pool_candidates:
            cand = await self._pick_least_loaded(tenant_id, pool_candidates)
            return cand, "8"
        raise NoAssignmentRuleError("没有命中任何分配规则，进入经理待分配池")

    async def _matched_union(
        self,
        tenant_id: TenantId,
        country: str,
        need_category: str | None,
        buyer_type: str | None,
        need_rules: list[TerritoryAssignment] | None,
        buyer_rules: list[TerritoryAssignment] | None,
    ) -> list[TerritoryAssignment]:
        """country+need 与 country+buyer 命中规则的**确定性并集**（去重 + 稳定排序）。

        need 与 buyer 都为空时按 country-only。规则对象无 ID，用全字段
        可哈希 key 去重——不得错误合并不同 backup 的规则。
        """
        if need_category is None and buyer_type is None:
            base = await self._territories.list_matching(tenant_id, country, None, None)
        else:
            base = list(need_rules or []) + list(buyer_rules or [])
        seen: set[tuple] = set()
        out: list[TerritoryAssignment] = []
        for r in base:
            key = self._rule_key(r)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return sorted(out, key=lambda r: (r.priority, str(r.employee_id)))

    @staticmethod
    def _rule_key(r: TerritoryAssignment) -> tuple:
        """规则去重 key（全字段可哈希稳定；规则无 ID 时的唯一标识）。"""
        return (
            r.tenant_id,
            r.employee_id,
            r.priority,
            r.effective_from,
            tuple(r.countries),
            tuple(r.product_categories),
            tuple(r.need_categories),
            tuple(r.buyer_types),
            tuple(r.languages),
            r.manager_id,
            r.backup_employee_id,
            r.effective_until,
        )

    async def _pick_from_rules(
        self, tenant_id: TenantId, rules: list[TerritoryAssignment]
    ) -> EmployeeId | None:
        """取规则里最高优先级且 active 的规则员工。"""
        for r in sorted(rules, key=lambda r: (r.priority, str(r.employee_id))):
            emp = await self._employees.get(tenant_id, r.employee_id)
            if emp is not None and emp.is_active:
                return r.employee_id
        return None

    async def _pick_by_profile(
        self,
        tenant_id: TenantId,
        language: str | None,
        timezone: str | None,
        backup_ids: set[EmployeeId],
        pool_ids: set[EmployeeId],
    ) -> EmployeeId | None:
        """仅从 active SALES 常规池选同时满足语言/时区的候选（排除 backup 与池成员）。"""
        lang = language.lower() if language is not None else None
        matched: list[EmployeeId] = []
        for e in await self._employees.list_active(tenant_id):
            if e.role != Role.SALES:
                continue
            if e.employee_id in backup_ids or e.employee_id in pool_ids:
                continue
            if lang is not None and lang not in {x.lower() for x in e.languages}:
                continue
            if timezone is not None and e.timezone != timezone:
                continue
            matched.append(e.employee_id)
        if not matched:
            return None
        return await self._pick_least_loaded(tenant_id, matched)

    async def _pick_least_loaded(
        self, tenant_id: TenantId, candidate_ids: Sequence[EmployeeId]
    ) -> EmployeeId:
        """最少活跃客户数；同数按 employee_id 字典序（稳定、不随机）。"""
        scored = [
            (await self._count_active_accounts(tenant_id, eid), eid)
            for eid in candidate_ids
        ]
        scored.sort(key=lambda item: (item[0], str(item[1])))
        return scored[0][1]

    # --- transfer / get_ownership ----------------------------------------------

    async def transfer(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        new_owner: EmployeeId,
        *,
        actor: Actor,
        transferred_by: EmployeeId,
        reason: str,
    ) -> None:
        self._authorize(actor, EmployeeAction.OWNERSHIP_TRANSFER, tenant_id)
        existing = await self._ownership.get(tenant_id, account_id)
        if existing is None:
            raise NoOwnershipLockError("账户没有归属锁，无法转移；请先 resolve_owner")
        reason = reason.strip()  # strip 后校验并持久化
        if not reason:
            raise ValidationError("转移原因不能为空")
        new_owner_emp = await self._employees.get(tenant_id, new_owner)
        if new_owner_emp is None or not new_owner_emp.is_active:
            raise ValidationError("新负责人不存在、不属于该租户或已停用")
        by_emp = await self._employees.get(tenant_id, transferred_by)
        if by_emp is None or not by_emp.is_active:
            raise ValidationError("转移执行人不存在、不属于该租户或已停用")
        now = self._now()
        new_lock = OwnershipLock(
            tenant_id=tenant_id,
            account_id=account_id,
            owner=new_owner,
            locked_at=now,
            locked_by_rule="transfer",
        )
        transfer = OwnershipTransfer(
            transfer_id=new_id("otr"),
            tenant_id=tenant_id,
            account_id=account_id,
            from_owner=existing.owner,
            to_owner=new_owner,
            transferred_by=transferred_by,
            transferred_at=now,
            reason=reason,
        )
        # 换锁 + 追加历史由仓储一次原子完成；失败不产生部分提交。
        await self._ownership.replace(tenant_id, new_lock, transfer)

    async def get_notification_owner(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        *,
        actor: Actor,
    ) -> EmployeeId | None:
        """授权且核验精确账户后读取当前归属，无历史负责人回退。"""
        self._authorize(actor, EmployeeAction.NOTIFICATION_OWNER_READ, tenant_id)
        if actor.notification_account_id != account_id:
            raise PermissionDenied("通知账户关联拒绝")
        lock = await self._ownership.get(tenant_id, account_id)
        if lock is None:
            return None
        if lock.tenant_id != tenant_id or lock.account_id != account_id:
            raise PermissionDenied("通知账户关联拒绝")
        return lock.owner

    async def get_ownership(
        self, tenant_id: TenantId, account_id: ProspectAccountId, *, actor: Actor
    ) -> OwnershipLockView | None:
        self._authorize(actor, EmployeeAction.OWNERSHIP_READ, tenant_id)
        lock = await self._ownership.get(tenant_id, account_id)
        return self._to_lock_view(lock) if lock is not None else None

    # --- directive ---------------------------------------------------------------

    async def apply_territory_from_directive(
        self, tenant_id: TenantId, assignments: dict[str, str], *, actor: Actor
    ) -> None:
        self._authorize(actor, EmployeeAction.TERRITORY_APPLY, tenant_id)
        now = self._now()
        for country, target in assignments.items():
            employee_id = await self._resolve_directive_target(tenant_id, target)
            await self._territories.add(
                TerritoryAssignment(
                    tenant_id=tenant_id,
                    employee_id=employee_id,
                    priority=1,
                    effective_from=now,
                    countries=[country],
                )
            )

    async def _resolve_directive_target(
        self, tenant_id: TenantId, target: str
    ) -> EmployeeId:
        """指令目标：先按 employee_id 精确匹配，再按 name 匹配活跃员工。"""
        emp = await self._employees.get(tenant_id, EmployeeId(target))
        if emp is not None and emp.is_active:
            return emp.employee_id
        for e in await self._employees.list_active(tenant_id):
            if e.name == target:
                return e.employee_id
        raise ValidationError(f"指令目标不可解析: {target}")

    # --- 员工 / 规则读取 -----------------------------------------------------------

    async def get_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: Actor
    ) -> EmployeeView:
        self._authorize(actor, EmployeeAction.EMPLOYEE_READ, tenant_id)
        emp = await self._employees.get(tenant_id, employee_id)
        if emp is None:
            raise EmployeeNotFoundError(f"员工不存在或不属于该租户: {employee_id}")
        return self._to_employee_view(emp)

    async def list_active(
        self, tenant_id: TenantId, *, actor: Actor
    ) -> list[EmployeeView]:
        self._authorize(actor, EmployeeAction.EMPLOYEE_LIST, tenant_id)
        emps = await self._employees.list_active(tenant_id)
        return [self._to_employee_view(e) for e in emps]

    async def list_assignments(
        self, tenant_id: TenantId, employee_id: EmployeeId, *, actor: Actor
    ) -> list[TerritoryAssignmentView]:
        self._authorize(actor, EmployeeAction.ASSIGNMENT_LIST, tenant_id)
        rules = await self._territories.list_by_employee(tenant_id, employee_id)
        return [self._to_assignment_view(r) for r in rules]

    async def list_territory_matrix(
        self, tenant_id: TenantId, *, actor: Actor
    ) -> list[TerritoryAssignmentView]:
        self._authorize(actor, EmployeeAction.ASSIGNMENT_LIST, tenant_id)
        active = sorted(
            await self._employees.list_active(tenant_id),
            key=lambda employee: str(employee.employee_id),
        )
        rules = [
            rule
            for employee in active
            for rule in await self._territories.list_by_employee(
                tenant_id, employee.employee_id
            )
        ]
        return [
            self._to_assignment_view(rule)
            for rule in sorted(
                rules,
                key=lambda item: (
                    item.priority,
                    str(item.employee_id),
                    item.effective_from,
                ),
            )
        ]
