"""数据库角色边界的离线契约；实际数据库权限另由 PostgreSQL 集成测试验证。"""
from __future__ import annotations

import hashlib

import pytest
from pydantic import SecretStr

from infra.db.tenant_security import (
    provision_tenant_role,
    tenant_database_role,
)
from shared.errors import TenantIsolationViolation


def test_role_is_deterministic_bounded_and_distinguishes_enterprises() -> None:
    tenant = "tn_企业甲"
    expected = "tradeos_t_" + hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:48]
    assert tenant_database_role(tenant) == expected
    assert len(expected) == 58
    assert tenant_database_role("tn_企业乙") != expected
    assert tenant_database_role("a" * 1000).isascii()


@pytest.mark.parametrize("tenant", ["", " ", " x", "x ", "\x00", None, 3])
def test_role_rejects_noncanonical_tenant(tenant: str) -> None:
    with pytest.raises(TenantIsolationViolation):
        tenant_database_role(tenant)


@pytest.mark.asyncio
@pytest.mark.parametrize("password", [SecretStr(""), SecretStr("\x00"), "not-secret"])
async def test_provision_rejects_invalid_password_before_database_access(password: SecretStr) -> None:
    class NeverConnection:
        async def execute(self, *args: object, **kwargs: object) -> None:
            pytest.fail("非法输入不得触碰数据库")
    with pytest.raises(TenantIsolationViolation):
        await provision_tenant_role(NeverConnection(), "tn_test", password)


def test_fresh_process_covers_all_mapped_tables_including_private_mailboxes() -> None:
    """独立隔离入口必须注册全部分文件表；真实迁移与策略由 PG 测试验证。"""
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = """
import importlib
import pkgutil
import infra.db
from infra.db.tenant_security import _tenant_table_names
from infra.db.tables import Base

tables = _tenant_table_names()
assert {
    'mailbox_accounts', 'mailbox_messages', 'enterprise_knowledge_documents',
    'candidate_allocations', 'business_mail_messages', 'business_mail_followups',
    'conversation_reply_work_updates', 'qq_reply_trials',
} <= tables
# 先取得启动入口的注册结果，再独立导入全部分文件表；漏导入新表必须失败。
for module in pkgutil.iter_modules(infra.db.__path__):
    if module.name == 'tables' or module.name.endswith('_tables'):
        importlib.import_module(f'infra.db.{module.name}')
all_mapped = {
    table.name for table in Base.metadata.tables.values() if 'tenant_id' in table.c
}
assert tables == all_mapped, f'unregistered tenant tables: {all_mapped - tables}'
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True, check=False, timeout=20,
    )
    assert result.returncode == 0, "独立进程 ORM 注册不完整"



@pytest.mark.asyncio
async def test_create_only_provision_never_rotates_an_existing_role(monkeypatch) -> None:
    from infra.db import tenant_security

    statements = []

    async def checked_tables(connection):
        return []

    class ExistingResult:
        def mappings(self):
            return self

        def one_or_none(self):
            return {"rolname": tenant_database_role("tn_existing")}

    class Connection:
        async def execute(self, statement, parameters=None):
            statements.append(str(statement))
            return ExistingResult()

    monkeypatch.setattr(tenant_security, "_check_tables", checked_tables)
    with pytest.raises(TenantIsolationViolation):
        await provision_tenant_role(
            Connection(), "tn_existing", SecretStr("synthetic-runtime-secret-1234567890"),
            create_only=True,
        )
    assert not any("CREATE " in statement or "ALTER " in statement for statement in statements)



class _PrivilegeRows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [],
    [{"oid": 101, "dml": True, "unsafe": False}],
    [{"oid": 101, "dml": True, "unsafe": False}, {"oid": 303, "dml": True, "unsafe": False}],
    [{"oid": 101, "dml": True, "unsafe": False}, {"oid": 101, "dml": True, "unsafe": False}],
    [{"oid": 101, "dml": True, "unsafe": False}, {"oid": 202, "dml": False, "unsafe": False}],
    [{"oid": 101, "dml": True, "unsafe": False}, {"oid": 202, "dml": True, "unsafe": True}],
    [{"oid": 101, "dml": True, "unsafe": False}, {"oid": 202, "dml": True, "unsafe": False},
     {"oid": 303, "dml": True, "unsafe": False}],
])
async def test_batch_privilege_check_rejects_incomplete_or_unsafe_results(rows) -> None:
    from infra.db.tenant_security import _check_table_privileges

    class Connection:
        async def execute(self, statement, parameters):
            return _PrivilegeRows(rows)

    with pytest.raises(TenantIsolationViolation):
        await _check_table_privileges(
            Connection(), tenant_database_role("tn_batch"), [{"oid": 101}, {"oid": 202}],
        )


@pytest.mark.asyncio
async def test_batch_privilege_check_checks_all_tables_in_one_round_trip() -> None:
    from infra.db.tenant_security import _check_table_privileges

    calls = []

    class Connection:
        async def execute(self, statement, parameters):
            calls.append(parameters)
            return _PrivilegeRows([
                {"oid": 202, "dml": True, "unsafe": False},
                {"oid": 101, "dml": True, "unsafe": False},
            ])

    await _check_table_privileges(
        Connection(), tenant_database_role("tn_batch"), [{"oid": 101}, {"oid": 202}],
    )
    assert len(calls) == 1
    assert set(calls[0]["oids"]) == {101, 202}
