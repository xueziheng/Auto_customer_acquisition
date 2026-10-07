"""配置 CLI 的授权、幂等与失败清理流程；数据库隔离效果由真实 PG 测试负责。"""
from __future__ import annotations

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from infra.db.tenant_security import tenant_database_role
from infra.pilot.config import PilotConfig, exclusive_profile_lock


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[2] / "scripts/configure_enterprise_database.py"
    spec = importlib.util.spec_from_file_location("configure_enterprise_database_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def profile(tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "handoff_policy": {
            "sla_seconds": 7200, "backlog_threshold": 7, "t1_seconds": 40, "t2_seconds": 90,
        },
        "scoring_policy": {
            "version": "synthetic-configuration", "currency": "USD",
            "value_band_boundaries": ["250.00"],
            "bucket_map": {str(k): "low" for k in range(1, 8)},
        },
    }))
    path = tmp_path / "profile"
    config = PilotConfig.create(path, policy)
    return path, config


@pytest.fixture
def fake_database(cli, monkeypatch):
    events = []
    state = SimpleNamespace(role_oid=None, verification_failure=False, create_only=None)

    class Engine:
        def __init__(self, kind):
            self.kind = kind

        @asynccontextmanager
        async def begin(self):
            events.append("transaction")
            yield object()

        async def dispose(self):
            events.append("dispose_" + self.kind)

    def engine(url):
        # URL只在可信代码中检查，没有断言/日志包含 URL 或 SecretStr 的原文。
        from sqlalchemy.engine import make_url
        username = make_url(url.get_secret_value()).username
        kind = "admin" if username in {"pilot", "mailbox"} else "runtime"
        events.append("engine_" + kind)
        return Engine(kind)

    async def schema(admin):
        events.append("schema")

    async def role_oid(connection, role):
        return state.role_oid

    async def provision(connection, tenant, password, *, create_only=False):
        assert isinstance(password, SecretStr)
        assert len(password.get_secret_value()) >= 32
        assert create_only
        state.create_only = create_only
        events.append("provision")
        state.role_oid = 12345

    async def verify(runtime, tenant):
        events.append("verify")
        if state.verification_failure:
            raise RuntimeError("opaque_test_error_must_not_escape")

    async def cleanup(admin, role, oid):
        events.append("cleanup")
        assert state.role_oid == oid == 12345
        state.role_oid = None

    monkeypatch.setattr(cli, "_engine", engine)
    monkeypatch.setattr(cli, "assert_database_schema_current", schema)
    monkeypatch.setattr(cli, "_role_oid", role_oid)
    monkeypatch.setattr(cli, "provision_tenant_role", provision)
    monkeypatch.setattr(cli, "assert_tenant_database_isolation", verify)
    monkeypatch.setattr(cli, "_cleanup_created_role", cleanup)
    return state, events


def test_parser_never_echoes_unrecognized_sensitive_argument(cli, capsys):
    marker = "synthetic-do-not-echo"
    assert cli.main(["--profile", "/unused", "--password", marker]) == 1
    output = capsys.readouterr()
    assert marker not in output.out + output.err
    assert json.loads(output.err)["reason"] == "arguments_invalid"


def test_dry_run_does_not_connect_generate_password_or_write(cli, profile, monkeypatch, capsys):
    path, _ = profile
    before = (path / "config.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run 不得连接数据库或生成凭证")

    monkeypatch.setattr(cli, "_engine", forbidden)
    monkeypatch.setattr(cli.secrets, "token_urlsafe", forbidden)
    assert cli.main(["--profile", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert (path / "config.json").read_bytes() == before


def test_new_role_is_verified_before_private_configuration_commit(
    cli, profile, fake_database, monkeypatch,
):
    path, original = profile
    state, events = fake_database
    real_write = PilotConfig.write

    def recording_write(config, destination):
        assert "verify" in events
        events.append("write")
        real_write(config, destination)

    monkeypatch.setattr(PilotConfig, "write", recording_write)
    assert cli.main(["--profile", str(path), "--apply"]) == 0
    updated = PilotConfig.read(path / "config.json")
    assert updated.database_username == tenant_database_role(original.tenant_id)
    assert updated.database_runtime_password is not None
    assert updated.secrets == original.secrets
    assert updated.tenant_id == original.tenant_id
    assert events.index("schema") < events.index("provision") < events.index("verify") < events.index("write")
    assert "cleanup" not in events
    assert state.create_only is True
    assert (path / "config.json").stat().st_mode & 0o777 == 0o600


def test_configured_role_only_verifies_and_never_rotates(
    cli, profile, fake_database, monkeypatch, capsys,
):
    path, original = profile
    _, events = fake_database
    configured = PilotConfig.model_validate({
        **original.model_dump(mode="python"),
        "database_username": tenant_database_role(original.tenant_id),
        "database_runtime_password": SecretStr("synthetic-runtime-password-" + "x" * 32),
    })
    with exclusive_profile_lock(path):
        configured.write(path / "config.json")
    before = (path / "config.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("已配置的角色不得旋转密码或重写文件")

    monkeypatch.setattr(cli.secrets, "token_urlsafe", forbidden)
    monkeypatch.setattr(PilotConfig, "write", forbidden)
    assert cli.main(["--profile", str(path), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_configured"
    assert "verify" in events and "provision" not in events and "cleanup" not in events
    assert (path / "config.json").read_bytes() == before


def test_unassociated_existing_role_is_neither_rotated_nor_removed(
    cli, profile, fake_database, capsys,
):
    path, _ = profile
    state, events = fake_database
    state.role_oid = 87654
    before = (path / "config.json").read_bytes()
    assert cli.main(["--profile", str(path), "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "role_exists_unassociated"
    assert "provision" not in events and "cleanup" not in events
    assert state.role_oid == 87654
    assert (path / "config.json").read_bytes() == before


def test_verification_failure_cleans_only_new_role_and_preserves_profile(
    cli, profile, fake_database, capsys,
):
    path, _ = profile
    state, events = fake_database
    state.verification_failure = True
    before = (path / "config.json").read_bytes()
    assert cli.main(["--profile", str(path), "--apply"]) == 1
    output = capsys.readouterr()
    assert "opaque_test_error_must_not_escape" not in output.err
    assert json.loads(output.err)["reason"] == "new_role_verification_failed"
    assert "cleanup" in events and state.role_oid is None
    assert (path / "config.json").read_bytes() == before


def test_write_failure_after_replace_restores_profile_before_dropping_new_role(
    cli, profile, fake_database, monkeypatch, capsys,
):
    path, original = profile
    state, events = fake_database
    before = (path / "config.json").read_bytes()
    real_write = PilotConfig.write

    def failing_after_replace(config, destination):
        real_write(config, destination)
        if config.database_username != "pilot":
            raise OSError("synthetic fsync failure")

    monkeypatch.setattr(PilotConfig, "write", failing_after_replace)
    assert cli.main(["--profile", str(path), "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "profile_write_failed"
    assert PilotConfig.read(path / "config.json") == original
    assert (path / "config.json").read_bytes() == before
    assert "cleanup" in events and state.role_oid is None


def test_uncertain_profile_restore_preserves_referenced_new_role(
    cli, profile, fake_database, monkeypatch, capsys,
):
    path, _ = profile
    state, events = fake_database
    real_write = PilotConfig.write

    def failing_restore(config, destination):
        if config.database_username != "pilot":
            real_write(config, destination)
        raise OSError("synthetic unavailable storage")

    monkeypatch.setattr(PilotConfig, "write", failing_restore)
    assert cli.main(["--profile", str(path), "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "profile_restore_uncertain"
    assert "cleanup" not in events and state.role_oid == 12345
    assert PilotConfig.read(path / "config.json").database_username != "pilot"


@pytest.mark.asyncio
async def test_cleanup_refuses_same_name_role_with_different_oid(cli, monkeypatch):
    executed = []

    class Connection:
        async def execute(self, statement):
            executed.append(statement)

    class Engine:
        @asynccontextmanager
        async def begin(self):
            yield Connection()

    async def different_oid(connection, role):
        return 99999

    monkeypatch.setattr(cli, "_role_oid", different_oid)
    with pytest.raises(cli.ConfigurationFailure, match="role_cleanup_identity_changed"):
        await cli._cleanup_created_role(Engine(), tenant_database_role("tn_synthetic"), 12345)
    assert not executed



@pytest.fixture
def mailbox_profile(tmp_path):
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox" / "personal-mail.json"
    config = MailboxConfig.create(path)
    return path, config


def test_profile_and_mailbox_config_flags_are_mutually_exclusive(cli, capsys):
    assert cli.main(["--profile", "/unused", "--mailbox-config", "/unused/mail.json"]) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "arguments_invalid"


def test_mailbox_uses_explicit_file_and_same_idempotent_configuration_flow(
    cli, mailbox_profile, fake_database, monkeypatch, capsys,
):
    from infra.pilot.mailbox_config import MailboxConfig

    path, original = mailbox_profile
    _, events = fake_database
    assert cli.main(["--mailbox-config", str(path), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configured"
    updated = MailboxConfig.read(path)
    assert updated.database_username == tenant_database_role(original.tenant_id)
    assert updated.db_password == original.db_password
    assert updated.bindings == original.bindings
    assert not (path.parent / "config.json").exists()
    before = path.read_bytes()
    events.clear()

    def forbidden(*args, **kwargs):
        pytest.fail("邮箱运行配置幂等重试不得旋转密码或写文件")

    monkeypatch.setattr(cli.secrets, "token_urlsafe", forbidden)
    monkeypatch.setattr(MailboxConfig, "write", forbidden)
    assert cli.main(["--mailbox-config", str(path), "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_configured"
    assert "verify" in events and "provision" not in events
    assert path.read_bytes() == before


def test_mailbox_write_failure_restores_original_mailbox_type(
    cli, mailbox_profile, fake_database, monkeypatch, capsys,
):
    from infra.pilot.mailbox_config import MailboxConfig

    path, original = mailbox_profile
    state, events = fake_database
    before = path.read_bytes()
    real_write = MailboxConfig.write

    def failing_after_replace(config, destination):
        real_write(config, destination)
        if config.database_username != "mailbox":
            raise OSError("synthetic mailbox write failure")

    monkeypatch.setattr(MailboxConfig, "write", failing_after_replace)
    assert cli.main(["--mailbox-config", str(path), "--apply"]) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "profile_write_failed"
    assert MailboxConfig.read(path) == original
    assert path.read_bytes() == before
    assert "cleanup" in events and state.role_oid is None
    assert not (path.parent / "config.json").exists()


@pytest.mark.asyncio
async def test_mailbox_requires_explicit_config_path_before_database_access(
    cli, mailbox_profile, monkeypatch,
):
    path, config = mailbox_profile

    def forbidden(*args, **kwargs):
        pytest.fail("配置路径不明确时不得连接数据库")

    monkeypatch.setattr(cli, "_engine", forbidden)
    with pytest.raises(cli.ConfigurationFailure, match="configuration_path_required"):
        await cli.configure(path.parent, config)


@pytest.mark.asyncio
async def test_configuration_path_cannot_escape_locked_directory(cli, profile, monkeypatch):
    path, config = profile

    def forbidden(*args, **kwargs):
        pytest.fail("配置路径越界时不得连接数据库")

    monkeypatch.setattr(cli, "_engine", forbidden)
    with pytest.raises(cli.ConfigurationFailure, match="configuration_path_invalid"):
        await cli.configure(path, config, config_path=path.parent / "another.json")
