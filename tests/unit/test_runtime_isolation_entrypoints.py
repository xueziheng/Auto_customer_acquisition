"""真实运行入口不得跳过隔离；只替换数据库传输，不替换被测隔离门禁。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from infra.db.tenant_security import tenant_database_role
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import TenantId

TENANT = TenantId("tn_01K2C5R6J7ABCDEFGHJKMNPQRS")


class RejectingDatabase:
    """模拟真实角色查询或连接故障；后续业务 IO 一旦发生即使测试失败。"""

    def __init__(self, tenant_id: str, failure: str):
        self.tenant_id = tenant_id
        self.failure = failure
        username = "owner" if failure == "owner" else tenant_database_role(tenant_id)
        self.url = make_url("postgresql+asyncpg://" + username + "@unused.invalid/test")
        self.disposed = False
        self.probed = False

    @asynccontextmanager
    async def begin(self):
        self.probed = True
        if self.failure == "probe_error":
            raise RuntimeError("private-database-driver-detail")
        yield self

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        if sql.startswith("SET LOCAL search_path"):
            return None
        if "pg_catalog.pg_roles" not in sql:
            pytest.fail("隔离拒绝后不得读取业务数据或继续装配")
        role = tenant_database_role(self.tenant_id)
        assert parameters == {"role": role}
        row = {
            "rolname": role,
            "rolcanlogin": True,
            "current_role": "owner",
            "login_role": "owner",
        }
        row.update(
            {
                key: False
                for key in (
                    "rolsuper",
                    "rolinherit",
                    "rolcreaterole",
                    "rolcreatedb",
                    "rolreplication",
                    "rolbypassrls",
                    "membership",
                    "owns_objects",
                    "schema_create",
                    "database_create",
                )
            }
        )
        return SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: row)
        )

    def connect(self):
        pytest.fail("数据库隔离失败后不得进入工作线程或健康服务")

    async def dispose(self):
        self.disposed = True


async def schema_is_current(_engine):
    """本测试只检查隔离门禁；schema 探针由各自集成测试覆盖。"""


async def forbidden_after_scope(*args, **kwargs):
    pytest.fail("隔离失败后不得进入后续生命周期")


def forbidden_server(*args, **kwargs):
    pytest.fail("隔离失败后不得启动健康监听")


def assert_rejected_safely(error, engine, expected_type=TenantIsolationViolation):
    assert isinstance(error, expected_type)
    assert "private-database-driver-detail" not in str(error)
    assert engine.probed, "运行入口未执行真实隔离门禁"
    assert engine.disposed, "启动失败未清理数据库资源"


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_api_entrypoint_refuses_unisolated_database_before_startup(
    monkeypatch, failure
):
    from apps.api import runtime
    from apps.api.runtime_config import Phase1RuntimeSettings
    from tests.unit.test_api_runtime_config import _VALID_ENV

    engine = RejectingDatabase(TENANT, failure)
    monkeypatch.setattr(runtime, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(
        runtime,
        "build_phase1_dependencies",
        lambda *a, **kw: SimpleNamespace(
            quotation=None,
            email_inbound=None,
            model_lifecycle=None,
            object_store_lifecycle=None,
        ),
    )
    monkeypatch.setattr(runtime, "assert_database_schema_current", schema_is_current)
    monkeypatch.setattr(
        runtime, "assert_handoff_reminder_compatibility", forbidden_after_scope
    )
    settings = Phase1RuntimeSettings.from_environ(
        {**_VALID_ENV, "TRADEOS_TENANT_ID": str(TENANT)}
    )
    app = runtime.create_runtime_app_from_settings(
        settings, secret_resolver=object(), object_store_settings=object()
    )
    with pytest.raises(TenantIsolationViolation) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("未隔离数据库被允许提供 API")
    assert_rejected_safely(caught.value, engine)


def scheduler_config():
    from apps.scheduler_worker.config import SchedulerWorkerConfig
    from tests.unit.test_scheduler_sourcing_runtime import _base_environ

    environ = {**_base_environ(), "TRADEOS_TENANT_ID": str(TENANT)}
    return environ, SchedulerWorkerConfig.from_environ(environ)


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_scheduler_factory_refuses_unisolated_database_before_health_or_work(
    monkeypatch, failure
):
    from apps.scheduler_worker import runtime

    engine = RejectingDatabase(TENANT, failure)
    environ, _ = scheduler_config()
    monkeypatch.setattr(runtime, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(runtime, "assert_database_schema_current", schema_is_current)
    monkeypatch.setattr(
        runtime, "assert_handoff_reminder_compatibility", forbidden_after_scope
    )
    factory = runtime.SchedulerRuntimeFactory(
        environ, bootstrap=object(), health_server_factory=forbidden_server
    )
    with pytest.raises(TenantIsolationViolation) as caught:
        async with factory():
            pytest.fail("未隔离数据库被允许推进 scheduler")
    assert_rejected_safely(caught.value, engine)


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_configured_scheduler_entrypoint_also_refuses_unisolated_database(
    monkeypatch, failure
):
    from apps.scheduler_worker import runtime

    engine = RejectingDatabase(TENANT, failure)
    _, config = scheduler_config()
    monkeypatch.setattr(runtime, "assert_database_schema_current", schema_is_current)
    monkeypatch.setattr(
        runtime, "assert_handoff_reminder_compatibility", forbidden_after_scope
    )
    with pytest.raises(TenantIsolationViolation) as caught:
        async with runtime.configured_scheduler_runtime(
            config,
            engine=engine,
            outbox=object(),
            workflow=object(),
            notification_handler=forbidden_after_scope,
        ):
            pytest.fail("显式配置入口绕过了数据库隔离")
    assert_rejected_safely(caught.value, engine)


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_notification_entrypoint_refuses_unisolated_database_before_claim_or_delivery(
    monkeypatch, failure
):
    from apps.notification_worker import runtime
    from tests.unit.test_notification_worker import _config

    config = _config()
    engine = RejectingDatabase(str(config.tenant_id), failure)
    monkeypatch.setattr(runtime, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(runtime, "assert_database_schema_current", schema_is_current)
    monkeypatch.setattr(runtime, "NotificationHealthServer", forbidden_server)
    monkeypatch.setattr(runtime, "_compose_email_channel", forbidden_after_scope)
    with pytest.raises(TenantIsolationViolation) as caught:
        async with runtime.notification_worker_runtime(config):
            pytest.fail("未隔离数据库被允许处理通知")
    assert_rejected_safely(caught.value, engine)


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_mailbox_api_entrypoint_refuses_unisolated_database_before_serving(
    monkeypatch, tmp_path, failure
):
    from pydantic import SecretStr

    from apps.api import mailbox

    engine = RejectingDatabase(TENANT, failure)
    monkeypatch.setattr(mailbox, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(mailbox, "assert_database_schema_current", schema_is_current)
    (tmp_path / "index.html").write_text("<html>synthetic mailbox</html>")
    config = SimpleNamespace(
        tenant_id=str(TENANT),
        api_port=18762,
        origin="http://127.0.0.1:18762",
        database_url=SecretStr("postgresql+asyncpg://owner@unused.invalid/test"),
    )
    app = mailbox.create_mailbox_app(config, tmp_path)
    with pytest.raises(mailbox.RuntimeStartupError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("独立邮箱 API 绕过了隔离门禁")
    assert_rejected_safely(caught.value, engine, mailbox.RuntimeStartupError)


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_enabled_email_feedback_entrypoint_refuses_unisolated_database_before_provider(
    monkeypatch, failure
):
    from apps.email_feedback_worker import runtime
    from tests.unit.test_email_feedback_worker_config import _environment

    environ = _environment()
    engine = RejectingDatabase(environ["TRADEOS_TENANT_ID"], failure)
    monkeypatch.setattr(runtime, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(runtime, "assert_database_schema_current", schema_is_current)
    monkeypatch.setattr(runtime, "EnvironmentSecretResolver", forbidden_server)
    factory = runtime.EmailFeedbackRuntimeFactory(
        environ, transport_factory=forbidden_server
    )
    with pytest.raises(TenantIsolationViolation) as caught:
        async with factory():
            pytest.fail("邮件反馈入口未隔离就读取 Provider")
    assert_rejected_safely(caught.value, engine)


@pytest.mark.parametrize("failure", ["owner", "probe_error"])
async def test_mailbox_sync_entrypoint_refuses_unisolated_database_before_register_or_oauth(
    monkeypatch, tmp_path, failure
):
    from pydantic import SecretStr

    from apps.email_feedback_worker import mailbox

    engine = RejectingDatabase(TENANT, failure)
    config = mailbox.SyncConfiguration(
        database_url=SecretStr("postgresql+asyncpg://owner@unused.invalid/test"),
        tenant_id=str(TENANT),
        employee_id="emp-synthetic",
        email="synthetic@example.test",
        credentials_file=tmp_path / "must-not-open.json",
        fingerprint_version="synthetic-v1",
        fingerprint_key=SecretStr("synthetic-only-fingerprint"),
    )
    monkeypatch.setattr(mailbox, "load_sync_configuration", lambda _: config)
    monkeypatch.setattr(mailbox, "create_engine_from", lambda _: engine)
    monkeypatch.setattr(mailbox, "assert_database_schema_current", schema_is_current)
    monkeypatch.setattr(mailbox, "GmailMailboxHttpProvider", forbidden_server)
    with pytest.raises(TenantIsolationViolation) as caught:
        await mailbox.run(SimpleNamespace())
    assert_rejected_safely(caught.value, engine)
