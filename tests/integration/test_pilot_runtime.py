"""本机内测组合：真实入口、同源静态、显式政策与拒绝外部端口。"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from infra.pilot.config import PilotError
from tests.unit.test_pilot_profile import make_config


async def test_pilot_model_rejects_without_synthetic_result():
    from apps.api.pilot import UnconfiguredModelClient

    with pytest.raises(PilotError, match="model_not_configured"):
        await UnconfiguredModelClient().complete_json(
            model="unused", system_prompt="", payload={}, max_output_tokens=1
        )


async def test_static_mount_enters_canonical_lifespan_and_never_masks_api(tmp_path):
    from apps.api.pilot import mount_web

    build = tmp_path / "dist"
    build.mkdir()
    (build / "index.html").write_text("<html>TradeOS</html>")
    (build / "assets").mkdir()
    (build / "assets/app.js").write_text("void 0")
    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        try:
            yield
        finally:
            events.append("stop")

    business = FastAPI(lifespan=lifespan)
    app = mount_web(business, build)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client,
    ):
        assert (await client.get("/crm/opportunities")).status_code == 200
        assert (await client.get("/assets/app.js")).status_code == 200
        assert (await client.get("/assets/missing.js")).status_code == 404
        assert (await client.get("/api/unknown")).status_code == 404
        assert (await client.get("/%2e%2e/config.json")).status_code == 404
    assert events == ["start", "stop"]


def test_settings_explicit_nondev_and_no_provider_references(tmp_path):
    from apps.api.pilot import runtime_settings
    from apps.scheduler_worker.config import SchedulerWorkerConfig
    from shared.errors import ValidationError

    config = make_config(tmp_path)
    settings = runtime_settings(config)
    assert settings.dev_mode is False
    assert (
        settings.handoff_policy.sla_seconds == config.policy.handoff_policy.sla_seconds
    )
    assert not settings.gmail_oauth_token_ref
    assert not settings.openai_api_key_ref
    env = config.runtime_environment()
    with pytest.raises(ValidationError):
        SchedulerWorkerConfig.from_environ(env)
    parsed = SchedulerWorkerConfig.from_pilot_environ(env)
    assert parsed.gmail_oauth_token_ref is None
    assert parsed.dkim_selector is None
    with pytest.raises(ValidationError):
        SchedulerWorkerConfig.from_pilot_environ(
            {**env, "GMAIL_OAUTH_TOKEN_REF": "EXTERNAL"}
        )


async def test_pilot_dns_step_fails_as_unconfigured():
    from apps.scheduler_worker.pilot import UnconfiguredDnsResolver, UnconfiguredDnsStep

    assert await UnconfiguredDnsStep().execute(None) == (
        "fail",
        "dns_not_configured",
        {},
    )
    with pytest.raises(PilotError, match="dns_not_configured"):
        await UnconfiguredDnsResolver().resolve("example.com", "TXT")


def test_start_refuses_missing_build_before_storage(tmp_path, monkeypatch):
    from scripts.pilot_web_supervisor import PilotSupervisor

    make_config(tmp_path)
    supervisor = PilotSupervisor(tmp_path / "profile", root=tmp_path)
    try:
        with pytest.raises(PilotError, match="web_build_missing"):
            supervisor.start()
        assert supervisor.profile.config.storage == {}
    finally:
        supervisor.close()


def test_cli_account_parser_never_echoes_unknown_secret(capsys):
    import secrets

    from scripts.run_web_pilot import main

    material = secrets.token_urlsafe(32)
    assert (
        main(
            ["accounts", "--profile", "/nonexistent", "create", "--password", material]
        )
        == 2
    )
    output = capsys.readouterr()
    hidden = material not in output.out + output.err
    assert hidden, "CLI_ARGUMENT_EXPOSED"


from tests.integration.test_pilot_persistence import initialized
from tests.integration.test_pilot_persistence import owned_profiles as _owned_profiles

owned_profiles = _owned_profiles


def test_actual_three_process_start_health_stop_and_schema_refusal(owned_profiles):
    from infra.pilot.resources import reserve_port
    from scripts.run_web_pilot import start_profile

    directory, profiles = owned_profiles
    profile = initialized(directory, profiles)
    profile.stop()
    start_profile(profile.path)
    profile.reload()
    state = profile.runtime_state()
    assert state.status == "running"
    assert {p.name for p in state.processes} == {"api", "scheduler", "notification"}
    origin = f"http://127.0.0.1:{profile.config.api_port}"
    with httpx.Client(base_url=origin, trust_env=False) as client:
        assert client.get("/crm/opportunities").status_code == 200
        assert client.get("/api/health/ready").status_code == 200
        assert client.get("/api/auth/session").status_code == 401
        assert client.get("/api/health/capabilities").status_code == 401
        assert (
            client.get(
                "/api/health/ready", headers={"Host": "evil.example"}
            ).status_code
            == 403
        )
        response = client.get(
            f"http://127.0.0.1:{profile.config.notification_port}/health/capabilities"
        )
        assert response.json()["email"] == "disabled"
    with pytest.raises(PilotError):
        start_profile(profile.path)
    assert profile.runtime_state().status == "running"
    profile.stop()
    assert all(not p.live() for p in (state.supervisor,))
    assert profile.runtime_state().status == "stopped"
    for port in (
        profile.config.api_port,
        profile.config.scheduler_port,
        profile.config.notification_port,
    ):
        with reserve_port(port):
            pass
    start_profile(profile.path)
    assert profile.runtime_state().status == "running"
    assert profile.config.api_port == int(origin.rsplit(":", 1)[1])
    profile.stop()
    # start 只能检查版本；使用本测试新数据库模拟落后，不触碰真实 profile。
    profile.start_storage()

    async def lag_schema():
        from sqlalchemy import text

        from infra.db.session import create_engine_from

        engine = create_engine_from(profile.config.database_url.get_secret_value())
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE alembic_version SET version_num='synthetic-old'")
                )
        finally:
            await engine.dispose()

    asyncio.run(lag_schema())
    with pytest.raises(PilotError):
        start_profile(profile.path)
    profile.require_stopped()


def test_pilot_object_store_parser_keeps_auth_mode_false_and_exact_loopback(tmp_path):
    from connectors.object_store.config import S3ObjectStoreSettings
    from shared.errors import PolicyViolation

    config = make_config(tmp_path).model_copy(update={"object_port": 19091})
    env = config.runtime_environment()
    result = S3ObjectStoreSettings.from_pilot_environ(env)
    assert result.dev_mode is False
    assert result.endpoint == "http://127.0.0.1:19091"
    with pytest.raises(PolicyViolation):
        S3ObjectStoreSettings.from_environ(env)
    for endpoint in (
        "https://example.com",
        "http://localhost:19091",
        "http://[::1]:19091",
        "http://127.0.0.1:19091/",
        "http://127.0.0.1:19091?x=1",
    ):
        with pytest.raises(PolicyViolation):
            S3ObjectStoreSettings.from_pilot_environ({**env, "S3_ENDPOINT": endpoint})
    with pytest.raises(PolicyViolation):
        S3ObjectStoreSettings.from_pilot_environ({**env, "TRADEOS_DEV_MODE": "true"})


def test_pilot_scheduler_factory_refuses_external_ports(tmp_path):
    from types import SimpleNamespace

    from apps.scheduler_worker.pilot import create_pilot_factory
    from apps.scheduler_worker.runtime import SchedulerRuntimeFactory
    from shared.errors import ValidationError

    config = make_config(tmp_path)
    factory = create_pilot_factory(config)
    external = SimpleNamespace(
        research_enabled=False, campaign_enabled=True, contacts_enabled=False
    )
    with pytest.raises(ValidationError):
        SchedulerRuntimeFactory(
            config.runtime_environment(),
            pilot_config=factory._pilot_config,
            secret_resolver=config,
            unconfigured_dns_step=factory._unconfigured_dns_step,
            bootstrap=external,
        )


def test_pilot_scheduler_factory_composes_only_explicit_gmail_ports(tmp_path):
    from apps.scheduler_worker.pilot import create_pilot_factory
    from infra.pilot.config import PilotGmailConfig

    credentials = tmp_path / "gmail-oauth.json"
    config = make_config(tmp_path).model_copy(
        update={
            "object_port": 19091,
            "gmail": PilotGmailConfig(
                address="owner@example.com",
                credentials_file=credentials,
                employee_id="emp_01M3M3BBRGA87N1H1Y7W79DCAX",
            )
        }
    )
    factory = create_pilot_factory(config)

    assert factory._pilot_config.gmail_oauth_token_ref == "GMAIL_OAUTH_TOKEN_REF"
    assert factory._inbound_ports is not None
    assert factory._inbound_ports.profile.mailbox_alias == "pilot-gmail"
    assert factory._inbound_ports.profile.tenant_id == config.tenant_id
    assert factory._bootstrap.campaign_enabled is True
    assert factory._bootstrap.contacts_enabled is False
    assert factory._bootstrap.research_enabled is False
    assert factory._bootstrap.reply_factory is not None


async def test_pilot_api_composes_explicit_gmail_inbound_management(tmp_path):
    from apps.api.pilot import create_pilot_app, runtime_settings
    from infra.pilot.config import PilotGmailConfig

    credentials = tmp_path / "gmail-oauth.json"
    config = make_config(tmp_path).model_copy(
        update={
            "object_port": 19091,
            "gmail": PilotGmailConfig(
                address="owner@example.com",
                credentials_file=credentials,
                employee_id="emp_01M3M3BBRGA87N1H1Y7W79DCAX",
            ),
        }
    )
    build = tmp_path / "dist"
    build.mkdir()
    (build / "index.html").write_text("<html>TradeOS</html>")

    app = create_pilot_app(config, build)
    business = app.state.business_app
    try:
        assert runtime_settings(config).gmail_oauth_token_ref == "GMAIL_OAUTH_TOKEN_REF"
        assert business.state.dependencies.email_inbound is not None
        status = business.state.dependencies.email_inbound.management.status_of(None)
        assert status.state == "disabled"
    finally:
        await business.state.runtime_engine.dispose()


def test_network_boundary_rejects_external_dns_and_socket_before_io():
    import subprocess
    import sys

    code = """
import socket
from infra.controlled.network import install_network_boundary
from infra.controlled.config import ControlledError
install_network_boundary(destinations=frozenset({19091}),listeners=frozenset({19092}))
rejected=0
for operation in (lambda: socket.getaddrinfo("example.com",443),lambda: socket.socket().connect(("203.0.113.10",443)),lambda: socket.socket().bind(("0.0.0.0",19092))):
 try: operation()
 except ControlledError: rejected+=1
print(rejected)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0
    assert result.stdout == b"3\n"


def test_network_boundary_allows_only_pre_resolved_gmail_endpoints():
    import subprocess
    import sys

    code = """
import socket
import sys
from infra.controlled.network import install_network_boundary
from infra.controlled.config import ControlledError
install_network_boundary(
 destinations=frozenset({19091}),
 listeners=frozenset({19092}),
 external_hosts=frozenset({("gmail.googleapis.com",443),("oauth2.googleapis.com",443)}),
 external_destinations=frozenset({("203.0.113.20",443),("2001:db8::20",443)}),
)
allowed=0
for event,args in (
 ("socket.getaddrinfo",("gmail.googleapis.com",443,0,0,0)),
 ("socket.getaddrinfo",("oauth2.googleapis.com",443,0,0,0)),
 ("socket.connect",(socket.socket(),("203.0.113.20",443))),
 ("socket.connect",(socket.socket(socket.AF_INET6),("2001:db8::20",443,0,0))),
):
 try: sys.audit(event,*args); allowed+=1
 finally:
  if event == "socket.connect": args[0].close()
rejected=0
for event,args in (
 ("socket.getaddrinfo",("example.com",443,0,0,0)),
 ("socket.connect",(socket.socket(),("203.0.113.21",443))),
 ("socket.connect",(socket.socket(),("203.0.113.20",80))),
):
 try: sys.audit(event,*args)
 except ControlledError: rejected+=1
 finally:
  if event == "socket.connect": args[0].close()
print(allowed,rejected)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0
    assert result.stdout == b"4 3\n"


def test_start_busy_error_stays_specific_and_never_mutates_profile(tmp_path):
    from infra.pilot.config import exclusive_profile_lock
    from scripts.run_web_pilot import start_profile

    make_config(tmp_path)
    with (
        exclusive_profile_lock(tmp_path / "profile"),
        pytest.raises(PilotError, match="profile_busy"),
    ):
        start_profile(tmp_path / "profile")


def test_start_rejects_profile_symlink_before_resource_operations(tmp_path):
    from scripts.run_web_pilot import start_profile

    make_config(tmp_path)
    (tmp_path / "linked").symlink_to(tmp_path / "profile", target_is_directory=True)
    with pytest.raises(PilotError, match="configuration_invalid"):
        start_profile(tmp_path / "linked")


def test_unexpected_owned_child_exit_preserves_failed_state_after_cleanup(
    owned_profiles,
):
    import os
    import signal
    import time

    from scripts.run_web_pilot import start_profile

    directory, profiles = owned_profiles
    profile = initialized(directory, profiles)
    start_profile(profile.path)
    state = profile.runtime_state()
    scheduler = next(p for p in state.processes if p.name == "scheduler")
    assert scheduler.live()
    os.kill(scheduler.pid, signal.SIGKILL)
    deadline = time.monotonic() + 40
    while state.supervisor.live() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not state.supervisor.live(), "OWNED_SUPERVISOR_DID_NOT_STOP"
    assert all(not process.live() for process in state.processes)
    profile.reload()
    profile.require_stopped()
    result = profile.runtime_state()
    assert result.status == "failed"
    assert result.reason == "operation_failed"
    assert result.supervisor is None
    assert result.processes == ()
    assert profile.status()["applications"] == "failed"
