"""S3-4 employees 持久化集成测试（0003 迁移 + infra 仓储）。

行为断言，不依赖实现细节：
- 0003 四表存在且各含 tenant_id NOT NULL；逐列集合与 Schema 附录一致。
- 0002→0003→0002→0003 round-trip，finally 恢复 head。
- 复合 FK：territory employee_id/manager_id/backup_employee_id、
  transfer from_owner/to_owner/transferred_by 均 (tenant_id, ...)→employees；
  跨租户引用被拒、同租户合法。
- transfer reason ``CHECK btrim(reason) <> ''``；历史 UPDATE/DELETE DB 触发器拒绝。
- Employee repo get/add/update/list_active/count_active_accounts
  （count 统计当前租户 owner 对应非终态机会的 distinct account_id，排除 won/lost）。
- Territory repo add/list_matching（维度 + 有效窗口 + priority/employee_id 稳定排序）
  /list_by_employee。
- Ownership try_lock 唯一并发（UNIQUE(tenant_id,account_id) 真正支撑）、get、
  replace 同一事务更新当前锁+追加历史（失败不得部分提交）、list_transfers 时间升序。
- 所有 repo 读写跨租户负测：读 None/[]/0/False，写 ValueError。

RED 前置：0003 迁移/仓储未建 → 表缺失断言失败 + 仓储导入转行为失败
（非收集错误）。域私有模型经 ``from domains.employees import models`` 引入。
禁止打印/记录任何连接串（db_url 经 conftest RedactedUrl 脱敏）。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import TextClause, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from domains.employees import models
from domains.employees.errors import OwnershipConflictError
from shared.schemas.identifiers import (
    EmployeeId,
    ProspectAccountId,
    TeamId,
    TenantId,
    UserId,
)

Employee = models.Employee
Role = models.Role
TerritoryAssignment = models.TerritoryAssignment
OwnershipLock = models.OwnershipLock
OwnershipTransfer = models.OwnershipTransfer

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 四表（Schema 附录）。
EXPECTED_TABLES: tuple[str, ...] = (
    "employees",
    "territory_assignments",
    "ownership_locks",
    "ownership_transfer_history",
)

EMPLOYEE_COLUMNS = {
    "employee_id", "tenant_id", "name", "role", "created_at", "user_id",
    "team_id", "manager_id", "languages", "timezone", "is_active", "max_active_accounts",
}
TERRITORY_COLUMNS = {
    "assignment_id", "tenant_id", "employee_id", "priority", "effective_from",
    "countries", "product_categories", "need_categories", "buyer_types",
    "languages", "manager_id", "backup_employee_id", "effective_until",
}
LOCK_COLUMNS = {"lock_id", "tenant_id", "account_id", "owner", "locked_at", "locked_by_rule"}
TRANSFER_COLUMNS = {
    "transfer_id", "tenant_id", "account_id", "from_owner", "to_owner",
    "transferred_by", "transferred_at", "reason",
}

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "EmployeeRepositoryImpl": "infra.db.repositories.employees",
    "TerritoryRepositoryImpl": "infra.db.repositories.employees",
    "OwnershipRepositoryImpl": "infra.db.repositories.employees",
}


def _load(symbol: str):
    """按模块字符串导入仓储符号；缺失转行为失败（RED 阶段未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _emp(eid: str, *, tenant: str = "tEmp", role: str = "sales", active: bool = True, languages: list[str] | None = None) -> Employee:
    return Employee(
        employee_id=EmployeeId(eid),
        tenant_id=TenantId(tenant),
        name=f"{eid}-name",
        role=Role(role),
        created_at=_NOW,
        is_active=active,
        languages=languages or [],
    )


def _rule(
    employee: str,
    *,
    tenant: str = "tTerritory",
    priority: int = 1,
    need: str | None = None,
    buyer: str | None = None,
    countries: list[str] | None = None,
    effective_from: datetime = _NOW,
    effective_until: datetime | None = None,
) -> TerritoryAssignment:
    return TerritoryAssignment(
        tenant_id=TenantId(tenant),
        employee_id=EmployeeId(employee),
        priority=priority,
        effective_from=effective_from,
        countries=countries or ["US"],
        need_categories=[need] if need else [],
        buyer_types=[buyer] if buyer else [],
        effective_until=effective_until,
    )


def _lock(owner: str, *, tenant: str = "tOwn", account: str = "acct-1") -> OwnershipLock:
    return OwnershipLock(
        tenant_id=TenantId(tenant),
        account_id=ProspectAccountId(account),
        owner=EmployeeId(owner),
        locked_at=_NOW,
        locked_by_rule="3",
    )


def _transfer(
    tid: str,
    *,
    from_owner: str | None,
    to_owner: str,
    reason: str,
    at: datetime = _NOW,
    tenant: str = "tOwn",
    account: str = "acct-1",
) -> OwnershipTransfer:
    return OwnershipTransfer(
        transfer_id=tid,
        tenant_id=TenantId(tenant),
        account_id=ProspectAccountId(account),
        from_owner=EmployeeId(from_owner) if from_owner else None,
        to_owner=EmployeeId(to_owner),
        transferred_by=EmployeeId(to_owner),
        transferred_at=at,
        reason=reason,
    )


def _run_alembic(db_url: str, *command: str) -> None:
    """在仓库根运行 ``alembic <command>``；仅经 env 注入 DATABASE_URL，不输出连接内容。"""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *command],
        capture_output=True,
        check=False,
        env=env,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, f"alembic {' '.join(command)} 失败（不输出连接内容）"


def _sync_table_names(conn: Connection) -> list[str]:
    return inspect(conn).get_table_names()


def _sync_columns(conn: Connection, table: str) -> list[dict[str, object]]:
    return [dict(c) for c in inspect(conn).get_columns(table)]


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(_sync_table_names))


async def _columns(engine: AsyncEngine, table: str) -> dict[str, bool]:
    """列名 → 是否 nullable。"""
    async with engine.connect() as conn:
        cols = await conn.run_sync(_sync_columns, table)
    return {str(c["name"]): bool(c["nullable"]) for c in cols}


_INSERT_EMPLOYEE = text(
    "INSERT INTO employees (employee_id, tenant_id, name, role) "
    "VALUES (:employee_id, :tenant_id, :name, :role)"
)

_INSERT_TERRITORY = text(
    "INSERT INTO territory_assignments ("
    "assignment_id, tenant_id, employee_id, priority, effective_from, "
    "countries, need_categories, buyer_types) "
    "VALUES (:assignment_id, :tenant_id, :employee_id, :priority, now(), "
    "CAST(:countries AS text[]), CAST(:need_categories AS text[]), CAST(:buyer_types AS text[]))"
)

_INSERT_LOCK = text(
    "INSERT INTO ownership_locks (lock_id, tenant_id, account_id, owner, locked_at, locked_by_rule) "
    "VALUES (:lock_id, :tenant_id, :account_id, :owner, now(), :locked_by_rule)"
)

_INSERT_TRANSFER = text(
    "INSERT INTO ownership_transfer_history ("
    "transfer_id, tenant_id, account_id, from_owner, to_owner, transferred_by, transferred_at, reason) "
    "VALUES (:transfer_id, :tenant_id, :account_id, :from_owner, :to_owner, :transferred_by, now(), :reason)"
)

_INSERT_OPPORTUNITY = text(
    "INSERT INTO opportunities ("
    "opportunity_id, tenant_id, account_id, account_name, country, need_id, product_category, "
    "state, owner, closed_by, closed_at, loss_reason, died_at_state) "
    "VALUES (:opportunity_id, :tenant_id, :account_id, :account_name, :country, :need_id, "
    ":product_category, :state, :owner, :closed_by, :closed_at, :loss_reason, :died_at_state)"
)


async def _assert_statement_integrity_rejected(
    engine: AsyncEngine,
    statement: TextClause,
    params: Mapping[str, object],
    label: str,
) -> None:
    """INSERT 应被 CHECK/唯一/FK/NOT NULL 拒绝（IntegrityError）；独立事务回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(statement, params)
            await conn.rollback()
            pytest.fail(label)
        except IntegrityError:
            await conn.rollback()


async def _assert_dml_rejected(
    engine: AsyncEngine, table: str, statement: str, params: dict[str, object]
) -> None:
    """UPDATE/DELETE 应被 DB 拒绝（只增触发器）；独立事务回滚。"""
    async with engine.connect() as conn:
        try:
            await conn.execute(text(statement), params)
        except DBAPIError:
            await conn.rollback()
        else:
            await conn.rollback()
            pytest.fail(f"{table}：该语句应被数据库拒绝")


# --- 迁移：表结构 / 列集合 / round-trip / 约束 / FK / 触发器 ------------------------


async def test_four_tables_exist_with_tenant_id_not_null(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        missing = set(EXPECTED_TABLES) - names
        assert not missing, f"缺失表：{sorted(missing)}"
        for table in EXPECTED_TABLES:
            cols = await _columns(engine, table)
            assert "tenant_id" in cols, f"{table} 缺 tenant_id（硬边界 8）"
            assert cols["tenant_id"] is False, f"{table}.tenant_id 应 NOT NULL"
    finally:
        await engine.dispose()


async def test_four_table_column_sets_match_appendix(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        for table, expected in (
            ("employees", EMPLOYEE_COLUMNS),
            ("territory_assignments", TERRITORY_COLUMNS),
            ("ownership_locks", LOCK_COLUMNS),
            ("ownership_transfer_history", TRANSFER_COLUMNS),
        ):
            actual = set((await _columns(engine, table)).keys())
            assert actual == expected, f"{table} 列集合不符：{sorted(actual ^ expected)}"
    finally:
        await engine.dispose()


async def test_roundtrip_downgrade_0002_then_upgrade_head(db_url: str) -> None:
    """0002→0003→0002→0003 round-trip；finally 恢复 head，不依赖测试顺序。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        names = await _table_names(engine)
        assert set(EXPECTED_TABLES) <= names, "roundtrip 前置：head 应含四表"
        _run_alembic(db_url, "downgrade", "0002")
        names = await _table_names(engine)
        assert not (set(EXPECTED_TABLES) & names), "downgrade 0002 后四表应消失"
        _run_alembic(db_url, "upgrade", "head")
        names = await _table_names(engine)
        assert set(EXPECTED_TABLES) <= names, "upgrade head 后四表应恢复"
    finally:
        _run_alembic(db_url, "upgrade", "head")
        await engine.dispose()


async def test_transfer_blank_reason_rejected_by_check(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(_INSERT_EMPLOYEE, {"employee_id": "e-r-1", "tenant_id": "tReason", "name": "R", "role": "sales"})
        await _assert_statement_integrity_rejected(
            engine,
            _INSERT_TRANSFER,
            {"transfer_id": "tr-blank", "tenant_id": "tReason", "account_id": "acct-1",
             "from_owner": None, "to_owner": "e-r-1", "transferred_by": "e-r-1", "reason": "   "},
            "空白 reason 应被 CHECK btrim(reason) <> '' 拒绝",
        )
    finally:
        await engine.dispose()


async def test_transfer_history_append_only_rejects_update_delete(db_url: str) -> None:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        tenant = "tAppend"
        async with engine.begin() as conn:
            await conn.execute(_INSERT_EMPLOYEE, {"employee_id": "e-ap-1", "tenant_id": tenant, "name": "A", "role": "sales"})
            await conn.execute(
                _INSERT_TRANSFER,
                {"transfer_id": "tr-ap-1", "tenant_id": tenant, "account_id": "acct-1",
                 "from_owner": None, "to_owner": "e-ap-1", "transferred_by": "e-ap-1", "reason": "init"},
            )
        await _assert_dml_rejected(
            engine,
            "ownership_transfer_history",
            "UPDATE ownership_transfer_history SET reason = :reason WHERE transfer_id = :id",
            {"reason": "changed", "id": "tr-ap-1"},
        )
        await _assert_dml_rejected(
            engine,
            "ownership_transfer_history",
            "DELETE FROM ownership_transfer_history WHERE transfer_id = :id",
            {"id": "tr-ap-1"},
        )
    finally:
        await engine.dispose()


async def test_territory_transfer_composite_fk_tenant_isolation(db_url: str) -> None:
    """复合 FK (tenant_id, ...)→employees：跨租户引用被拒、同租户合法。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        tenant_a = "tFKa"
        tenant_b = "tFKb"
        async with engine.begin() as conn:
            # 两个租户各有一个员工：e-emp 属 tenant_a，e-b-1 属 tenant_b
            await conn.execute(_INSERT_EMPLOYEE, {"employee_id": "e-emp", "tenant_id": tenant_a, "name": "E", "role": "sales"})
            await conn.execute(_INSERT_EMPLOYEE, {"employee_id": "e-b-1", "tenant_id": tenant_b, "name": "B", "role": "sales"})
        # territory：跨租户 employee / manager / backup 引用（指向 tenant_a 的 e-emp）均被拒。
        # 被测 FK 列用 tenant_a 的 e-emp；其余列用 tenant_b 的 e-b-1 以隔离该列。
        for field_sql in ("employee_id", "manager_id", "backup_employee_id"):
            stmt = text(
                "INSERT INTO territory_assignments (assignment_id, tenant_id, employee_id, priority, "
                "effective_from, manager_id, backup_employee_id) VALUES "
                "(:assignment_id, :tenant_id, :employee_id, :priority, now(), :manager_id, :backup_employee_id)"
            )
            await _assert_statement_integrity_rejected(
                engine,
                stmt,
                {
                    "assignment_id": f"t-{field_sql}",
                    "tenant_id": tenant_b,
                    "employee_id": "e-emp" if field_sql == "employee_id" else "e-b-1",
                    "priority": 1,
                    "manager_id": "e-emp" if field_sql == "manager_id" else None,
                    "backup_employee_id": "e-emp" if field_sql == "backup_employee_id" else None,
                },
                f"territory 跨租户 {field_sql} 引用应被复合 FK 拒绝",
            )
        # transfer：跨租户 to_owner / transferred_by / from_owner 引用被拒
        for field_sql in ("to_owner", "transferred_by", "from_owner"):
            stmt = text(
                "INSERT INTO ownership_transfer_history (transfer_id, tenant_id, account_id, from_owner, "
                "to_owner, transferred_by, transferred_at, reason) VALUES "
                "(:transfer_id, :tenant_id, :account_id, :from_owner, :to_owner, :transferred_by, now(), :reason)"
            )
            params = {
                "transfer_id": f"x-{field_sql}",
                "tenant_id": tenant_b,
                "account_id": "acct-1",
                "from_owner": "e-emp" if field_sql == "from_owner" else None,
                "to_owner": "e-emp" if field_sql == "to_owner" else "e-b-1",
                "transferred_by": "e-emp" if field_sql == "transferred_by" else "e-b-1",
                "reason": "move",
            }
            await _assert_statement_integrity_rejected(
                engine,
                stmt,
                params,
                f"transfer 跨租户 {field_sql} 引用应被复合 FK 拒绝",
            )
        # 同租户合法：territory 与 transfer 均成功
        async with engine.begin() as conn:
            await conn.execute(_INSERT_TERRITORY, {
                "assignment_id": "t-ok-1", "tenant_id": tenant_b, "employee_id": "e-b-1",
                "priority": 1, "countries": ["US"], "need_categories": [], "buyer_types": [],
            })
            await conn.execute(_INSERT_TRANSFER, {
                "transfer_id": "tr-ok-1", "tenant_id": tenant_b, "account_id": "acct-1",
                "from_owner": None, "to_owner": "e-b-1", "transferred_by": "e-b-1", "reason": "init",
            })
    finally:
        await engine.dispose()


async def test_all_four_tables_require_tenant_id(db_url: str) -> None:
    """四表缺 tenant_id 插入均被 NOT NULL 拒绝。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        cases: list[tuple[TextClause, Mapping[str, object], str]] = [
            (
                text("INSERT INTO employees (employee_id, name, role) VALUES (:employee_id, :name, :role)"),
                {"employee_id": "e-noten", "name": "N", "role": "sales"},
                "employees 缺 tenant_id",
            ),
            (
                text("INSERT INTO territory_assignments (assignment_id, employee_id, priority, effective_from) VALUES (:assignment_id, :employee_id, :priority, now())"),
                {"assignment_id": "t-noten", "employee_id": "e-noten", "priority": 1},
                "territory_assignments 缺 tenant_id",
            ),
            (
                text("INSERT INTO ownership_locks (lock_id, account_id, owner, locked_at) VALUES (:lock_id, :account_id, :owner, now())"),
                {"lock_id": "l-noten", "account_id": "acct-1", "owner": "e-noten"},
                "ownership_locks 缺 tenant_id",
            ),
            (
                text("INSERT INTO ownership_transfer_history (transfer_id, account_id, to_owner, transferred_by, transferred_at, reason) VALUES (:transfer_id, :account_id, :to_owner, :transferred_by, now(), :reason)"),
                {"transfer_id": "tr-noten", "account_id": "acct-1", "to_owner": "e-noten", "transferred_by": "e-noten", "reason": "init"},
                "ownership_transfer_history 缺 tenant_id",
            ),
        ]
        for stmt, params, label in cases:
            await _assert_statement_integrity_rejected(engine, stmt, params, f"{label} 应被 NOT NULL 拒绝")
    finally:
        await engine.dispose()


# --- 仓储：Employee -------------------------------------------------------------


@pytest_asyncio.fixture
async def repo_session(db_url: str) -> AsyncIterator[AsyncSession]:
    """函数级本地引擎会话（避免 session 级 async fixture 的事件循环 teardown 问题）。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def test_employee_crud_and_list_active(repo_session: AsyncSession) -> None:
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    repo = EmployeeRepositoryImpl(repo_session, TenantId("tEmpCrud"))
    emp = Employee(
        employee_id=EmployeeId("e-crud-1"),
        tenant_id=TenantId("tEmpCrud"),
        name="full-emp",
        role=Role.MANAGER,
        created_at=_NOW,
        user_id=UserId("u-crud-1"),
        team_id=TeamId("team-a"),
        manager_id=EmployeeId("e-mgr"),
        languages=["en", "zh"],
        timezone="Asia/Shanghai",
        is_active=True,
        max_active_accounts=5,
    )
    await repo.add(emp)
    await repo_session.commit()

    found = await repo.get(TenantId("tEmpCrud"), EmployeeId("e-crud-1"))
    assert found is not None
    assert found.name == "full-emp"
    assert found.role == Role.MANAGER
    assert found.user_id == UserId("u-crud-1")
    assert found.team_id == TeamId("team-a")
    assert found.manager_id == EmployeeId("e-mgr")
    assert found.languages == ["en", "zh"]
    assert found.timezone == "Asia/Shanghai"
    assert found.is_active is True
    assert found.max_active_accounts == 5

    emp.is_active = False
    await repo.update(emp)
    await repo_session.commit()
    found = await repo.get(TenantId("tEmpCrud"), EmployeeId("e-crud-1"))
    assert found is not None and found.is_active is False


async def test_employee_list_active_filters_inactive(repo_session: AsyncSession) -> None:
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    repo = EmployeeRepositoryImpl(repo_session, TenantId("tEmpAct"))
    await repo.add(_emp("e-a-1", tenant="tEmpAct"))
    await repo.add(_emp("e-a-2", tenant="tEmpAct", active=False))
    await repo_session.commit()
    active = await repo.list_active(TenantId("tEmpAct"))
    ids = {e.employee_id for e in active}
    assert ids == {EmployeeId("e-a-1")}


async def test_count_active_accounts_distinct_non_terminal(repo_session: AsyncSession) -> None:
    """count_active_accounts：当前租户 owner 对应非终态机会的 distinct account_id。"""
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    repo = EmployeeRepositoryImpl(repo_session, TenantId("tCount"))
    await repo.add(_emp("e-c-1", tenant="tCount"))
    await repo.add(_emp("e-c-2", tenant="tCount"))
    await repo_session.commit()

    rows = [
        # (opp, tenant, account, state, owner, closed_by, closed_at, loss_reason, died_at_state)
        ("opp-1", "tCount", "acc-1", "qualified", "e-c-1", None, None, None, None),
        ("opp-2", "tCount", "acc-2", "qualified", "e-c-1", None, None, None, None),
        ("opp-3", "tCount", "acc-3", "won", "e-c-1", "e-c-1", _NOW, None, None),  # 终态排除
        ("opp-4", "tCount", "acc-4", "lost", "e-c-1", "e-c-1", _NOW, "price", "quoted"),  # 终态排除
        ("opp-5", "tCount", "acc-1", "qualified", "e-c-1", None, None, None, None),  # 重复 account → distinct
        ("opp-6", "tCount", "acc-5", "qualified", "e-c-2", None, None, None, None),  # 他人 owner → 排除
        ("opp-7", "tOther", "acc-9", "qualified", "e-c-1", None, None, None, None),  # 跨租户 → 排除
    ]
    async with repo_session.begin():
        for opp_id, tenant, account, state, owner, closed_by, closed_at, loss_reason, died_at_state in rows:
            await repo_session.execute(
                _INSERT_OPPORTUNITY,
                {"opportunity_id": opp_id, "tenant_id": tenant, "account_id": account,
                 "account_name": "Acme", "country": "US", "need_id": f"need-{opp_id}",
                 "product_category": "hinges", "state": state, "owner": owner,
                 "closed_by": closed_by, "closed_at": closed_at,
                 "loss_reason": loss_reason, "died_at_state": died_at_state},
            )
    count = await repo.count_active_accounts(TenantId("tCount"), EmployeeId("e-c-1"))
    assert count == 2  # {acc-1, acc-2}


async def test_employee_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    repo = EmployeeRepositoryImpl(repo_session, TenantId("tBound"))
    await repo.add(_emp("e-bnd-1", tenant="tBound"))
    await repo_session.commit()

    assert await repo.get(TenantId("tOther"), EmployeeId("e-bnd-1")) is None
    assert await repo.list_active(TenantId("tOther")) == []
    assert await repo.count_active_accounts(TenantId("tOther"), EmployeeId("e-bnd-1")) == 0
    with pytest.raises(ValueError):
        await repo.add(_emp("e-x-1", tenant="tOther"))
    with pytest.raises(ValueError):
        await repo.update(_emp("e-x-1", tenant="tOther"))


# --- 仓储：Territory --------------------------------------------------------------


async def test_territory_full_round_trip_and_list_by_employee(repo_session: AsyncSession) -> None:
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    TerritoryRepositoryImpl = _load("TerritoryRepositoryImpl")
    tenant = TenantId("tTer")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("e-t-1", tenant="tTer"))
    await emp_repo.add(_emp("e-t-2", tenant="tTer"))
    await emp_repo.add(_emp("e-mgr", tenant="tTer"))
    await emp_repo.add(_emp("e-bk", tenant="tTer"))
    await repo_session.commit()

    terr_repo = TerritoryRepositoryImpl(repo_session, tenant)
    full_rule = TerritoryAssignment(
        tenant_id=TenantId("tTer"),
        employee_id=EmployeeId("e-t-1"),
        priority=3,
        effective_from=_NOW,
        countries=["US", "DE"],
        product_categories=["hinges", "locks"],
        need_categories=["hardware"],
        buyer_types=["wholesaler", "retailer"],
        languages=["en", "zh"],
        manager_id=EmployeeId("e-mgr"),
        backup_employee_id=EmployeeId("e-bk"),
        effective_until=datetime(2999, 1, 1, tzinfo=UTC),
    )
    await terr_repo.add(full_rule)
    await terr_repo.add(_rule("e-t-2", tenant="tTer", priority=2, buyer="wholesaler"))
    await repo_session.commit()

    rules = await terr_repo.list_by_employee(tenant, EmployeeId("e-t-1"))
    assert len(rules) == 1
    r = rules[0]
    assert r.employee_id == EmployeeId("e-t-1")
    assert r.priority == 3
    assert r.effective_from == _NOW
    assert r.effective_until == datetime(2999, 1, 1, tzinfo=UTC)
    assert r.countries == ["US", "DE"]
    assert r.product_categories == ["hinges", "locks"]
    assert r.need_categories == ["hardware"]
    assert r.buyer_types == ["wholesaler", "retailer"]
    assert r.languages == ["en", "zh"]
    assert r.manager_id == EmployeeId("e-mgr")
    assert r.backup_employee_id == EmployeeId("e-bk")
    assert len(await terr_repo.list_by_employee(tenant, EmployeeId("e-t-2"))) == 1


async def test_territory_list_matching_dimensions_priority_tiebreak(repo_session: AsyncSession) -> None:
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    TerritoryRepositoryImpl = _load("TerritoryRepositoryImpl")
    tenant = TenantId("tMatch")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    for e in ("e-1", "e-2", "e-3"):
        await emp_repo.add(_emp(e, tenant="tMatch"))
    await repo_session.commit()

    terr_repo = TerritoryRepositoryImpl(repo_session, tenant)
    await terr_repo.add(_rule("e-2", tenant="tMatch", priority=1, need="hinges"))
    await terr_repo.add(_rule("e-3", tenant="tMatch", priority=2, need="hinges", buyer="wholesaler"))
    await terr_repo.add(_rule("e-1", tenant="tMatch", priority=1, need="hinges"))  # 同 priority → employee_id tie-break
    await repo_session.commit()

    matched = await terr_repo.list_matching(tenant, "US", "hinges", None)
    # priority 升序 + employee_id 升序稳定排序
    assert [r.employee_id for r in matched] == [EmployeeId("e-1"), EmployeeId("e-2"), EmployeeId("e-3")]
    # need+buyer 维度同时匹配
    both = await terr_repo.list_matching(tenant, "US", "hinges", "wholesaler")
    assert [r.employee_id for r in both] == [EmployeeId("e-3")]


async def test_territory_list_matching_effective_window(repo_session: AsyncSession) -> None:
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    TerritoryRepositoryImpl = _load("TerritoryRepositoryImpl")
    tenant = TenantId("tWindow")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("e-w-1", tenant="tWindow"))
    await repo_session.commit()

    terr_repo = TerritoryRepositoryImpl(repo_session, tenant)
    await terr_repo.add(_rule("e-w-1", tenant="tWindow", priority=1, need="hinges"))
    await terr_repo.add(_rule("e-w-1", tenant="tWindow", priority=2, need="hinges", effective_from=datetime(2999, 1, 1, tzinfo=UTC)))  # 未来 → 排除
    await terr_repo.add(_rule("e-w-1", tenant="tWindow", priority=3, need="hinges", effective_until=datetime(2020, 1, 1, tzinfo=UTC)))  # 已过期 → 排除
    await repo_session.commit()

    matched = await terr_repo.list_matching(tenant, "US", "hinges", None)
    assert [r.employee_id for r in matched] == [EmployeeId("e-w-1")]
    assert len(matched) == 1  # 只有第一条在有效窗口内


async def test_territory_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    TerritoryRepositoryImpl = _load("TerritoryRepositoryImpl")
    terr_repo = TerritoryRepositoryImpl(repo_session, TenantId("tBound"))
    assert await terr_repo.list_matching(TenantId("tOther"), "US", None, None) == []
    assert await terr_repo.list_by_employee(TenantId("tOther"), EmployeeId("e-x")) == []
    with pytest.raises(ValueError):
        await terr_repo.add(_rule("e-x", tenant="tOther"))


# --- 仓储：Ownership ---------------------------------------------------------------


async def test_try_lock_and_get(repo_session: AsyncSession) -> None:
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tLock")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("e-l-1", tenant="tLock"))
    await emp_repo.add(_emp("e-l-2", tenant="tLock"))  # 第二次锁的 owner 必须先存在，避免 FK 误判为唯一冲突
    await repo_session.commit()

    repo = OwnershipRepositoryImpl(repo_session, tenant)
    assert await repo.try_lock(_lock("e-l-1", tenant="tLock")) is True
    await repo_session.commit()
    current = await repo.get(tenant, ProspectAccountId("acct-1"))
    assert current is not None and current.owner == EmployeeId("e-l-1")
    assert current.locked_by_rule == "3"

    # 同账户再锁 → 唯一约束（tenant+account）→ False，既有锁不变
    assert await repo.try_lock(_lock("e-l-2", tenant="tLock")) is False
    current = await repo.get(tenant, ProspectAccountId("acct-1"))
    assert current is not None and current.owner == EmployeeId("e-l-1")


async def test_try_lock_concurrent_unique(db_url: str) -> None:
    """两个独立会话并发 try_lock 同一账户：各自 try_lock 后立即 commit/rollback。

    断言 ``try_lock`` 返回值恰好 ``[False, True]``（UNIQUE(tenant_id, account_id)
    真正支撑并发），且最终仅一行。不把 commit 冲突冒充 try_lock 语义。
    """
    from infra.db.session import create_engine_from

    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tLockRace")
    engine = create_engine_from(db_url)
    session_a = AsyncSession(bind=engine, expire_on_commit=False)
    session_b = AsyncSession(bind=engine, expire_on_commit=False)
    try:
        emp_repo = EmployeeRepositoryImpl(session_a, tenant)
        await emp_repo.add(_emp("e-lr-1", tenant="tLockRace"))
        await session_a.commit()
        repo_a = OwnershipRepositoryImpl(session_a, tenant)
        repo_b = OwnershipRepositoryImpl(session_b, tenant)

        async def _attempt(session: AsyncSession, repo, lock: OwnershipLock) -> bool:
            ok = await repo.try_lock(lock)
            if ok:
                await session.commit()
            else:
                await session.rollback()
            return ok

        results = await asyncio.gather(
            _attempt(session_a, repo_a, _lock("e-lr-1", tenant="tLockRace", account="acct-race")),
            _attempt(session_b, repo_b, _lock("e-lr-1", tenant="tLockRace", account="acct-race")),
        )
        assert sorted(results) == [False, True], f"try_lock 应恰好一个成功一个冲突：{results}"
        # 最终仅一行（UNIQUE 兜底，无并发双锁）
        async with engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM ownership_locks WHERE tenant_id = :t AND account_id = :a"),
                    {"t": "tLockRace", "a": "acct-race"},
                )
            ).scalar_one()
        assert count == 1
    finally:
        await session_a.close()
        await session_b.close()
        await engine.dispose()


async def test_replace_updates_lock_and_appends_history_atomically(repo_session: AsyncSession) -> None:
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tReplace")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("e-o-1", tenant="tReplace"))
    await emp_repo.add(_emp("e-n-1", tenant="tReplace"))
    await repo_session.commit()

    repo = OwnershipRepositoryImpl(repo_session, tenant)
    assert await repo.try_lock(_lock("e-o-1", tenant="tReplace")) is True
    await repo_session.commit()

    new_lock = _lock("e-n-1", tenant="tReplace")
    transfer = _transfer("tr-rep-1", from_owner="e-o-1", to_owner="e-n-1", reason="move", tenant="tReplace")
    await repo.replace(tenant, new_lock, transfer)
    await repo_session.commit()  # 调用方事务提交

    current = await repo.get(tenant, ProspectAccountId("acct-1"))
    assert current is not None and current.owner == EmployeeId("e-n-1")
    history = await repo.list_transfers(tenant, ProspectAccountId("acct-1"))
    assert len(history) == 1
    assert history[0].from_owner == EmployeeId("e-o-1")
    assert history[0].to_owner == EmployeeId("e-n-1")
    assert history[0].reason == "move"


async def test_replace_failure_no_partial_commit(repo_session: AsyncSession) -> None:
    """replace 同一事务：历史 INSERT 被 CHECK 拒绝 → 整体回滚，锁不变、历史不写。"""
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tNoPartial")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    # employee_id 是全局 PK：用与 tReplace 测试不同的 id，避免跨测试冲突
    await emp_repo.add(_emp("e-op-1", tenant="tNoPartial"))
    await emp_repo.add(_emp("e-np-1", tenant="tNoPartial"))
    await repo_session.commit()

    repo = OwnershipRepositoryImpl(repo_session, tenant)
    assert await repo.try_lock(_lock("e-op-1", tenant="tNoPartial")) is True
    await repo_session.commit()

    bad_transfer = _transfer("tr-bad", from_owner="e-op-1", to_owner="e-np-1", reason="   ", tenant="tNoPartial")  # 空白 reason → CHECK 拒绝
    new_lock = _lock("e-np-1", tenant="tNoPartial")
    with pytest.raises(IntegrityError):
        await repo.replace(tenant, new_lock, bad_transfer)
        await repo_session.commit()
    await repo_session.rollback()

    current = await repo.get(tenant, ProspectAccountId("acct-1"))
    assert current is not None and current.owner == EmployeeId("e-op-1")  # 锁未变
    assert await repo.list_transfers(tenant, ProspectAccountId("acct-1")) == []  # 历史未写


async def test_replace_consistency_checks_fail_closed(repo_session: AsyncSession) -> None:
    """replace 三方租户一致 + account 一致：任一不符 → ValueError 且无副作用。"""
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tConsist")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("e-cs-1", tenant="tConsist"))
    await emp_repo.add(_emp("e-cs-2", tenant="tConsist"))
    await repo_session.commit()

    repo = OwnershipRepositoryImpl(repo_session, tenant)
    assert await repo.try_lock(_lock("e-cs-1", tenant="tConsist")) is True
    await repo_session.commit()

    valid_transfer = _transfer("tr-cs-1", from_owner="e-cs-1", to_owner="e-cs-2", reason="move", tenant="tConsist")
    # new_lock 是 foreign tenant → ValueError
    with pytest.raises(ValueError):
        await repo.replace(tenant, _lock("e-cs-2", tenant="tForeign"), valid_transfer)
    # transfer 是 foreign tenant → ValueError
    with pytest.raises(ValueError):
        await repo.replace(
            tenant,
            _lock("e-cs-2", tenant="tConsist"),
            _transfer("tr-cs-2", from_owner="e-cs-1", to_owner="e-cs-2", reason="move", tenant="tForeign"),
        )
    # account 不一致 → ValueError
    with pytest.raises(ValueError):
        await repo.replace(tenant, _lock("e-cs-2", tenant="tConsist", account="acct-x"), valid_transfer)
    # 无副作用：锁未变、历史未写
    current = await repo.get(tenant, ProspectAccountId("acct-1"))
    assert current is not None and current.owner == EmployeeId("e-cs-1")
    assert await repo.list_transfers(tenant, ProspectAccountId("acct-1")) == []


async def test_try_lock_illegal_owner_propagates_integrity_error(repo_session: AsyncSession) -> None:
    """同租户不存在 owner：owner 复合 FK 错误必须传播，不伪装成锁冲突 False。"""
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    repo = OwnershipRepositoryImpl(repo_session, TenantId("tOwn"))
    with pytest.raises(IntegrityError):
        await repo.try_lock(_lock("e-does-not-exist", tenant="tOwn"))
    await repo_session.rollback()


async def test_replace_history_authenticity_fail_closed(repo_session: AsyncSession) -> None:
    """replace 历史真实性：owner/to 一致 + from_owner 必须等于当前锁 owner。

    过期/伪造的 from_owner（含 None）→ OwnershipConflictError，且锁/历史不变；
    new_lock.owner != transfer.to_owner → ValueError，无副作用。UPDATE 仅在
    rowcount==1（WHERE 含当前 owner）后才追加历史。
    """
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tAuth")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("ea-1", tenant="tAuth"))
    await emp_repo.add(_emp("ea-2", tenant="tAuth"))
    await emp_repo.add(_emp("ea-3", tenant="tAuth"))
    await repo_session.commit()

    repo = OwnershipRepositoryImpl(repo_session, tenant)
    # 三个账户各上一把当前 owner=ea-1 的锁
    for acct in ("acct-1", "acct-2", "acct-3"):
        assert await repo.try_lock(_lock("ea-1", tenant="tAuth", account=acct)) is True
        await repo_session.commit()

    # 1) new_lock.owner(ea-3) != transfer.to_owner(ea-2) → ValueError 且无副作用
    with pytest.raises(ValueError):
        await repo.replace(
            tenant,
            _lock("ea-3", tenant="tAuth", account="acct-1"),
            _transfer("tr-auth-1", from_owner="ea-1", to_owner="ea-2", reason="m", tenant="tAuth", account="acct-1"),
        )
    # 2) 当前 owner 是 ea-1，from_owner 却写 ea-2（已过期）→ OwnershipConflictError
    with pytest.raises(OwnershipConflictError):
        await repo.replace(
            tenant,
            _lock("ea-3", tenant="tAuth", account="acct-2"),
            _transfer("tr-auth-2", from_owner="ea-2", to_owner="ea-3", reason="m", tenant="tAuth", account="acct-2"),
        )
    # 3) from_owner=None 无法匹配当前非空 owner → OwnershipConflictError
    with pytest.raises(OwnershipConflictError):
        await repo.replace(
            tenant,
            _lock("ea-2", tenant="tAuth", account="acct-3"),
            _transfer("tr-auth-3", from_owner=None, to_owner="ea-2", reason="m", tenant="tAuth", account="acct-3"),
        )
    # 全部无副作用：锁未变（仍 ea-1）、历史未写
    for acct in ("acct-1", "acct-2", "acct-3"):
        current = await repo.get(tenant, ProspectAccountId(acct))
        assert current is not None and current.owner == EmployeeId("ea-1"), acct
        assert await repo.list_transfers(tenant, ProspectAccountId(acct)) == [], acct


async def test_list_transfers_time_ascending(repo_session: AsyncSession) -> None:
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    EmployeeRepositoryImpl = _load("EmployeeRepositoryImpl")
    tenant = TenantId("tHist")
    emp_repo = EmployeeRepositoryImpl(repo_session, tenant)
    await emp_repo.add(_emp("e-h-1", tenant="tHist"))
    await emp_repo.add(_emp("e-h-2", tenant="tHist"))
    await emp_repo.add(_emp("e-h-3", tenant="tHist"))
    await repo_session.commit()

    repo = OwnershipRepositoryImpl(repo_session, tenant)
    assert await repo.try_lock(_lock("e-h-1", tenant="tHist")) is True
    await repo_session.commit()

    earlier = _transfer("tr-h-1", from_owner="e-h-1", to_owner="e-h-2", reason="first", at=_NOW, tenant="tHist")
    later = _transfer("tr-h-2", from_owner="e-h-2", to_owner="e-h-3", reason="second", at=datetime(2026, 8, 9, 12, 0, 0, tzinfo=UTC), tenant="tHist")
    await repo.replace(tenant, _lock("e-h-2", tenant="tHist"), earlier)
    await repo.replace(tenant, _lock("e-h-3", tenant="tHist"), later)
    await repo_session.commit()

    history = await repo.list_transfers(tenant, ProspectAccountId("acct-1"))
    assert [t.transfer_id for t in history] == ["tr-h-1", "tr-h-2"]  # 时间升序


async def test_ownership_tenant_binding_mismatch(repo_session: AsyncSession) -> None:
    OwnershipRepositoryImpl = _load("OwnershipRepositoryImpl")
    repo = OwnershipRepositoryImpl(repo_session, TenantId("tBound"))
    assert await repo.get(TenantId("tOther"), ProspectAccountId("acct-1")) is None
    assert await repo.list_transfers(TenantId("tOther"), ProspectAccountId("acct-1")) == []
    assert await repo.try_lock(_lock("e-x", tenant="tOther")) is False
    with pytest.raises(ValueError):
        await repo.replace(TenantId("tOther"), _lock("e-x", tenant="tOther"), _transfer("tr-x", from_owner=None, to_owner="e-x", reason="r"))
