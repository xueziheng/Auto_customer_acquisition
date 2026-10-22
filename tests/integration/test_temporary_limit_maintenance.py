"""一次性运维工具：隔离 PG、合成私有文件；不访问生产。"""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.assistant.schemas import AssistantActor, ModelSettingsUpdate
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.tables import ModelConfigurationHeadRow
from infra.pilot.config import private_read, private_write
from infra.standalone.settings import load_model_settings
from shared.schemas.identifiers import TenantId
from tests.integration.test_model_usage import limits
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)


def module():
    path = (
        Path(__file__).resolve().parents[2]
        / ".superpowers/sdd/2026-10-13-candidate-inventory/temporary-limit-version.py"
    )
    spec = importlib.util.spec_from_file_location("temporary_limit_version", path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


@pytest.fixture
async def environment(unit_engine, tmp_path):
    import json

    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    settings_path = root / "model.json"
    value = {
        "provider": "deepseek",
        "model": "test-model",
        "configuration_version": "old-v1",
        "limits": limits(tenant_calls=20, employee_calls=20).model_dump(),
        "model_data_export_enabled": True,
        "secret_ref": "SYNTHETIC",
        "api_key_file": "/synthetic/unused/key",
        "api_control_tenant": "control",
        "api_business_tenant": "tenant",
        "api_verified_at": datetime.now(UTC).isoformat(),
        "model_catalog_directory": "/synthetic/unused/catalog",
        "codex_job_root": "/synthetic/unused/jobs",
    }
    private_write(settings_path, json.dumps(value).encode())
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    repo = SqlModelConfigurationRepository(factory, None)
    config = load_model_settings(settings_path)
    await repo.initialize(
        TenantId("tenant"),
        config.configuration_version,
        config.model,
        config.limits,
        True,
    )
    for process in ("api", "scheduler"):
        await repo.register_process(TenantId("tenant"), process, "old-v1", process)
    actor = AssistantActor(tenant_id="tenant", user_id="operator", employee_id="emp")
    return factory, repo, actor, root, settings_path


async def change_via_existing_repository(repo, actor, config):
    await repo.save(
        actor,
        ModelSettingsUpdate(
            expected_version=config.configuration_version,
            model=config.model,
            limits=config.limits.model_copy(
                update={"tenant_calls": 26, "employee_calls": 26}
            ),
        ),
    )
    return (await repo.get(actor.tenant_id)).configuration_version


@pytest.mark.parametrize("fault", [None, "apply_file", "restore_head", "restore_file"])
async def test_apply_restore_reentrant_across_durable_failure_points(
    environment, fault
):
    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    original = private_read(settings_path)
    await tool.prepare()
    await tool.prepare()
    new = await change_via_existing_repository(
        repo, actor, load_model_settings(settings_path)
    )
    # 显式服务停止之外，还需无进程登记；模拟正常 shutdown。
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    if fault == "apply_file":
        tool.checkpoint = lambda stage: (
            (_ for _ in ()).throw(RuntimeError("PRIVATE")) if stage == fault else None
        )
        with pytest.raises(RuntimeError):
            await tool.apply(new, stopped=True)
        tool.checkpoint = lambda stage: None
    await tool.apply(new, stopped=True)
    current = load_model_settings(settings_path)
    assert current.configuration_version == new and current.limits.tenant_calls == 26
    assert current.api_verified_at is None and current.api_key_file == Path(
        "/synthetic/unused/key"
    )
    assert (await repo.get(actor.tenant_id)).runtime.verified_at is None
    if fault and fault.startswith("restore"):
        tool.checkpoint = lambda stage: (
            (_ for _ in ()).throw(RuntimeError("PRIVATE")) if stage == fault else None
        )
        with pytest.raises(RuntimeError):
            await tool.restore(new, stopped=True)
        tool.checkpoint = lambda stage: None
    await tool.restore(new, stopped=True)
    await tool.restore(new, stopped=True)
    assert private_read(settings_path) == original
    async with factory() as db:
        head = (
            await db.scalars(
                select(ModelConfigurationHeadRow).where(
                    ModelConfigurationHeadRow.tenant_id == actor.tenant_id
                )
            )
        ).one()
        assert head.version == "old-v1"


async def test_foreign_head_file_change_live_process_and_missing_stop_fail_closed(
    environment,
):
    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    await tool.prepare()
    config = load_model_settings(settings_path)
    new = await change_via_existing_repository(repo, actor, config)
    with pytest.raises(ValueError):
        await tool.apply(new, stopped=False)
    with pytest.raises(ValueError):
        await tool.apply(new, stopped=True)
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    await repo.save(
        actor,
        ModelSettingsUpdate(
            expected_version=new, model=config.model, limits=config.limits
        ),
    )
    with pytest.raises(ValueError):
        await tool.apply(new, stopped=True)
    with pytest.raises(ValueError):
        await tool.restore(new, stopped=True)
    assert load_model_settings(settings_path).configuration_version == "old-v1"


async def test_twenty_old_calls_stay_counted_after_new_policy_and_restore(environment):
    from datetime import timedelta

    from infra.db.model_usage import SqlModelUsageRepository
    from shared.schemas.model_invocation import ModelUsage
    from tests.integration.test_model_usage import identity

    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    old = load_model_settings(settings_path)
    usage = SqlModelUsageRepository(factory)
    now = datetime.now(UTC)

    async def reserve(config, capability="inventory_discovery"):
        item = identity(
            actor.tenant_id,
            employee=actor.employee_id,
            version=config.configuration_version,
        ).model_copy(update={"capability": capability})
        result = await usage.reserve(
            item,
            "a" * 64,
            config.limits,
            now,
            model=config.model,
            owner_id="test",
            lease_expires_at=now + timedelta(seconds=30),
        )
        if result.outcome == "reserved":
            await usage.mark_dispatched(actor.tenant_id, result.invocation_id)
            await usage.finish(
                actor.tenant_id,
                result.invocation_id,
                ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
                "succeeded",
            )
        return result.outcome

    for _ in range(20):
        assert await reserve(old) == "reserved"
    assert await reserve(old) == "limited"
    await tool.prepare()
    new = await change_via_existing_repository(repo, actor, old)
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    await tool.apply(new, stopped=True)
    config = load_model_settings(settings_path)
    # 受控端口证明探测与业务共用计数，不伪造真实 probe 验证状态。
    assert await reserve(config, "model_probe") == "reserved"
    for _ in range(5):
        assert await reserve(config) == "reserved"
    assert await reserve(config) == "limited"
    await tool.restore(new, stopped=True)
    assert await reserve(load_model_settings(settings_path)) == "limited"
    assert (await repo.get(actor.tenant_id)).runtime.verified_at is None


async def test_tampered_file_lock_and_live_job_fail_closed(environment):
    from domains.prospecting.inventory import InventoryQuery
    from infra.db.prospect_inventory import (
        PostgresInventoryService,
        PostgresPublicCatalog,
    )
    from infra.pilot.config import PilotError, exclusive_profile_lock

    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    await tool.prepare()
    new = await change_via_existing_repository(
        repo, actor, load_model_settings(settings_path)
    )
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    original = private_read(settings_path)
    private_write(settings_path, original + b" ")
    with pytest.raises(ValueError):
        await tool.apply(new, stopped=True)
    private_write(settings_path, original)
    with exclusive_profile_lock(root), pytest.raises(PilotError):
        await tool.apply(new, stopped=True)
    await tool.apply(new, stopped=True)
    catalog = PostgresPublicCatalog(factory, TenantId("control"))
    service = PostgresInventoryService(factory, actor.tenant_id, catalog)
    await service.request(
        actor.tenant_id,
        actor.employee_id,
        InventoryQuery(product_tags=("synthetic",)),
        1,
        "new-policy-job",
    )
    with pytest.raises(ValueError):
        await tool.restore(new, stopped=True)
    assert load_model_settings(settings_path).configuration_version == new


async def test_queued_probe_prevents_restore_without_cancellation(environment):
    from tool_gateway.fingerprint import HmacFingerprintProvider

    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    await tool.prepare()
    new = await change_via_existing_repository(
        repo, actor, load_model_settings(settings_path)
    )
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    await tool.apply(new, stopped=True)
    probe_repo = SqlModelConfigurationRepository(
        factory, HmacFingerprintProvider("v1", b"x" * 32)
    )
    turn = await probe_repo.probe(actor, new, "pending-probe")
    with pytest.raises(ValueError):
        await tool.restore(new, stopped=True)
    assert (await probe_repo.probe(actor, new, "pending-probe")).turn_id == turn.turn_id


async def test_policy_switch_requires_new_probe_and_restore_reuses_old_verified_policy(
    environment,
):
    from domains.assistant.service_impl import ModelConfigurationServiceImpl
    from infra.standalone.settings import deployment_verification
    from shared.schemas.model_invocation import ModelGenerationError

    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    original = load_model_settings(settings_path)
    await tool.prepare()
    new = await change_via_existing_repository(repo, actor, original)
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    await tool.apply(new, stopped=True)
    config = load_model_settings(settings_path)

    class Authority:
        async def check(self, actor):
            pass

        async def require_admin(self, actor):
            pass

    for process in ("api", "scheduler"):
        await repo.register_process(actor.tenant_id, process, new, process)
    new_repo = SqlModelConfigurationRepository(
        factory, None, deployment_verification=deployment_verification(config)
    )
    service = ModelConfigurationServiceImpl(
        new_repo, Authority(), now=lambda: datetime.now(UTC)
    )
    assert (await service.get_public(actor)).status == "unverified"
    with pytest.raises(ModelGenerationError):
        await service.authorize(actor, new, probe=False)
    await service.authorize(actor, new, probe=True)
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    await tool.restore(new, stopped=True)
    for process in ("api", "scheduler"):
        await repo.register_process(
            actor.tenant_id, process, original.configuration_version, process
        )
    old_repo = SqlModelConfigurationRepository(
        factory,
        None,
        deployment_verification=deployment_verification(
            load_model_settings(settings_path)
        ),
    )
    old_service = ModelConfigurationServiceImpl(
        old_repo, Authority(), now=lambda: datetime.now(UTC)
    )
    assert (await old_service.get_public(actor)).status == "verified"


async def test_actor_binding_and_file_write_failure_recovery(environment, monkeypatch):
    factory, repo, actor, root, settings_path = environment
    m = module()
    tool = m.Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    await tool.prepare()
    other = m.Maintenance(
        factory, actor.tenant_id, "other", settings_path, root / "operation"
    )
    with pytest.raises(ValueError):
        await other.prepare()
    new = await change_via_existing_repository(
        repo, actor, load_model_settings(settings_path)
    )
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    write = m.private_write

    def fail_settings(path, value):
        if path == settings_path:
            raise OSError("PRIVATE")
        write(path, value)

    monkeypatch.setattr(m, "private_write", fail_settings)
    with pytest.raises(OSError):
        await tool.apply(new, stopped=True)
    monkeypatch.setattr(m, "private_write", write)
    await tool.restore(new, stopped=True)
    assert load_model_settings(settings_path).configuration_version == "old-v1"


async def test_non_catalog_and_unapproved_limit_change_are_refused(environment):
    import json

    factory, repo, actor, root, settings_path = environment
    tool = module().Maintenance(
        factory, actor.tenant_id, actor.user_id, settings_path, root / "operation"
    )
    original = private_read(settings_path)
    value = json.loads(original)
    value.pop("model_catalog_directory")
    private_write(settings_path, json.dumps(value).encode())
    with pytest.raises(ValueError):
        await tool.prepare()
    private_write(settings_path, original)
    await tool.prepare()
    old = load_model_settings(settings_path)
    await repo.save(
        actor,
        ModelSettingsUpdate(
            expected_version=old.configuration_version,
            model=old.model,
            limits=old.limits.model_copy(
                update={"tenant_calls": 27, "employee_calls": 26}
            ),
        ),
    )
    new = (await repo.get(actor.tenant_id)).configuration_version
    for process in ("api", "scheduler"):
        await repo.unregister_process(actor.tenant_id, process, process)
    with pytest.raises(ValueError):
        await tool.apply(new, stopped=True)
    assert private_read(settings_path) == original


def test_cli_failure_never_outputs_exception_or_private_arguments(monkeypatch, capsys):
    m = module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tool",
            "prepare",
            "--profile",
            "/PRIVATE/profile",
            "--settings",
            "/PRIVATE/settings",
            "--state-dir",
            "/PRIVATE/state",
            "--actor-user-id",
            "PRIVATE",
        ],
    )
    monkeypatch.setattr(
        m.PilotConfig,
        "read",
        lambda path: (_ for _ in ()).throw(ValueError("PRIVATE_TOKEN")),
    )
    assert m.main() == 2
    output = capsys.readouterr()
    assert output.out == "" and output.err == "maintenance_refused_or_incomplete\n"
