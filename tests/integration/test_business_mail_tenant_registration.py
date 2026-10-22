"""真实临时PG、真实最小租户登录角色，在冷子进程核验0085启动门禁。"""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import text

from infra.db.tenant_security import provision_tenant_role, tenant_database_role
from shared.schemas.identifiers import new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = """import asyncio,os,sys
from sqlalchemy.ext.asyncio import create_async_engine
from infra.db.runtime_scope import verify_runtime_database_scope
from shared.errors import TenantIsolationViolation
assert 'infra.db.business_mail_tables' not in sys.modules
async def main():
    engine=create_async_engine(os.environ['REGISTRATION_TEST_DATABASE_URL'],hide_parameters=True)
    try:
        await verify_runtime_database_scope(engine,os.environ['REGISTRATION_TEST_TENANT'])
    except TenantIsolationViolation:
        print('tenant_isolation_rejected')
        return 1
    finally:
        await engine.dispose()
    print('tenant_isolation_passed')
    return 0
raise SystemExit(asyncio.run(main()))
"""


async def test_migrated_database_passes_runtime_scope_in_cold_process(unit_engine):
    # 测试准备模拟已运行API的映射；真正门禁在未导入任何业务模块的独立子进程执行。
    from infra.db.business_mail_tables import BusinessMailboxRow

    assert BusinessMailboxRow.__tablename__ == "business_mailboxes"
    tenant = new_id("tn")
    password = SecretStr(secrets.token_urlsafe(32))
    role = tenant_database_role(tenant)
    try:
        async with unit_engine.begin() as db:
            await provision_tenant_role(db, tenant, password, create_only=True)

        def check():
            return subprocess.run(
                [sys.executable, "-c", SCRIPT],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(ROOT),
                    "REGISTRATION_TEST_TENANT": tenant,
                    "REGISTRATION_TEST_DATABASE_URL": unit_engine.url.set(
                        username=role, password=password.get_secret_value()
                    ).render_as_string(hide_password=False),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=40,
            )

        result = await asyncio.to_thread(check)
        assert result.returncode == 0, (
            "冷进程真实租户启动门禁失败（不回显连接和原始异常）"
        )
        assert result.stdout.strip() == "tenant_isolation_passed"
    finally:
        async with unit_engine.begin() as db:
            exists = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=:role)"
                ),
                {"role": role},
            )
            if exists:
                await db.execute(text(f'DROP OWNED BY "{role}"'))
                await db.execute(text(f'DROP ROLE "{role}"'))
