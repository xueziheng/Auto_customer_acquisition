"""S3-3 员工域服务纯单元测试（行为断言，不断言实现细节）。

覆盖计划 S3-3 Step 1 全部行为 + 监督合同裁决：
- resolve_owner 八级字典序：首命中即停、locked_by_rule 记录级别、已有锁幂等返回
- 第 2 级历史负责人 = transferred_at 升序历史最后一条的 to_owner（最近实际负责人）
- 第 5 级语言与时区：有输入时不猜测，候选必须同时满足所有已提供条件
- 第 6 级仅 active SALES 常规池（排除匹配规则 backup 与注入经理池），
  最少负载 + 稳定 employee_id tie-break
- 第 7 级匹配规则里 active backup 可达
- 第 8 级经理池兜底：仅经员工仓储验证 active 且 MANAGER/BOSS，绝不随机；
  非法/停用池成员跳过
- transfer：reason 去空白非空、new_owner/transferred_by 同租户且 active；
  换锁 + 历史一次原子 replace，失败不出现「只换锁无历史」
- apply_territory_from_directive 只改未来规则、不回溯已有锁
- 公共 View 供 apps 消费（employees.schemas），不暴露 models
- 所有公开读写均经 EmployeeAuthorizer 判权；未知 action 默认拒绝
- 授权审计仅 actor/action/tenant/scope/rule，无敏感值；拒绝也用同一白名单 rule=deny

RED：``domains/employees/service_impl.py`` / ``permissions.py`` 尚未创建 → 收集错误。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from domains.employees import models
from domains.employees.errors import NoAssignmentRuleError, NoOwnershipLockError

Employee = models.Employee
OwnershipLock = models.OwnershipLock
OwnershipTransfer = models.OwnershipTransfer
Role = models.Role
TerritoryAssignment = models.TerritoryAssignment
from domains.employees.permissions import (
    Actor,
    DefaultDenyAuthorizer,
    EmployeeAction,
    EmployeeScope,
)
from domains.employees.schemas import (
    EmployeeView,
    OwnershipLockView,
    TerritoryAssignmentView,
)
from domains.employees.service_impl import EmployeeServiceImpl
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TenantId,
)

TENANT = TenantId("tenant-1")
ACCOUNT = ProspectAccountId("acct-1")
AT = datetime(2026, 8, 1, tzinfo=UTC)
ACTOR = Actor(actor_id="actor-boss", role="boss", scope=EmployeeScope.TENANT)


def _emp(
    eid: str,
    *,
    role: Role = Role.SALES,
    active: bool = True,
    languages: list[str] | None = None,
    timezone: str | None = None,
) -> Employee:
    return Employee(
        employee_id=EmployeeId(eid),
        tenant_id=TENANT,
        name=f"{eid}-name",
        role=role,
        created_at=AT,
        is_active=active,
        languages=languages or [],
        timezone=timezone,
    )


def _rule(
    employee: str,
    *,
    priority: int = 1,
    need: str | None = None,
    buyer: str | None = None,
    backup: str | None = None,
) -> TerritoryAssignment:
    return TerritoryAssignment(
        tenant_id=TENANT,
        employee_id=EmployeeId(employee),
        priority=priority,
        effective_from=AT,
        countries=["US"],
        need_categories=[need] if need else [],
        buyer_types=[buyer] if buyer else [],
        backup_employee_id=EmployeeId(backup) if backup else None,
    )


def _lock(owner: str, *, account: str | None = None, rule: str = "3") -> OwnershipLock:
    return OwnershipLock(
        tenant_id=TENANT,
        account_id=ProspectAccountId(account or ACCOUNT),
        owner=EmployeeId(owner),
        locked_at=AT,
        locked_by_rule=rule,
    )


def _transfer(
    tid: str,
    *,
    from_owner: str | None,
    to_owner: str,
    at: datetime,
    account: str | None = None,
) -> OwnershipTransfer:
    return OwnershipTransfer(
        transfer_id=tid,
        tenant_id=TENANT,
        account_id=ProspectAccountId(account or ACCOUNT),
        from_owner=EmployeeId(from_owner) if from_owner else None,
        to_owner=EmployeeId(to_owner),
        transferred_by=EmployeeId(to_owner),
        transferred_at=at,
        reason="handoff",
    )


# --- 测试替身 ------------------------------------------------------------------


class _FakeEmployeeRepository:
    """实现 ``EmployeeRepository`` 全量（含 ``count_active_accounts``），
    并作为服务「注入活跃计数」的同一数据源复用。"""

    def __init__(
        self,
        employees: list[Employee] | None = None,
        counts: dict[str, int] | None = None,
    ) -> None:
        self._by_id = {e.employee_id: e for e in (employees or [])}
        self._counts = dict(counts or {})

    async def get(self, tenant_id: TenantId, employee_id: EmployeeId) -> Employee | None:
        e = self._by_id.get(employee_id)
        return e if e is not None and e.tenant_id == tenant_id else None

    async def add(self, employee: Employee) -> None:
        self._by_id[employee.employee_id] = employee

    async def update(self, employee: Employee) -> None:
        self._by_id[employee.employee_id] = employee

    async def list_active(self, tenant_id: TenantId) -> list[Employee]:
        return [e for e in self._by_id.values() if e.tenant_id == tenant_id and e.is_active]

    async def count_active_accounts(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> int:
        return self._counts.get(employee_id, 0)


class _FakeTerritoryRepository:
    def __init__(self, rules: list[TerritoryAssignment] | None = None) -> None:
        self._rules = list(rules or [])
        self.added: list[TerritoryAssignment] = []

    async def add(self, assignment: TerritoryAssignment) -> None:
        self._rules.append(assignment)
        self.added.append(assignment)

    async def list_matching(
        self,
        tenant_id: TenantId,
        country: str,
        need_category: str | None,
        buyer_type: str | None,
    ) -> list[TerritoryAssignment]:
        matched = []
        for r in self._rules:
            if r.tenant_id != tenant_id or country not in r.countries:
                continue
            if need_category is not None and need_category not in r.need_categories:
                continue
            if buyer_type is not None and buyer_type not in r.buyer_types:
                continue
            matched.append(r)
        return sorted(matched, key=lambda r: (r.priority, str(r.employee_id)))

    async def list_by_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> list[TerritoryAssignment]:
        return [r for r in self._rules if r.tenant_id == tenant_id and r.employee_id == employee_id]


class _FakeOwnershipRepository:
    def __init__(
        self,
        locks: list[OwnershipLock] | None = None,
        transfers: list[OwnershipTransfer] | None = None,
    ) -> None:
        self._locks = {l.account_id: l for l in (locks or [])}
        self._transfers = list(transfers or [])
        self.lock_attempts: list[OwnershipLock] = []
        self.replaced: list[tuple[OwnershipLock | None, OwnershipLock, OwnershipTransfer]] = []

    async def try_lock(self, lock: OwnershipLock) -> bool:
        self.lock_attempts.append(lock)
        if lock.account_id in self._locks:
            return False
        self._locks[lock.account_id] = lock
        return True

    async def get(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> OwnershipLock | None:
        return self._locks.get(account_id)

    async def replace(
        self,
        tenant_id: TenantId,
        new_lock: OwnershipLock,
        transfer: OwnershipTransfer,
    ) -> None:
        # 模拟原子：换锁 + 追加历史在同一次调用内完成（要么都写，要么抛出不写）
        old = self._locks.get(new_lock.account_id)
        self._locks[new_lock.account_id] = new_lock
        self._transfers.append(transfer)
        self.replaced.append((old, new_lock, transfer))

    async def list_transfers(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[OwnershipTransfer]:
        return [t for t in self._transfers if t.tenant_id == tenant_id and t.account_id == account_id]


class _FailingOwnershipRepository(_FakeOwnershipRepository):
    """replace 抛异常模拟事务回滚：不提交任何变更（锁与历史都保持原样）。"""

    def __init__(
        self, locks: list[OwnershipLock] | None = None, transfers: list[OwnershipTransfer] | None = None
    ) -> None:
        super().__init__(locks, transfers)
        self.replace_calls = 0

    async def replace(
        self,
        tenant_id: TenantId,
        new_lock: OwnershipLock,
        transfer: OwnershipTransfer,
    ) -> None:
        self.replace_calls += 1
        raise RuntimeError("transaction failed")


class _FakeAuthorizer:
    def __init__(self, allow: bool = True, rule: str = "allow") -> None:
        self.allow = allow
        self.rule = rule
        self.calls: list[tuple[Actor, EmployeeAction, EmployeeScope, TenantId]] = []

    def require(
        self,
        actor: Actor,
        action: EmployeeAction,
        scope: EmployeeScope,
        tenant_id: TenantId,
    ) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        if not self.allow:
            raise PermissionDenied(f"denied: {action.value}")
        return self.rule


class _FakeAudit:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def log(self, **kwargs: str) -> None:
        self.entries.append(kwargs)


def _make_service(
    *,
    employees: list[Employee] | None = None,
    rules: list[TerritoryAssignment] | None = None,
    locks: list[OwnershipLock] | None = None,
    transfers: list[OwnershipTransfer] | None = None,
    counts: dict[str, int] | None = None,
    pool: list[str] | None = None,
    allow: bool = True,
    auth_rule: str = "allow",
    authorizer: _FakeAuthorizer | None = None,
    audit: _FakeAudit | None = None,
    ownership: _FakeOwnershipRepository | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[EmployeeServiceImpl, _FakeEmployeeRepository, _FakeTerritoryRepository, _FakeOwnershipRepository, _FakeAuthorizer, _FakeAudit]:
    employee_repo = _FakeEmployeeRepository(employees or [], counts)
    territory_repo = _FakeTerritoryRepository(rules or [])
    ownership_repo = ownership or _FakeOwnershipRepository(locks or [], transfers or [])
    auth = authorizer or _FakeAuthorizer(allow=allow, rule=auth_rule)
    audit = audit or _FakeAudit()
    service = EmployeeServiceImpl(
        employees=employee_repo,
        territories=territory_repo,
        ownership=ownership_repo,
        now=now or (lambda: AT),
        manager_pool=lambda _tenant: [EmployeeId(e) for e in (pool or [])],
        count_active_accounts=employee_repo.count_active_accounts,  # 复用仓储同一数据源
        authorizer=auth,
        audit=audit,
    )
    return service, employee_repo, territory_repo, ownership_repo, auth, audit


# --- resolve_owner：已有锁幂等、八级字典序 ---------------------------------------


async def test_resolve_owner_existing_lock_idempotent() -> None:
    service, _er, _tr, ownership, _auth, _audit = _make_service(
        locks=[_lock("e-old", rule="3")]
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", need_category="hinges"
    )
    assert result.owner == EmployeeId("e-old")
    assert result.locked_by_rule == "3"
    assert ownership.lock_attempts == []  # 已有锁幂等：未再尝试上锁


async def test_resolve_owner_boss_override_is_level_one() -> None:
    service, *_ = _make_service(employees=[_emp("boss-1")])
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", boss_override=EmployeeId("boss-1")
    )
    assert result.owner == EmployeeId("boss-1")
    assert result.locked_by_rule == "1"


async def test_resolve_owner_historical_owner_is_level_two() -> None:
    # 历史负责人 = 最近一次转移的 to_owner（最近实际负责人），不是 from_owner
    transfers = [
        _transfer("tr-1", from_owner="old-1", to_owner="hist-1", at=AT),
    ]
    service, *_ = _make_service(employees=[_emp("hist-1")], transfers=transfers)
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", need_category="hinges"
    )
    assert result.owner == EmployeeId("hist-1")
    assert result.locked_by_rule == "2"


async def test_resolve_owner_historical_owner_uses_most_recent_transfer() -> None:
    # 多条历史：按 transferred_at 升序取最后一条的 to_owner
    transfers = [
        _transfer("tr-1", from_owner="old-1", to_owner="mid-1", at=AT),
        _transfer("tr-2", from_owner="mid-1", to_owner="latest-1", at=AT + timedelta(hours=1)),
    ]
    service, *_ = _make_service(employees=[_emp("latest-1")], transfers=transfers)
    result = await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")
    assert result.owner == EmployeeId("latest-1")  # 最近实际负责人
    assert result.locked_by_rule == "2"


async def test_resolve_owner_need_category_rule_is_level_three() -> None:
    service, *_ = _make_service(employees=[_emp("e-1")], rules=[_rule("e-1", need="hinges")])
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", need_category="hinges"
    )
    assert result.owner == EmployeeId("e-1")
    assert result.locked_by_rule == "3"


async def test_resolve_owner_buyer_type_rule_is_level_four() -> None:
    service, *_ = _make_service(employees=[_emp("e-2")], rules=[_rule("e-2", buyer="wholesaler")])
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", buyer_type="wholesaler"
    )
    assert result.owner == EmployeeId("e-2")
    assert result.locked_by_rule == "4"


async def test_resolve_owner_language_is_level_five() -> None:
    service, *_ = _make_service(employees=[_emp("e-lang", languages=["en"])])
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", language="en"
    )
    assert result.owner == EmployeeId("e-lang")
    assert result.locked_by_rule == "5"


async def test_resolve_owner_timezone_mismatch_is_skipped() -> None:
    service, *_ = _make_service(
        employees=[
            _emp("e-sh", timezone="Asia/Shanghai"),
            _emp("e-berlin", timezone="Europe/Berlin"),
        ],
        counts={"e-sh": 0, "e-berlin": 0},
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", timezone="Asia/Shanghai"
    )
    assert result.owner == EmployeeId("e-sh")  # 时区错配的 e-berlin 被跳过
    assert result.locked_by_rule == "5"


async def test_resolve_owner_language_and_timezone_both_required() -> None:
    # 有输入时不猜测：候选必须同时满足已提供的 language 与 timezone
    service, *_ = _make_service(
        employees=[
            _emp("e-both", languages=["en"], timezone="Asia/Shanghai"),
            _emp("e-lang-only", languages=["en"], timezone="Europe/Berlin"),
        ],
        counts={"e-both": 0, "e-lang-only": 0},
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", language="en", timezone="Asia/Shanghai"
    )
    assert result.owner == EmployeeId("e-both")  # 两者都满足才入选
    assert result.locked_by_rule == "5"


async def test_resolve_owner_level5_picks_sales_not_manager_even_if_both_match() -> None:
    # 语言匹配：SALES 与 MANAGER 都匹配 → level5 只从 active SALES 常规池选
    service, *_ = _make_service(
        employees=[
            _emp("sales-en", languages=["en"]),  # SALES，负载更高
            _emp("mgr-en", role=Role.MANAGER, languages=["en"]),  # MANAGER，负载更低
        ],
        counts={"sales-en": 5, "mgr-en": 0},
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", language="en"
    )
    assert result.owner == EmployeeId("sales-en")  # 只从 SALES 常规池选，即使 MANAGER 负载更低
    assert result.locked_by_rule == "5"


async def test_resolve_owner_level5_does_not_steal_matching_backup_or_manager() -> None:
    # backup-lang 是 language=en 的 active SALES（默认角色）：未排除 backup 时它会在
    # level5/6 被选。正确行为必须因 **backup 身份**被排除常规池，最终 level7 由它接手。
    # m-lang（MANAGER）与池成员同样不得在 level5 抢占。
    service, *_ = _make_service(
        employees=[
            _emp("m-lang", role=Role.MANAGER, languages=["en"]),
            _emp("backup-lang", languages=["en"]),  # SALES + en：仅因 backup 身份被排除
        ],
        rules=[_rule("primary", need="hinges", backup="backup-lang")],
        pool=["m-lang"],
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", language="en", need_category="hinges"
    )
    assert result.owner == EmployeeId("backup-lang")
    assert result.locked_by_rule == "7"  # 不是 level5/6：常规池排除 backup 后由 level7 接手


async def test_resolve_owner_level6_least_active_and_employee_id_tiebreak() -> None:
    service, *_ = _make_service(
        employees=[_emp("e-aaa"), _emp("e-bbb")],
        counts={"e-aaa": 3, "e-bbb": 1},
    )
    result = await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")
    assert result.owner == EmployeeId("e-bbb")  # 活跃客户数最少
    assert result.locked_by_rule == "6"

    # 相同计数 → 稳定 employee_id 字典序 tie-break
    service, *_ = _make_service(
        employees=[_emp("e-aaa"), _emp("e-bbb")],
        counts={"e-aaa": 2, "e-bbb": 2},
    )
    result = await service.resolve_owner(
        TENANT, ProspectAccountId("acct-2"), actor=ACTOR, country="US"
    )
    assert result.owner == EmployeeId("e-aaa")  # employee_id 升序最小
    assert result.locked_by_rule == "6"


async def test_resolve_owner_level7_backup_active_when_level6_empty() -> None:
    # 主员工停用 + 备用 active MANAGER → level6 常规池为空 → level7 备用可达
    service, *_ = _make_service(
        employees=[
            _emp("e-primary", active=False),
            _emp("e-backup", role=Role.MANAGER),
        ],
        rules=[_rule("e-primary", need="hinges", backup="e-backup")],
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US", need_category="hinges"
    )
    assert result.owner == EmployeeId("e-backup")
    assert result.locked_by_rule == "7"


async def test_resolve_owner_level7_backup_from_need_rule_union_when_both_dimensions() -> None:
    # need + buyer 同时提供：两主负责人都 inactive，只有 need 规则里的 active backup，
    # 常规销售池为空 → level7 必须考虑 country+need 与 country+buyer 的确定性并集并命中 backup
    service, *_ = _make_service(
        employees=[
            _emp("need-primary", active=False),
            _emp("buyer-primary", active=False),
            _emp("need-backup", role=Role.MANAGER),  # 只在 need 规则里的 active backup
        ],
        rules=[
            _rule("need-primary", need="hinges", backup="need-backup"),
            _rule("buyer-primary", buyer="wholesaler"),
        ],
    )
    result = await service.resolve_owner(
        TENANT, ACCOUNT, actor=ACTOR, country="US",
        need_category="hinges", buyer_type="wholesaler",
    )
    assert result.owner == EmployeeId("need-backup")
    assert result.locked_by_rule == "7"


async def test_resolve_owner_level8_manager_pool_fallback_not_random() -> None:
    # 池成员须经员工仓储验证 active 且 MANAGER/BOSS；最少负载 + 稳定 tie-break
    service, *_ = _make_service(
        employees=[_emp("m-aaa", role=Role.MANAGER), _emp("m-bbb", role=Role.MANAGER)],
        pool=["m-aaa", "m-bbb"],
        counts={"m-aaa": 2, "m-bbb": 5},
    )
    result = await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")
    assert result.owner == EmployeeId("m-aaa")  # 池内最少负载
    assert result.locked_by_rule == "8"

    # 多次调用结果一致（确定性，非随机）
    result2 = await service.resolve_owner(
        TENANT, ProspectAccountId("acct-9"), actor=ACTOR, country="US"
    )
    assert result2.owner == EmployeeId("m-aaa")


async def test_resolve_owner_level8_skips_inactive_or_non_manager_pool_members() -> None:
    # 非法/停用池成员被跳过，不参与分配
    service, *_ = _make_service(
        employees=[
            _emp("m-ok", role=Role.MANAGER),
            _emp("m-inactive", role=Role.MANAGER, active=False),
            _emp("s-notmanager"),
        ],
        pool=["m-ok", "m-inactive", "s-notmanager"],
        counts={"m-ok": 0},
    )
    result = await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")
    assert result.owner == EmployeeId("m-ok")
    assert result.locked_by_rule == "8"


async def test_resolve_owner_level8_all_pool_members_illegal_raises() -> None:
    # 池成员全部非法 → 无可分配 → NoAssignmentRuleError
    service, *_ = _make_service(employees=[_emp("s-only")], pool=["s-only"])
    with pytest.raises(NoAssignmentRuleError):
        await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")


async def test_resolve_owner_first_hit_stops_at_higher_level() -> None:
    # 老板指定 + 有 territory 规则 + 有活跃员工 → 只到第 1 级即停
    service, *_ = _make_service(
        employees=[_emp("e-1"), _emp("boss-9")],
        rules=[_rule("e-1", need="hinges")],
        counts={"e-1": 0, "boss-9": 0},
    )
    result = await service.resolve_owner(
        TENANT,
        ACCOUNT,
        actor=ACTOR,
        country="US",
        need_category="hinges",
        boss_override=EmployeeId("boss-9"),
    )
    assert result.owner == EmployeeId("boss-9")  # 首命中即停，不落到 3/6 级
    assert result.locked_by_rule == "1"


async def test_resolve_owner_no_rule_and_empty_pool_raises() -> None:
    service, *_ = _make_service(pool=[])
    with pytest.raises(NoAssignmentRuleError):
        await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")


# --- transfer：原子 replace、字段完整、输入校验 -----------------------------------


async def test_transfer_records_complete_ownership_transfer_atomically() -> None:
    service, _er, _tr, ownership, _auth, _audit = _make_service(
        employees=[_emp("e-new"), _emp("boss-1")],
        locks=[_lock("e-old")],
    )
    await service.transfer(
        TENANT,
        ACCOUNT,
        EmployeeId("e-new"),
        actor=ACTOR,
        transferred_by=EmployeeId("boss-1"),
        reason="boss move",
    )
    # 服务只调用一次原子 replace（换锁 + 历史同一次）
    assert len(ownership.replaced) == 1
    old, new_lock, t = ownership.replaced[0]
    assert old is not None and old.owner == EmployeeId("e-old")
    assert new_lock.owner == EmployeeId("e-new")
    assert t.from_owner == EmployeeId("e-old")
    assert t.to_owner == EmployeeId("e-new")
    assert t.transferred_by == EmployeeId("boss-1")
    assert t.transferred_at == AT
    assert t.reason  # 非空
    current = await ownership.get(TENANT, ACCOUNT)
    assert current is not None and current.owner == EmployeeId("e-new")
    assert len(ownership._transfers) == 1  # 只增历史


async def test_transfer_atomic_failure_leaves_lock_and_history_intact() -> None:
    # 失败时绝不允许出现「只换锁、历史没写」——仓储原子，锁与历史都不变
    ownership = _FailingOwnershipRepository(locks=[_lock("e-old")])
    service, _er, _tr, _own, _auth, _audit = _make_service(
        employees=[_emp("e-new"), _emp("boss-1")],
        ownership=ownership,
    )
    with pytest.raises(RuntimeError):
        await service.transfer(
            TENANT, ACCOUNT, EmployeeId("e-new"), actor=ACTOR,
            transferred_by=EmployeeId("boss-1"), reason="r",
        )
    assert ownership.replace_calls == 1  # 只调一次原子 replace
    current = await ownership.get(TENANT, ACCOUNT)
    assert current is not None and current.owner == EmployeeId("e-old")  # 锁未变
    assert len(ownership._transfers) == 0  # 历史未写


async def test_transfer_without_existing_lock_raises() -> None:
    service, *_ = _make_service()
    with pytest.raises(NoOwnershipLockError):
        await service.transfer(
            TENANT, ACCOUNT, EmployeeId("e-new"), actor=ACTOR,
            transferred_by=EmployeeId("boss-1"), reason="r",
        )


async def test_transfer_rejects_blank_reason() -> None:
    service, *_ = _make_service(
        employees=[_emp("e-new"), _emp("boss-1")], locks=[_lock("e-old")]
    )
    with pytest.raises(ValidationError):
        await service.transfer(
            TENANT, ACCOUNT, EmployeeId("e-new"), actor=ACTOR,
            transferred_by=EmployeeId("boss-1"), reason="   ",
        )


async def test_transfer_rejects_inactive_new_owner() -> None:
    service, *_ = _make_service(
        employees=[_emp("e-new", active=False), _emp("boss-1")], locks=[_lock("e-old")]
    )
    with pytest.raises(ValidationError):
        await service.transfer(
            TENANT, ACCOUNT, EmployeeId("e-new"), actor=ACTOR,
            transferred_by=EmployeeId("boss-1"), reason="r",
        )


async def test_transfer_strips_reason_before_persisting() -> None:
    service, _er, _tr, ownership, _auth, _audit = _make_service(
        employees=[_emp("e-new"), _emp("boss-1")],
        locks=[_lock("e-old")],
    )
    await service.transfer(
        TENANT, ACCOUNT, EmployeeId("e-new"), actor=ACTOR,
        transferred_by=EmployeeId("boss-1"), reason="  handoff  ",
    )
    t = ownership.replaced[0][2]
    assert t.reason == "handoff"  # strip 后持久化


async def test_transfer_rejects_foreign_transferred_by() -> None:
    # transferred_by 不属于该租户（仓储查不到）→ 拒绝
    service, *_ = _make_service(employees=[_emp("e-new")], locks=[_lock("e-old")])
    with pytest.raises(ValidationError):
        await service.transfer(
            TENANT, ACCOUNT, EmployeeId("e-new"), actor=ACTOR,
            transferred_by=EmployeeId("ghost"), reason="r",
        )


# --- apply_territory_from_directive：只改未来规则，不回溯已有锁 -----------------------


async def test_directive_only_affects_future_rules() -> None:
    service, _er, territory, ownership, _auth, _audit = _make_service(
        employees=[_emp("e-new"), _emp("e-old")],
        locks=[_lock("e-old")],
    )
    await service.apply_territory_from_directive(
        TENANT, {"US": "e-new"}, actor=ACTOR
    )
    # 已有归属锁不动
    existing = await ownership.get(TENANT, ACCOUNT)
    assert existing is not None and existing.owner == EmployeeId("e-old")
    # 新增一条未来规则：country=US、高优先级、effective_from=now
    assert len(territory.added) == 1
    added = territory.added[0]
    assert added.employee_id == EmployeeId("e-new")
    assert added.countries == ["US"]
    assert added.effective_from == AT


async def test_directive_unresolvable_target_raises() -> None:
    service, *_ = _make_service()
    with pytest.raises(ValidationError):
        await service.apply_territory_from_directive(
            TENANT, {"US": "nobody"}, actor=ACTOR
        )


# --- 公共 View：不暴露 models -----------------------------------------------------


async def test_public_views_do_not_expose_models() -> None:
    from domains.employees import models, schemas

    for name in dir(models):
        obj = getattr(models, name)
        if isinstance(obj, type) and obj.__module__ == models.__name__:
            assert not hasattr(schemas, name), f"employees.schemas 泄露内部 models 类: {name}"
    # 公共 View 存在且可构造（供 apps 消费）
    assert EmployeeView(employee_id=EmployeeId("e"), tenant_id=TENANT, name="n", role="sales")
    assert OwnershipLockView(tenant_id=TENANT, account_id=ACCOUNT, owner=EmployeeId("e"), locked_at=AT, locked_by_rule="1")
    assert TerritoryAssignmentView(tenant_id=TENANT, employee_id=EmployeeId("e"), priority=1, effective_from=AT)


async def test_service_returns_public_views_not_models() -> None:
    service, *_ = _make_service(employees=[_emp("e-1")])
    emp_view = await service.get_employee(TENANT, EmployeeId("e-1"), actor=ACTOR)
    assert isinstance(emp_view, EmployeeView)
    assert emp_view.role == "sales"  # 字符串，不引内部 Role 枚举


async def test_list_territory_matrix_is_tenant_bound_and_stably_ordered() -> None:
    service, _er, _tr, _own, auth, _audit = _make_service(
        employees=[_emp("e-2"), _emp("e-1"), _emp("inactive", active=False)],
        rules=[
            _rule("e-1", priority=2, need="hinges"),
            _rule("inactive", priority=0, need="ignored"),
            _rule("e-2", priority=1, buyer="wholesaler"),
            _rule("e-1", priority=1, buyer="retailer"),
        ],
    )

    matrix = await service.list_territory_matrix(TENANT, actor=ACTOR)

    assert [(item.priority, item.employee_id) for item in matrix] == [
        (1, EmployeeId("e-1")),
        (1, EmployeeId("e-2")),
        (2, EmployeeId("e-1")),
    ]
    assert auth.calls[-1][1] is EmployeeAction.ASSIGNMENT_LIST


# --- 服务层授权：所有公开读写判权、未知 action 默认拒绝、审计仅白名单字段 --------------


async def test_all_public_reads_and_writes_authorize() -> None:
    employees = [_emp("e-1"), _emp("e-2"), _emp("boss")]
    rules = [_rule("e-1", need="hinges"), _rule("e-2", buyer="wholesaler")]
    service, _er, _tr, _own, auth, _audit = _make_service(
        employees=employees, rules=rules
    )
    actions: list[EmployeeAction] = []

    await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US", need_category="hinges")
    actions.append(EmployeeAction.OWNERSHIP_LOCK)
    await service.transfer(TENANT, ACCOUNT, EmployeeId("e-2"), actor=ACTOR, transferred_by=EmployeeId("boss"), reason="r")
    actions.append(EmployeeAction.OWNERSHIP_TRANSFER)
    await service.get_ownership(TENANT, ACCOUNT, actor=ACTOR)
    actions.append(EmployeeAction.OWNERSHIP_READ)
    await service.apply_territory_from_directive(TENANT, {"US": "e-1"}, actor=ACTOR)
    actions.append(EmployeeAction.TERRITORY_APPLY)
    await service.get_employee(TENANT, EmployeeId("e-1"), actor=ACTOR)
    actions.append(EmployeeAction.EMPLOYEE_READ)
    await service.list_active(TENANT, actor=ACTOR)
    actions.append(EmployeeAction.EMPLOYEE_LIST)
    await service.list_assignments(TENANT, EmployeeId("e-1"), actor=ACTOR)
    actions.append(EmployeeAction.ASSIGNMENT_LIST)
    await service.list_territory_matrix(TENANT, actor=ACTOR)
    actions.append(EmployeeAction.ASSIGNMENT_LIST)

    called = [a for _actor, a, _scope, _t in auth.calls]
    assert called == actions  # 每个公开方法都先判权，且 action 正确


async def test_service_propagates_authorizer_denial() -> None:
    # 注入 authorizer 拒绝 → 操作失败、不产生任何副作用，且拒绝也审计
    service, _er, _tr, ownership, _auth, audit = _make_service(
        employees=[_emp("e-1")], allow=False
    )
    with pytest.raises(PermissionDenied):
        await service.resolve_owner(TENANT, ACCOUNT, actor=ACTOR, country="US")
    assert ownership.lock_attempts == []  # 判权失败未上锁
    assert audit.entries[-1]["rule"] == "deny"


def test_actor_requires_explicit_scope() -> None:
    # scope 不得默认成最高权限 TENANT；必须显式传
    with pytest.raises(TypeError):
        Actor(actor_id="x")  # type: ignore[call-arg]
    # 显式传 scope 保持可用
    assert Actor(actor_id="x", scope=EmployeeScope.MANAGER).scope == EmployeeScope.MANAGER


async def test_unknown_action_default_denied() -> None:
    auth = DefaultDenyAuthorizer()
    with pytest.raises(PermissionDenied):
        auth.require(ACTOR, "totally:unknown", EmployeeScope.TENANT, TENANT)  # type: ignore[arg-type]
    with pytest.raises(PermissionDenied):
        auth.require(ACTOR, EmployeeAction.EMPLOYEE_READ, "unknown-scope", TENANT)  # type: ignore[arg-type]


async def test_authorization_audit_only_whitelist_fields_no_sensitive_values() -> None:
    service, _er, _tr, _own, _auth, audit = _make_service(
        employees=[_emp("e-1")], locks=[_lock("e-1")]
    )
    await service.get_ownership(TENANT, ACCOUNT, actor=ACTOR)  # 读取会接触账户数据
    entry = audit.entries[-1]
    assert set(entry.keys()) == {"actor", "action", "tenant_id", "scope", "rule"}
    assert str(ACCOUNT) not in str(entry)  # 无账户/锁等敏感值
    assert entry["actor"] == "actor-boss"
    assert entry["action"] == EmployeeAction.OWNERSHIP_READ.value
    assert entry["scope"] == EmployeeScope.TENANT.value
    assert entry["rule"] == "allow"


async def test_authorization_denial_audit_rule_deny_no_business_content() -> None:
    service, _er, _tr, _own, _auth, audit = _make_service(
        employees=[_emp("e-1")], allow=False
    )
    with pytest.raises(PermissionDenied):
        await service.get_employee(TENANT, EmployeeId("e-1"), actor=ACTOR)
    entry = audit.entries[-1]
    assert set(entry.keys()) == {"actor", "action", "tenant_id", "scope", "rule"}
    assert entry["rule"] == "deny"
    assert entry["action"] == EmployeeAction.EMPLOYEE_READ.value
    assert "e-1" not in str(entry)  # 无业务内容
    assert "denied" not in str(entry)  # 无异常消息内容
