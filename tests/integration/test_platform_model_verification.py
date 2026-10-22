"""部署验证只有精确租户与版本可用，仍需要两个进程心跳。"""
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.model_configuration import SqlModelConfigurationRepository
from tests.integration.test_assistant_recovery import Provider
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.integration.test_standalone_model_runtime import configured
from tool_gateway.fingerprint import HmacFingerprintProvider


@pytest.mark.parametrize("binding", ["match", "foreign_tenant", "stale_version"])
async def test_platform_probe_does_not_authorize_another_tenant_or_version(unit_engine, binding):
    owner, _, _, _, _, service, config, _ = await configured(unit_engine, Provider())
    stamp = datetime.now(UTC)
    repo = SqlModelConfigurationRepository(async_sessionmaker(unit_engine, expire_on_commit=False), HmacFingerprintProvider("test-v1", b"t" * 32),
        deployment_verification=(owner.tenant_id if binding != "foreign_tenant" else "foreign", config.configuration_version if binding != "stale_version" else "stale", stamp))
    view = service.view(await repo.get(owner.tenant_id))
    assert view.status == ("verified" if binding == "match" else "unverified")
    await repo.unregister_process(owner.tenant_id, "scheduler", "instance-scheduler")
    assert service.view(await repo.get(owner.tenant_id)).status != "verified"
