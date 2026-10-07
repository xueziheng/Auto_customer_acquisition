"""本人邮箱迁移使用管理连接，业务配置持久保存派生角色；无真实数据库。"""

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from infra.db.tenant_security import tenant_database_role
from infra.pilot.config import PilotError, exclusive_profile_lock
from infra.pilot.mailbox_config import MailboxConfig
from shared.schemas.identifiers import new_id
from tests.unit.test_mailbox_launcher import fake_docker


@pytest.fixture(autouse=True)
def restore_logging():
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)


def _runtime(config):
    return MailboxConfig.model_validate(
        {
            **config.model_dump(mode="python"),
            "database_username": tenant_database_role(config.tenant_id),
            "database_runtime_password": SecretStr(
                "synthetic-runtime-password-only-" + "x" * 32
            ),
        }
    )


def test_private_roundtrip_and_binding_preserve_separate_runtime_identity(tmp_path):
    path = tmp_path / "mailbox/custom-private.json"
    bootstrap = MailboxConfig.create(path)
    assert bootstrap.database_username == "mailbox"
    assert bootstrap.database_runtime_password is None
    configured = _runtime(bootstrap)
    with exclusive_profile_lock(path.parent):
        configured.write(path)
    loaded = MailboxConfig.read(path)
    assert loaded == configured
    runtime, migration = map(
        make_url,
        (
            loaded.database_url.get_secret_value(),
            loaded.migration_database_url.get_secret_value(),
        ),
    )
    assert runtime.username == tenant_database_role(bootstrap.tenant_id)
    assert migration.username == "mailbox"
    assert runtime.password == loaded.database_runtime_password.get_secret_value()
    assert migration.password == bootstrap.db_password.get_secret_value()
    assert runtime.password != migration.password
    assert loaded.database_runtime_password.get_secret_value() not in repr(loaded)
    assert path.stat().st_mode & 0o777 == 0o600
    bound = MailboxConfig.bind(
        path,
        employee_id=str(new_id("emp")),
        email="synthetic@example.test",
        credentials_file=tmp_path / "unread.json",
    )
    assert bound.database_username == loaded.database_username
    assert bound.database_runtime_password == loaded.database_runtime_password
    assert MailboxConfig.read(path) == bound


@pytest.mark.parametrize(
    "case", ["admin_password", "foreign", "foreign_tenant", "missing", "short", "long"]
)
def test_runtime_configuration_rejects_wrong_role_or_password(tmp_path, case):
    bootstrap = MailboxConfig.create(tmp_path / "mailbox/config.json")
    role = tenant_database_role(bootstrap.tenant_id)
    password = SecretStr("synthetic-" + "x" * 40)
    if case == "admin_password":
        role = "mailbox"
    elif case == "foreign":
        role = "owner"
    elif case == "foreign_tenant":
        role = tenant_database_role(str(new_id("tn")))
    elif case == "missing":
        password = None
    elif case == "short":
        password = SecretStr("short")
    elif case == "long":
        password = SecretStr("x" * 129)
    with pytest.raises(ValidationError):
        MailboxConfig.model_validate(
            {
                **bootstrap.model_dump(mode="python"),
                "database_username": role,
                "database_runtime_password": password,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_explicit_migration_uses_admin_then_configures_same_private_file(
    tmp_path, monkeypatch, configured
):
    from scripts import configure_enterprise_database, run_mailbox

    path = tmp_path / "mailbox/custom-private.json"
    initial = MailboxConfig.create(path)
    if configured:
        _runtime(initial).write(path)
    database = run_mailbox.MailboxDatabase(path, client=fake_docker())
    database.provision()
    events = []
    usernames = []

    class Connection:
        async def execute(self, query):
            return None

        async def scalar(self, query):
            return True

    class Engine:
        @asynccontextmanager
        async def connect(self):
            yield Connection()

        async def dispose(self):
            events.append("dispose")

    def engine(url):
        usernames.append(make_url(url).username)
        return Engine()

    async def launch(*args, **kwargs):
        assert args[-2:] == ("upgrade", "head")
        assert make_url(kwargs["env"]["DATABASE_URL"]).username == "mailbox"
        events.append("migration")

        async def wait():
            return 0

        return SimpleNamespace(wait=wait)

    async def schema(engine):
        events.append("schema")

    async def configure(directory, config, *, config_path=None):
        assert directory == path.parent and config_path == path
        assert isinstance(config, MailboxConfig)
        assert events[-1] == "dispose" and "schema" in events
        events.append("configure")
        _runtime(config).write(config_path)
        return "configured"

    monkeypatch.setattr(run_mailbox, "create_engine_from", engine)
    monkeypatch.setattr(run_mailbox.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(run_mailbox, "assert_database_schema_current", schema)
    monkeypatch.setattr(configure_enterprise_database, "configure", configure)
    await database.migrate()
    assert usernames == ["mailbox", "mailbox"]
    assert events[-1] == "configure"
    assert database.config == MailboxConfig.read(path)
    assert database.config.database_username == tenant_database_role(initial.tenant_id)


@pytest.mark.asyncio
async def test_failed_migration_never_configures_runtime_role(tmp_path, monkeypatch):
    from scripts import configure_enterprise_database, run_mailbox

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    database = run_mailbox.MailboxDatabase(path, client=fake_docker())
    database.provision()
    before = path.read_bytes()

    class Engine:
        @asynccontextmanager
        async def connect(self):
            async def execute(query):
                return None

            async def scalar(query):
                return True

            yield SimpleNamespace(execute=execute, scalar=scalar)

        async def dispose(self):
            pass

    async def launch(*args, **kwargs):
        async def wait():
            return 2

        return SimpleNamespace(wait=wait)

    async def forbidden(*args, **kwargs):
        pytest.fail("迁移失败不得配置运行角色")

    monkeypatch.setattr(run_mailbox, "create_engine_from", lambda url: Engine())
    monkeypatch.setattr(run_mailbox.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(configure_enterprise_database, "configure", forbidden)
    with pytest.raises(PilotError, match="migration_failed"):
        await database.migrate()
    assert path.read_bytes() == before


def test_start_never_migrates_or_provisions_runtime_role(tmp_path, monkeypatch):
    from scripts import configure_enterprise_database, mailbox_supervisor, run_mailbox

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    before = path.read_bytes()
    started = []

    def forbidden(*args, **kwargs):
        pytest.fail("普通启动不得迁移或配置数据库角色")

    monkeypatch.setattr(run_mailbox, "MailboxDatabase", forbidden)
    monkeypatch.setattr(configure_enterprise_database, "configure", forbidden)
    monkeypatch.setattr(
        mailbox_supervisor, "launch", lambda *args: started.append(args)
    )
    assert run_mailbox.main(["start", "--profile", str(path)]) == 0
    assert len(started) == 1
    assert path.read_bytes() == before
