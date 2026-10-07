"""企业数据库身份必须贯穿 profile 和运行时，不泄漏私有连接材料。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from infra.pilot.config import PilotConfig
from tests.unit.test_pilot_profile import make_config


def test_profile_uses_only_its_own_database_role(tmp_path):
    from infra.db.tenant_security import tenant_database_role
    profile = make_config(tmp_path)
    payload = profile.model_dump()
    payload["database_username"] = tenant_database_role(profile.tenant_id)
    payload["database_runtime_password"] = SecretStr("synthetic-runtime-password-" * 2)
    isolated = PilotConfig.model_validate(payload)
    assert make_url(isolated.database_url.get_secret_value()).username == payload["database_username"]
    assert make_url(isolated.runtime_environment()["DATABASE_URL"]).username == payload["database_username"]
    assert make_url(isolated.migration_database_url.get_secret_value()).username == "pilot"
    assert make_url(isolated.migration_database_url.get_secret_value()).password != make_url(isolated.database_url.get_secret_value()).password
    isolated.write(tmp_path / "profile" / "config.json")
    restored = PilotConfig.read(tmp_path / "profile" / "config.json")
    assert restored.database_runtime_password == isolated.database_runtime_password
    payload["database_username"] = tenant_database_role("another_enterprise")
    with pytest.raises(ValidationError):
        PilotConfig.model_validate(payload)


@pytest.mark.parametrize("username", ["postgres", "admin", "", "pilot "])
def test_profile_refuses_arbitrary_database_identity(tmp_path, username):
    payload = make_config(tmp_path).model_dump()
    payload["database_username"] = username
    with pytest.raises(ValidationError):
        PilotConfig.model_validate(payload)


async def test_tenant_role_always_runs_database_security_gate(monkeypatch):
    from infra.db import runtime_scope
    probe = AsyncMock()
    monkeypatch.setattr(runtime_scope, "assert_tenant_database_isolation", probe)
    engine = SimpleNamespace(url=make_url("postgresql+asyncpg://tradeos_t_abc@localhost/test"))
    await runtime_scope.verify_runtime_database_scope(engine, "tenant_a")
    probe.assert_awaited_once_with(engine, "tenant_a")
    probe.side_effect = RuntimeError("isolation_rejected")
    with pytest.raises(RuntimeError, match="isolation_rejected"):
        await runtime_scope.verify_runtime_database_scope(engine, "tenant_a")


@pytest.mark.parametrize("username", ["pilot", "postgres", "tradeos_t_abc", None])
async def test_every_runtime_identity_must_pass_security_gate(monkeypatch, username):
    from infra.db import runtime_scope
    from shared.errors import TenantIsolationViolation
    probe = AsyncMock(side_effect=TenantIsolationViolation("运行数据库隔离检查失败"))
    monkeypatch.setattr(runtime_scope, "assert_tenant_database_isolation", probe)
    engine = SimpleNamespace(url=SimpleNamespace(username=username))
    with pytest.raises(TenantIsolationViolation):
        await runtime_scope.verify_runtime_database_scope(engine, "tenant_a")
    probe.assert_awaited_once_with(engine, "tenant_a")
