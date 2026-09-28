"""邮箱专用启动器只操作所属资源，守护进程不启动模型或发送。"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra.pilot.config import PilotError
from shared.schemas.identifiers import new_id


@pytest.fixture(autouse=True)
def restore_cli_logging():
    """进程级 CLI 日志屏蔽不得泄漏到同进程后续测试。"""
    previous = logging.root.manager.disable
    try:
        yield
    finally:
        logging.disable(previous)


def test_unknown_cli_argument_does_not_echo_value(capsys) -> None:
    from scripts.run_mailbox import main

    assert (
        main(
            [
                "status",
                "--profile",
                "/missing/config.json",
                "--secret",
                "never-echo-this",
            ]
        )
        == 2
    )
    result = capsys.readouterr()
    assert "never-echo-this" not in result.out + result.err
    assert json.loads(result.out)["reason"] == "mailbox_input_invalid"


def test_service_commands_keep_credentials_and_email_out_of_argv(
    tmp_path: Path,
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.mailbox_supervisor import service_commands

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    unbound = service_commands(config, path, tmp_path / "dist")
    assert set(unbound) == {"api"}
    config = MailboxConfig.bind(
        path,
        employee_id=str(new_id("emp")),
        email="owner@example.test",
        credentials_file=tmp_path / "oauth.json",
    )
    commands = service_commands(config, path, tmp_path / "dist")
    assert len(commands) == 2
    worker = commands[config.bindings[0].binding_id]
    assert worker[1:4] == ["-m", "apps.email_feedback_worker.mailbox", "sync"]
    assert "--binding-id" in worker and "--watch" in worker
    exposed = " ".join(part for command in commands.values() for part in command)
    assert "owner@example.test" not in exposed and "oauth.json" not in exposed
    assert config.db_password.get_secret_value() not in exposed
    assert "send" not in exposed and "scheduler" not in exposed


def test_launch_agent_is_private_path_only_and_throttled(tmp_path: Path) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.run_mailbox import launch_agent_payload

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    payload = launch_agent_payload(config, path, tmp_path / "dist")
    assert payload["RunAtLoad"] is True and payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] >= 30
    assert "EnvironmentVariables" not in payload
    assert config.db_password.get_secret_value() not in str(payload)
    assert str(path) in payload["ProgramArguments"]


def test_supervisor_reconciles_bindings_and_restarts_with_backoff(
    tmp_path: Path,
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.mailbox_supervisor import ChildServices

    class Child:
        def __init__(self) -> None:
            self.exited = False
            self.closed = False
            self.process = SimpleNamespace(poll=lambda: 1 if self.exited else None)

        def stop(self, timeout=120) -> None:
            self.closed = True

    spawned = []

    def spawn(name, command, **kwargs):
        child = Child()
        spawned.append((name, command, child))
        return child

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    children = ChildServices(path, tmp_path / "dist", spawn=spawn)
    children.reconcile(config, now=0)
    assert len(spawned) == 1
    spawned[0][2].exited = True
    children.reconcile(config, now=1)
    children.reconcile(config, now=29)
    assert len(spawned) == 1
    children.reconcile(config, now=32)
    assert len(spawned) == 2
    config = MailboxConfig.bind(
        path,
        employee_id=str(new_id("emp")),
        email="owner@example.test",
        credentials_file=tmp_path / "oauth.json",
    )
    children.reconcile(config, now=33)
    assert len(spawned) == 3
    children.stop()
    assert all(child.closed for _, _, child in spawned)


def test_database_verification_refuses_wrong_owner_before_action(
    tmp_path: Path,
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig, MailboxStorageIdentity
    from scripts.run_mailbox import MailboxDatabase

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    identity = MailboxStorageIdentity(
        image_id="sha256:" + "a" * 64,
        volume_name=f"tradeos-mailbox-{config.owner_id}-database",
        volume_created_at="now",
        container_id="b" * 64,
    )
    config.model_copy(update={"storage": identity}).write(path)
    container = SimpleNamespace(
        id=identity.container_id,
        labels={"tradeos.mailbox.owner": "wrong"},
        attrs={},
        reload=lambda: None,
    )
    client = SimpleNamespace(containers=SimpleNamespace(get=lambda value: container))
    database = MailboxDatabase(path, client=client)
    with pytest.raises(PilotError, match="resource_identity_invalid"):
        database.verify()


def fake_docker() -> SimpleNamespace:
    """隔离 Docker SDK 的外部调用，保留实际配置写入与归属检查。"""
    import docker

    volumes = {}
    containers = {}

    def volume_get(name):
        if name not in volumes:
            raise docker.errors.NotFound("missing")
        return volumes[name]

    def volume_create(*, name, labels):
        volume = SimpleNamespace(
            attrs={"Name": name, "CreatedAt": "2026-09-27T00:00:00Z", "Labels": labels},
            reload=lambda: None,
        )
        volumes[name] = volume
        return volume

    def container_create(image, **kwargs):
        name, mount = next(iter(kwargs["volumes"].items()))
        host, port = kwargs["ports"]["5432/tcp"]
        container = SimpleNamespace(
            id="c" * 64,
            name=kwargs["name"],
            labels=kwargs["labels"],
            status="created",
            reload=lambda: None,
            attrs={
                "Image": image,
                "HostConfig": {
                    "PortBindings": {
                        "5432/tcp": [{"HostIp": host, "HostPort": str(port)}]
                    }
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": name,
                        "Destination": mount["bind"],
                        "RW": mount["mode"] == "rw",
                    }
                ],
            },
        )
        container.start = lambda: setattr(container, "status", "running")
        container.stop = lambda **kw: setattr(container, "status", "exited")
        containers[container.id] = container
        return container

    def container_get(ident):
        matches = [
            item for item in containers.values() if ident in {item.id, item.name}
        ]
        if not matches:
            raise docker.errors.NotFound("missing")
        return matches[0]

    return SimpleNamespace(
        images=SimpleNamespace(
            get=lambda name: SimpleNamespace(id="sha256:" + "a" * 64)
        ),
        volumes=SimpleNamespace(get=volume_get, create=volume_create),
        containers=SimpleNamespace(
            get=container_get,
            create=container_create,
            list=lambda **kw: list(containers.values()),
        ),
    )


def test_new_database_is_loopback_owned_and_stop_preserves_storage(
    tmp_path: Path,
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.run_mailbox import MailboxDatabase

    path = tmp_path / "mailbox/config.json"
    before = MailboxConfig.create(path)
    client = fake_docker()
    database = MailboxDatabase(path, client=client)
    database.provision()
    stored = MailboxConfig.read(path)
    assert stored.tenant_id == before.tenant_id and stored.bindings == ()
    assert stored.storage is not None
    container = database.verify()
    assert container.attrs["HostConfig"]["PortBindings"] == {
        "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(before.db_port)}]
    }
    database.start()
    assert container.status == "running"
    database.stop()
    assert container.status == "exited"
    assert database.verify().id == stored.storage.container_id
    assert MailboxConfig.read(path).storage == stored.storage


@pytest.mark.parametrize(
    "mutation", ["public_port", "wrong_mount", "replaced_volume", "shared_volume"]
)
def test_database_identity_drift_blocks_start(tmp_path: Path, mutation: str) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.run_mailbox import MailboxDatabase

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    client = fake_docker()
    database = MailboxDatabase(path, client=client)
    database.provision()
    container = database.verify()
    if mutation == "public_port":
        container.attrs["HostConfig"]["PortBindings"]["5432/tcp"][0]["HostIp"] = (
            "0.0.0.0"
        )
    elif mutation == "wrong_mount":
        container.attrs["Mounts"][0]["Type"] = "bind"
    elif mutation == "replaced_volume":
        client.volumes.get(database.config.storage.volume_name).attrs["CreatedAt"] = (
            "changed"
        )
    else:
        client.containers.list = lambda **kw: [container, SimpleNamespace(id="d" * 64)]
    with pytest.raises(PilotError, match="resource_identity_invalid"):
        database.start()
    assert container.status == "created"


def test_init_resumes_same_profile_after_docker_connection_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts import run_mailbox

    path = tmp_path / "mailbox/config.json"
    attempts = []

    class Database:
        def __init__(self, path):
            attempts.append(MailboxConfig.read(path).tenant_id)
            if len(attempts) == 1:
                raise RuntimeError("private-runtime-detail")

        def provision(self):
            pass

        async def migrate(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(run_mailbox, "MailboxDatabase", Database)
    assert run_mailbox.main(["init", "--profile", str(path)]) == 2
    original = MailboxConfig.read(path)
    assert run_mailbox.main(["init", "--profile", str(path)]) == 0
    assert MailboxConfig.read(path) == original
    assert attempts == [original.tenant_id, original.tenant_id]
    assert "private-runtime-detail" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure",
    [
        "image_missing",
        "volume_response_lost",
        "container_not_created",
        "container_response_lost",
    ],
)
def test_provision_resumes_only_its_own_partial_resources(
    tmp_path: Path, failure: str
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.run_mailbox import MailboxDatabase

    path = tmp_path / "mailbox/config.json"
    original = MailboxConfig.create(path)
    client = fake_docker()
    database = MailboxDatabase(path, client=client)
    target = (
        client.images
        if failure == "image_missing"
        else client.volumes
        if failure == "volume_response_lost"
        else client.containers
    )
    method = "get" if failure == "image_missing" else "create"
    real_call = getattr(target, method)

    def failing(*args, **kwargs):
        if failure.endswith("response_lost"):
            real_call(*args, **kwargs)
        raise RuntimeError("controlled-docker-failure")

    setattr(target, method, failing)
    with pytest.raises(RuntimeError, match="controlled-docker-failure"):
        database.provision()
    partial = MailboxConfig.read(path)
    setattr(target, method, real_call)
    database.provision()
    complete = MailboxConfig.read(path)
    assert complete.tenant_id == original.tenant_id
    assert complete.db_password == original.db_password
    assert complete.storage is not None
    if partial.storage is not None and partial.storage.volume_created_at:
        assert complete.storage.volume_created_at == partial.storage.volume_created_at
    assert database.verify().id == complete.storage.container_id
    database.provision()
    assert MailboxConfig.read(path) == complete


def test_partial_resume_refuses_foreign_or_attached_volume(tmp_path: Path) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts.run_mailbox import MailboxDatabase

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    client = fake_docker()
    database = MailboxDatabase(path, client=client)
    real_create = client.containers.create
    client.containers.create = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("failed")
    )
    with pytest.raises(RuntimeError, match="failed"):
        database.provision()
    partial = MailboxConfig.read(path)
    assert partial.storage is not None
    client.containers.create = real_create
    volume = client.volumes.get(partial.storage.volume_name)
    original_owner = volume.attrs["Labels"]["tradeos.mailbox.owner"]
    volume.attrs["Labels"]["tradeos.mailbox.owner"] = "another-owner"
    with pytest.raises(PilotError, match="resource_identity_invalid"):
        database.provision()
    volume.attrs["Labels"]["tradeos.mailbox.owner"] = original_owner
    client.containers.list = lambda **kwargs: [SimpleNamespace(id="d" * 64)]
    with pytest.raises(PilotError, match="resource_identity_invalid"):
        database.provision()
    assert MailboxConfig.read(path) == partial


def test_launch_agent_registered_but_not_executing_stays_pending(
    tmp_path: Path, monkeypatch
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts import run_mailbox

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    ticks = iter([0, 1, 16])
    monkeypatch.setattr(run_mailbox.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(run_mailbox.time, "sleep", lambda delay: None)
    monkeypatch.setattr(run_mailbox, "supervisor_identity", lambda *args: None)
    with pytest.raises(PilotError, match="launch_agent_start_pending"):
        run_mailbox.wait_for_launch_agent(path, config)


def test_launch_agent_success_needs_its_actual_supervisor_and_http_ready(
    tmp_path: Path, monkeypatch
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts import run_mailbox

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    monkeypatch.setattr(
        run_mailbox, "supervisor_identity", lambda *args: SimpleNamespace(pid=44)
    )
    monkeypatch.setattr(run_mailbox, "launch_agent_pid", lambda label: 44)
    monkeypatch.setattr(run_mailbox, "mailbox_http_ready", lambda origin: True)
    run_mailbox.wait_for_launch_agent(path, config)
    ticks = iter([0, 1, 16])
    monkeypatch.setattr(run_mailbox.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(run_mailbox.time, "sleep", lambda delay: None)
    monkeypatch.setattr(run_mailbox, "mailbox_http_ready", lambda origin: False)
    with pytest.raises(PilotError, match="launch_agent_start_pending"):
        run_mailbox.wait_for_launch_agent(path, config)


def test_install_reports_pending_without_removing_registered_plist(
    tmp_path: Path, monkeypatch
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts import run_mailbox

    path = tmp_path / "mailbox/config.json"
    config = MailboxConfig.create(path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(run_mailbox.sys, "platform", "darwin")
    monkeypatch.setattr(run_mailbox, "stop_supervisor", lambda path: None)
    monkeypatch.setattr(
        run_mailbox.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )

    def pending(*args):
        raise PilotError("launch_agent_start_pending")

    monkeypatch.setattr(run_mailbox, "wait_for_launch_agent", pending)
    with pytest.raises(PilotError, match="launch_agent_start_pending"):
        run_mailbox.install_agent(path, tmp_path / "dist")
    assert (
        tmp_path
        / "Library/LaunchAgents"
        / f"com.tradeos.mailbox.{config.owner_id}.plist"
    ).is_file()


def test_http_ready_checks_api_json_not_spa_success() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from scripts.run_mailbox import mailbox_http_ready

    state = {"status": 503, "body": b'{"status":"unavailable"}'}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            api = self.path == "/api/health/ready"
            self.send_response(state["status"] if api else 200)
            self.send_header("Content-Type", "application/json" if api else "text/html")
            self.end_headers()
            self.wfile.write(state["body"] if api else b"<html>SPA</html>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        assert mailbox_http_ready(origin) is False
        state.update(status=200, body=b'{"status":"ready"}')
        assert mailbox_http_ready(origin) is True
        state.update(body=b'{"status":"live"}')
        assert mailbox_http_ready(origin) is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_status_separates_process_health_from_mailbox_sync(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig
    from scripts import run_mailbox

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    monkeypatch.setattr(
        run_mailbox, "supervisor_identity", lambda *args: SimpleNamespace(pid=44)
    )
    monkeypatch.setattr(run_mailbox, "mailbox_http_ready", lambda origin: True)
    assert run_mailbox.main(["status", "--profile", str(path)]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["supervisor_status"] == "running"
    assert value["api_status"] == "ready"
    assert value["mailbox_sync"] == "not_checked"
    assert value["bound_mailboxes"] == 0
