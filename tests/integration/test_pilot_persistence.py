"""仅本测试新 owner 的真实持久卷、停启与冷恢复演练。"""

import asyncio
import hashlib
import json
import logging
import secrets
import shutil

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.authentication.service import PostgresAuthentication
from infra.db.session import create_engine_from
from infra.db.tables import EmployeeRow
from shared.authentication import AuthenticationDenied
from shared.schemas.identifiers import EmployeeId, TenantId, new_id
from tests.unit.test_pilot_profile import synthetic_policy


@pytest.fixture
def owned_profiles(tmp_path):
    logging.getLogger("botocore").setLevel(logging.CRITICAL)
    logging.getLogger("boto3").setLevel(logging.CRITICAL)
    profiles = []
    yield tmp_path, profiles
    errors = 0
    for profile in reversed(profiles):
        try:
            profile.stop()
            profile.reload()
            for kind in profile.config.storage:
                container = profile.verify(kind)
                container.remove()
            for identity in profile.config.storage.values():
                volume = profile.client.volumes.get(identity.volume_name)
                if (
                    volume.attrs["Labels"].get("tradeos.pilot.owner")
                    != profile.config.owner
                ):
                    errors += 1
                else:
                    volume.remove()
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            errors += 1
        finally:
            profile.client.close()
    assert errors == 0, "OWNED_TEST_RESOURCE_CLEANUP_FAILED"


def initialized(tmp_path, profiles):
    from infra.pilot.config import PilotConfig
    from infra.pilot.resources import PilotProfile

    directory = tmp_path / "source"
    PilotConfig.create(directory, synthetic_policy(tmp_path / "policy.json"))
    profile = PilotProfile(directory)
    profiles.append(profile)
    profile.provision_storage()
    profile.migrate()
    return profile


async def write_employee_session(config):
    engine = create_engine_from(config.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    employee = EmployeeId(new_id("emp"))
    auth = PostgresAuthentication(factory, TenantId(config.tenant_id))
    try:
        async with factory.begin() as session:
            session.add(
                EmployeeRow(
                    tenant_id=config.tenant_id,
                    employee_id=employee,
                    user_id=new_id("usr"),
                    name="合成持久化标记",
                    role="sales",
                    is_active=True,
                )
            )
        await auth.create_account(
            "synthetic-pilot", SecretStr(secrets.token_urlsafe(24)), employee
        )
        # 密码只在进程中随机生成和消费，不使用断言回显。
        password = SecretStr(secrets.token_urlsafe(24))
        await auth.reset_password("synthetic-pilot", password)
        issued = await auth.login("synthetic-pilot", password)
        return employee, issued.token
    finally:
        await engine.dispose()


async def check_employee_session(config, employee, token, revoked):
    engine = create_engine_from(config.database_url.get_secret_value())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = await session.scalar(
                select(EmployeeRow).where(
                    EmployeeRow.tenant_id == config.tenant_id,
                    EmployeeRow.employee_id == employee,
                )
            )
            assert row is not None
            assert row.name == "合成持久化标记"
        auth = PostgresAuthentication(factory, TenantId(config.tenant_id))
        if revoked:
            with pytest.raises(AuthenticationDenied):
                await auth.authenticate(token)
        else:
            principal = await auth.authenticate(token)
            assert principal.employee_id == employee
    finally:
        await engine.dispose()


@pytest.mark.parametrize("object_root_uid", [0, 1001])
def test_real_stop_restart_and_cold_restore(owned_profiles, object_root_uid):
    from infra.pilot.backup import backup_profile, restore_profile
    from infra.pilot.config import PilotConfig, PilotError

    tmp_path, profiles = owned_profiles
    source = initialized(tmp_path, profiles)
    # 旧部署的卷根可属于 root；恢复必须保留归档属主，不能改成容器运行用户。
    outcome = source.verify("objects").exec_run(
        ["chown", f"{object_root_uid}:0", "/bitnami/minio/data"], user="0"
    )
    assert outcome.exit_code == 0
    employee, token = asyncio.run(write_employee_session(source.config))
    payload = secrets.token_bytes(4097)
    expected_hash = hashlib.sha256(payload).hexdigest()
    with source.object_client() as client:
        client.put_object(
            Bucket=source.config.bucket, Key="synthetic/marker.bin", Body=payload
        )
    with pytest.raises(PilotError, match="profile_not_stopped"):
        backup_profile(source.path, tmp_path / "running-backup")
    original_tenant, original_bucket = source.config.tenant_id, source.config.bucket
    original_ids = {k: v.container_id for k, v in source.config.storage.items()}
    source.stop()
    assert source.status()["storage"] == "stopped"
    source.start_storage()
    assert source.config.database_port > 0 and source.config.object_port > 0
    current_config = PilotConfig.read(source.path / "config.json")
    assert current_config.database_port == source.config.database_port
    asyncio.run(check_employee_session(current_config, employee, token, False))
    with source.object_client() as client:
        actual_hash = hashlib.sha256(
            client.get_object(Bucket=source.config.bucket, Key="synthetic/marker.bin")[
                "Body"
            ].read()
        ).hexdigest()
    assert actual_hash == expected_hash
    assert {k: v.container_id for k, v in source.config.storage.items()} == original_ids
    source.stop()
    backup = tmp_path / "backup"
    backup_profile(source.path, backup)
    source_config_hash = hashlib.sha256(
        (source.path / "config.json").read_bytes()
    ).hexdigest()
    from infra.pilot.resources import PilotProfile

    try:
        target = restore_profile(backup, tmp_path / "restored")
    except Exception:
        if (tmp_path / "restored/config.json").exists():
            profiles.append(PilotProfile(tmp_path / "restored"))
        raise
    profiles.append(target)
    assert target.status()["storage"] == "stopped"
    assert target.config.owner != source.config.owner
    assert target.config.tenant_id == original_tenant
    assert target.config.bucket == original_bucket
    assert target.config.api_port != source.config.api_port
    assert (
        hashlib.sha256((source.path / "config.json").read_bytes()).hexdigest()
        == source_config_hash
    )
    target.start_storage()
    ownership = target.verify("objects").exec_run(
        ["stat", "-c", "%u:%g", "/bitnami/minio/data"]
    )
    assert ownership.exit_code == 0
    assert ownership.output.strip() == f"{object_root_uid}:0".encode()
    asyncio.run(check_employee_session(target.config, employee, token, True))
    with target.object_client() as client:
        actual_hash = hashlib.sha256(
            client.get_object(Bucket=target.config.bucket, Key="synthetic/marker.bin")[
                "Body"
            ].read()
        ).hexdigest()
    assert actual_hash == expected_hash
    target.stop()
    with pytest.raises(PilotError, match="profile_exists"):
        restore_profile(backup, target.path)


def test_real_identity_tampering_refused_without_stopping_storage(owned_profiles):
    from infra.pilot.config import PilotError

    tmp_path, profiles = owned_profiles
    source = initialized(tmp_path, profiles)
    original = source.config
    identity = original.storage["database"]
    source.config = original.model_copy(
        update={
            "storage": {
                **original.storage,
                "database": identity.model_copy(update={"container_id": "0" * 64}),
            }
        }
    )
    with pytest.raises(PilotError, match="resource_identity_invalid"):
        source.verify("database")
    source.config = original.model_copy(
        update={
            "storage": {
                **original.storage,
                "database": identity.model_copy(
                    update={"volume_name": original.storage["objects"].volume_name}
                ),
            }
        }
    )
    with pytest.raises(PilotError, match="resource_identity_invalid"):
        source.verify("database")
    source.config = original
    assert source.status()["storage"] == "running"
    source.stop()


def test_corrupt_backup_rejected_before_creating_target(owned_profiles):
    from infra.pilot.backup import backup_profile, restore_profile
    from infra.pilot.config import PilotError

    tmp_path, profiles = owned_profiles
    source = initialized(tmp_path, profiles)
    source.stop()
    original = tmp_path / "backup"
    backup_profile(source.path, original)
    for corruption in ("checksum", "missing", "version", "symlink", "extra"):
        copy = tmp_path / corruption
        shutil.copytree(original, copy)
        copy.chmod(0o700)
        if corruption == "checksum":
            with (copy / "objects.tar").open("ab") as stream:
                stream.write(b"invalid")
        elif corruption == "missing":
            (copy / "database.tar").unlink()
        elif corruption == "version":
            manifest = json.loads((copy / "manifest.json").read_bytes())
            manifest["version"] = 99
            (copy / "manifest.json").write_text(json.dumps(manifest))
        elif corruption == "symlink":
            (copy / "objects.tar").unlink()
            (copy / "objects.tar").symlink_to(original / "objects.tar")
        else:
            (copy / "extra").write_bytes(b"unknown")
        with pytest.raises(PilotError, match="backup_invalid"):
            restore_profile(copy, tmp_path / (corruption + "-target"))
        assert not (tmp_path / (corruption + "-target")).exists()
    assert source.status()["storage"] == "stopped"


def test_live_owned_process_blocks_backup_and_stop_until_exact_cleanup(owned_profiles):
    import os
    import sys

    from infra.controlled.resources import OwnedProcess
    from infra.pilot.backup import backup_profile
    from infra.pilot.config import PilotError, exclusive_profile_lock
    from infra.pilot.resources import ROOT

    tmp_path, profiles = owned_profiles
    source = initialized(tmp_path, profiles)
    source.stop()
    process = OwnedProcess.start(
        "api",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=ROOT,
        environ={"PATH": os.defpath},
    )
    try:
        with exclusive_profile_lock(source.path):
            source.publish_processes_locked(
                supervisor=None,
                processes=[process],
                status="running",
                reason="applications_ready",
            )
        with pytest.raises(PilotError, match="profile_apps_running"):
            backup_profile(source.path, tmp_path / "busy-backup")
        with pytest.raises(PilotError, match="profile_apps_running"):
            source.stop()
        assert process.verified()
    finally:
        process.stop()
    assert source.status()["applications"] == "stopped"
    source.stop()
    assert source.status()["applications"] == "stopped"


def test_schema_check_never_migrates_and_failed_restore_cannot_start(
    owned_profiles, monkeypatch
):
    from infra.pilot.backup import backup_profile, restore_profile
    from infra.pilot.config import PilotError
    from infra.pilot.resources import PilotProfile

    tmp_path, profiles = owned_profiles
    source = initialized(tmp_path, profiles)
    container = source.verify("database")
    outcome = container.exec_run(
        [
            "psql",
            "-U",
            "pilot",
            "-d",
            "pilot",
            "-c",
            "UPDATE alembic_version SET version_num = 'stale-synthetic'",
        ]
    )
    assert outcome.exit_code == 0
    with pytest.raises(PilotError, match="schema_not_current"):
        source.check_schema()
    outcome = container.exec_run(
        [
            "psql",
            "-U",
            "pilot",
            "-d",
            "pilot",
            "-At",
            "-c",
            "SELECT version_num FROM alembic_version",
        ]
    )
    unchanged = outcome.output.strip() == b"stale-synthetic"
    assert unchanged, "SCHEMA_WAS_AUTOMATICALLY_CHANGED"
    source.stop()
    backup_profile(source.path, tmp_path / "stale-backup")
    with pytest.raises(PilotError, match="restore_failed"):
        restore_profile(tmp_path / "stale-backup", tmp_path / "failed-restore")
    failed = PilotProfile(tmp_path / "failed-restore")
    profiles.append(failed)
    assert failed.config.restore_state == "failed"
    assert failed.status()["storage"] == "stopped"
    with pytest.raises(PilotError, match="restore_incomplete"):
        failed.start_storage()


@pytest.mark.parametrize("failure", ["interrupt", "diagnostic_io"])
def test_restore_failure_after_storage_start_always_stops_and_closes(
    owned_profiles, monkeypatch, capsys, failure
):
    from infra.pilot.backup import backup_profile
    from infra.pilot.config import PilotError
    from infra.pilot.resources import PilotProfile
    from scripts.run_web_pilot import main

    tmp_path, profiles = owned_profiles
    source = initialized(tmp_path, profiles)
    source.stop()
    backup_profile(source.path, tmp_path / "failure-backup")
    source_hash = hashlib.sha256((source.path / "config.json").read_bytes()).hexdigest()
    target_path = tmp_path / "failure-target"
    observed = {"started": False, "closed": False}
    original_check = PilotProfile.check_schema_locked

    def fail_after_storage_started(profile):
        if profile.path != target_path:
            return original_check(profile)
        observed["started"] = all(
            container.status == "running" for container in profile.verify_all().values()
        )
        original_close = profile.client.close

        def close():
            observed["closed"] = True
            original_close()

        patch.setattr(profile.client, "close", close)
        if failure == "interrupt":
            raise KeyboardInterrupt

        def fail_save():
            raise OSError("synthetic_private_write_failure")

        from infra.pilot import backup as backup_module
        from infra.pilot import resources as resource_module

        def fail_diagnostic_write(path, payload):
            raise OSError("synthetic_private_write_failure")

        patch.setattr(profile, "save", fail_save)
        patch.setattr(resource_module, "private_write", fail_diagnostic_write)
        patch.setattr(backup_module, "private_write", fail_diagnostic_write)
        profile.save()

    with monkeypatch.context() as patch:
        patch.setattr(PilotProfile, "check_schema_locked", fail_after_storage_started)
        try:
            result = main(
                [
                    "restore",
                    "--backup",
                    str(tmp_path / "failure-backup"),
                    "--profile",
                    str(target_path),
                ]
            )
        except BaseException:  # noqa: BLE001 测试拦住中断以检查真实资源是否已停止
            result = None
        finally:
            if (target_path / "config.json").exists():
                target = PilotProfile(target_path)
                profiles.append(target)
    assert observed["started"], "RESTORE_FAILURE_INJECTION_NOT_REACHED"
    assert target.status()["storage"] == "stopped", (
        "RESTORE_FAILURE_LEFT_STORAGE_RUNNING"
    )
    assert observed["closed"], "RESTORE_FAILURE_LEFT_CLIENT_OPEN"
    assert result == 2, "RESTORE_FAILURE_ESCAPED_CLI"
    expected_reason = (
        "restore_interrupted" if failure == "interrupt" else "restore_failed"
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "reason": expected_reason,
    }
    assert target.config.restore_state in {"pending", "failed"}
    with pytest.raises(PilotError, match="restore_incomplete"):
        target.start_storage()
    assert source.status()["storage"] == "stopped"
    assert (
        hashlib.sha256((source.path / "config.json").read_bytes()).hexdigest()
        == source_hash
    )
