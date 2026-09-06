"""启动器真实进程边界；不借用既有部署配置。"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run_web_core_controlled.py"


def command(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=env or {"PATH": os.defpath, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def test_occupied_port_fails_without_killing_listener(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = command(
            "--api-port", str(listener.getsockname()[1]), "--directory", str(tmp_path)
        )
        assert result.returncode != 0
        assert json.loads(result.stdout)["reason"] == "port_in_use"
        assert listener.getsockname()[1] > 0
        assert list(tmp_path.iterdir()) == []


def test_missing_node_is_fixed_nonzero(tmp_path: Path) -> None:
    result = command(
        "--directory",
        str(tmp_path),
        env={"PATH": "/nonexistent", "PYTHON_DOTENV_DISABLED": "1"},
    )
    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "dependency_missing"
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []


async def test_provider_mail_survives_transport_reconstruction(tmp_path: Path) -> None:
    import importlib

    assert importlib.util.find_spec("infra.controlled.providers") is not None
    from infra.controlled.providers import ControlledGmailTransport

    first = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-a")
    raw = b"Message-ID: <sample@controlled.test>\r\nX-TradeOS-Idempotency-V1: sample\r\n\r\nHello"
    ref = await first.send(token="ignored", raw_message=raw)
    second = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-a")
    assert (
        await second.search(
            token="ignored", message_id="sample@controlled.test", header="sample"
        )
        == ref
    )
    assert await second.send(token="ignored", raw_message=raw) == ref


async def test_dns_unknown_name_refused_without_resolver_fallback() -> None:
    import importlib

    import pytest

    assert importlib.util.find_spec("infra.controlled.providers") is not None
    from infra.controlled.config import ControlledError
    from infra.controlled.providers import ControlledDnsResolver

    resolver = ControlledDnsResolver()
    with pytest.raises(ControlledError, match="external_operation_rejected"):
        await resolver.resolve("gmail.com", "TXT")
    records = await resolver.resolve("tradeos-controlled.test", "TXT")
    assert records[0].strings == (b"v=spf1 -all",)


def test_stack_ready_restart_and_term_owned_cleanup(tmp_path: Path) -> None:
    import time
    import urllib.request

    import pytest

    env = {"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"}
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--directory", str(tmp_path)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 100
        state: dict = {}
        while time.monotonic() < deadline:
            paths = list(tmp_path.glob("*/status.json"))
            if paths:
                state = json.loads(paths[0].read_text())
                if state["status"] == "ready":
                    break
            if process.poll() is not None:
                pytest.fail(f"launcher failed before ready: {process.communicate()[0]}")
            time.sleep(0.1)
        assert state["status"] == "ready"
        assert len({item["pid"] for item in state["processes"]}) == 4
        with urllib.request.urlopen(
            urllib.request.Request(
                state["api_url"] + "/health/capabilities",
                headers={
                    "X-Tenant-Id": state["tenant_id"],
                    "X-Employee-Id": state["identities"][0]["employee_id"],
                },
            )
        ) as response:
            assert response.status == 200
        with urllib.request.urlopen(
            state["notification_url"] + "/health/capabilities"
        ) as response:
            capability = json.load(response)
            assert capability == {
                "mode": "controlled_in_app",
                "in_app": "enabled",
                "email": "disabled",
            }
        with urllib.request.urlopen(
            state["scheduler_url"] + "/health/capabilities"
        ) as response:
            capabilities = json.load(response)
            assert any(
                c["name"] == "inbound_body" and c["status"] == "enabled"
                for c in capabilities
            )
        with urllib.request.urlopen(state["web_url"]) as response:
            assert response.status == 200
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                errors = []
                page.on("pageerror", lambda error: errors.append(type(error).__name__))
                page.goto(state["web_url"] + "/settings")
                page.get_by_text("API / 调度 / Web 当前就绪", exact=True).wait_for(
                    timeout=10000
                )
                page.get_by_label("演练角色").select_option(
                    state["identities"][1]["employee_id"]
                )
                assert (
                    page.get_by_label("演练角色").input_value()
                    == state["identities"][1]["employee_id"]
                )
                page.get_by_role("heading", name="系统设置", exact=True).wait_for(
                    timeout=10000
                )
                page.get_by_role("heading", name="首次配置", exact=True).wait_for(
                    timeout=10000
                )
                page.get_by_text("尚未配置 Company Playbook", exact=True).wait_for(
                    timeout=10000
                )
                assert page.evaluate("document.documentElement.scrollWidth") <= 390
                page.screenshot(
                    path=str(tmp_path / "controlled-390.png"), full_page=True
                )
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.screenshot(
                    path=str(tmp_path / "controlled-desktop.png"), full_page=True
                )
                assert not errors
            finally:
                browser.close()
        import signal

        initial = {item["pid"] for item in state["processes"]}
        process.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = json.loads(paths[0].read_text())
            if state["status"] == "ready" and initial.isdisjoint(
                {item["pid"] for item in state["processes"]}
            ):
                break
            time.sleep(0.1)
        assert state["status"] == "ready", state
        assert initial.isdisjoint({item["pid"] for item in state["processes"]})
        process.terminate()
        stdout, stderr = process.communicate(timeout=35)
        assert process.returncode == 0, stdout
        assert stderr == ""
        final = json.loads(paths[0].read_text())
        assert final["status"] == "stopped"
        assert final["cleanup_errors"] == []
        assert not (paths[0].parent / "config.json").exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=40)


async def test_explicit_chinese_scenario_produces_guarded_proposal() -> None:
    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from agent_runtime.trade_manager import (
        StructuredTradeManagerModelPort,
        TradeManagerAgent,
    )
    from apps.api.controlled import ControlledModelClient

    model = ControlledModelClient()
    agent = TradeManagerAgent(
        "controlled-json-v1",
        StructuredTradeManagerModelPort(model, "controlled-json-v1"),
        None,
        CredentialMarkerGuard(),
    )
    draft = await agent.propose_discovery(
        "受控演练：只研究肯尼亚家具五金需求，覆盖进口商、分销商、电商三线路，各查1次，最多读3页、记录3条信号和3条假设，最低证据档位low_mid，不触达不发送。"
    )
    assert draft.plan.execution_mode == "research_only"
    assert draft.plan.target_countries == ("KE",)
    assert draft.plan.max_search_queries == 3


def test_unknown_network_address_fails_before_connection() -> None:
    script = """
import socket
from infra.controlled.network import install_network_boundary
from infra.controlled.config import ControlledError
install_network_boundary(destinations=frozenset({19000}), listeners=frozenset())
for action in (lambda: socket.getaddrinfo('example.com', 443), lambda: socket.create_connection(('127.0.0.1', 19001))):
    try:
        action()
    except ControlledError:
        pass
    else:
        raise AssertionError('unknown destination accepted')
print('rejected_before_network')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "rejected_before_network"


@pytest.mark.parametrize("identity_field", ["born", "anchor_born"])
def test_process_birth_mismatch_never_signals_foreign_process(
    identity_field: str,
) -> None:
    import pytest

    from infra.controlled.config import ControlledError
    from infra.controlled.resources import OwnedProcess

    process = OwnedProcess.start(
        "owned-test",
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=ROOT,
        environ={"PATH": os.defpath},
    )
    original = getattr(process, identity_field)
    try:
        setattr(process, identity_field, original + 1)
        with pytest.raises(ControlledError, match="process_owner_unknown"):
            process.stop()
        assert process.process.poll() is None
    finally:
        setattr(process, identity_field, original)
        process.stop()


def test_failure_after_owned_container_creation_cleans_all_stages(
    tmp_path: Path,
) -> None:
    import docker

    for fail_after in (1, 2):
        script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from infra.controlled.resources import OwnedContainers
from infra.controlled.config import ControlledError
original = OwnedContainers.create
count = 0
def fail(self, *args, **kwargs):
    global count
    result = original(self, *args, **kwargs)
    count += 1
    if count == FAIL_AFTER:
        raise ControlledError('infrastructure_test_failure')
    return result
OwnedContainers.create = fail
from run_web_core_controlled import main
sys.argv = ['run', '--directory', DIRECTORY]
raise SystemExit(main())
""".replace("FAIL_AFTER", str(fail_after)).replace("DIRECTORY", repr(str(tmp_path)))
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"},
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        final = json.loads(result.stdout.splitlines()[-1])
        assert final["reason"] == "infrastructure_test_failure"
        assert final["cleanup_errors"] == []
        state = json.loads((Path(final["directory"]) / "status.json").read_text())
        client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
        try:
            assert (
                client.containers.list(
                    all=True,
                    filters={"label": "tradeos.controlled.owner=" + state["owner"]},
                )
                == []
            )
        finally:
            client.close()


def test_real_http_configuration_approval_and_worker_crash(tmp_path: Path) -> None:
    import signal
    import time

    import docker
    import httpx

    from apps.api.controlled import CONTROLLED_RESEARCH_MESSAGE

    process = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--directory", str(tmp_path)],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        state = {}
        while time.monotonic() < deadline:
            paths = list(tmp_path.glob("*/status.json"))
            if paths:
                state = json.loads(paths[0].read_text())
                if state["status"] == "ready":
                    break
            assert process.poll() is None, process.communicate()[0]
            time.sleep(0.1)
        assert state["status"] == "ready"
        ids = state["identities"]
        headers = {
            "X-Tenant-Id": state["tenant_id"],
            "X-Employee-Id": ids[0]["employee_id"],
        }
        with httpx.Client(
            base_url=state["api_url"], headers=headers, trust_env=False, timeout=5
        ) as client:
            assert client.get("/settings/playbook").json()["configured"] is False
            proposal = client.post(
                "/commands/discovery-proposals",
                json={"message": CONTROLLED_RESEARCH_MESSAGE},
            )
            assert proposal.status_code == 200, proposal.text
            blocked = client.post(
                "/commands/discovery-proposals/"
                + proposal.json()["proposal_id"]
                + "/confirm"
            )
            assert blocked.status_code >= 400
            book = client.post(
                "/settings/playbook/proposals",
                headers={"Idempotency-Key": "controlled-book-v1"},
                json={
                    "company_type": "trading_company",
                    "minimum_deal_amount": "1000.00",
                    "minimum_deal_currency": "USD",
                    "excluded_categories": [],
                    "sourcing_regions": ["controlled"],
                    "excluded_countries": [],
                    "monthly_budget_credits": 3,
                    "approval_requirements": [],
                    "supply_capabilities_note": "仅用于本次合成演练。",
                },
            )
            assert book.status_code == 202, book.text
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                versions = client.get("/settings/playbook/versions").json()
                approval_id = next(
                    (v["approval_id"] for v in versions if v["approval_id"]), None
                )
                if approval_id:
                    break
                time.sleep(0.2)
            assert approval_id
            assert (
                client.post(
                    f"/approvals/{approval_id}/decide", json={"decision": "approve"}
                ).status_code
                == 400
            )
            approved = client.post(
                f"/approvals/{approval_id}/decide",
                headers={"X-Employee-Id": ids[1]["employee_id"]},
                json={"decision": "approve"},
            )
            assert approved.status_code == 200, approved.text
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                overview = client.get("/settings/playbook").json()
                if overview["configured"]:
                    break
                time.sleep(0.2)
            assert overview["configured"]
            policy = {
                "country": "KE",
                "public_research_allowed": True,
                "contact_enrichment_allowed": False,
                "cold_b2b_email_allowed": False,
                "personal_data_basis_required": True,
                "subject_type_affects_judgment": True,
                "contact_type_affects_judgment": True,
                "opt_out_deadline_days": 1,
                "local_representative_required": False,
                "requirements": ["honor_opt_out"],
                "notes": "合成演练政策，不代表真实法律判断。",
            }
            policy["field_sources"] = {
                key: {
                    "source_type": "employee_input",
                    "source_id": "controlled-assessment:" + key,
                }
                for key in policy
                if key not in {"country", "notes"}
            }
            submitted = client.post(
                "/settings/country-policies/proposals",
                headers={"Idempotency-Key": "controlled-policy-v1"},
                json=policy,
            )
            assert submitted.status_code == 202, submitted.text
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                versions = client.get(
                    "/settings/country-policies/versions?country=KE"
                ).json()
                approval_id = next(
                    (v["approval_id"] for v in versions if v["approval_id"]), None
                )
                if approval_id:
                    break
                time.sleep(0.2)
            assert approval_id
            assert (
                client.post(
                    f"/approvals/{approval_id}/decide", json={"decision": "approve"}
                ).status_code
                == 400
            )
            assert (
                client.post(
                    f"/approvals/{approval_id}/decide",
                    headers={"X-Employee-Id": ids[1]["employee_id"]},
                    json={"decision": "approve"},
                ).status_code
                == 200
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                policies = client.get("/settings/country-policies").json()[
                    "active_policies"
                ]
                if policies:
                    break
                time.sleep(0.2)
            assert len(policies) == 1
        scheduler = next(p for p in state["processes"] if p["name"] == "scheduler")
        import psutil

        assert psutil.Process(scheduler["pid"]).create_time() == scheduler["born"]
        os.kill(scheduler["pid"], signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=35)
        assert process.returncode != 0
        assert stderr == ""
        final = json.loads(stdout.splitlines()[-1])
        assert final["reason"] == "child_exited"
        assert final["cleanup_errors"] == []
        docker_client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
        try:
            assert (
                docker_client.containers.list(
                    all=True,
                    filters={"label": "tradeos.controlled.owner=" + state["owner"]},
                )
                == []
            )
        finally:
            docker_client.close()
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=40)


def test_migration_failure_keeps_primary_and_cleans_files_when_docker_close_fails(
    tmp_path: Path,
) -> None:
    script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from controlled_web_supervisor import Supervisor
from infra.controlled.resources import OwnedContainers
original_run = Supervisor.run_once
original_close = OwnedContainers.close
def fail_migration(self, name, command, environment):
    if name == 'migration': command = [sys.executable, '-c', 'raise SystemExit(9)']
    return original_run(self, name, command, environment)
def close_fails(self):
    original_close(self)
    raise RuntimeError('private-error-marker-must-not-appear')
Supervisor.run_once = fail_migration
OwnedContainers.close = close_fails
from run_web_core_controlled import main
sys.argv = ['run', '--directory', DIRECTORY]
raise SystemExit(main())
""".replace("DIRECTORY", repr(str(tmp_path)))
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode != 0
    final = json.loads(result.stdout.splitlines()[-1])
    assert final["reason"] == "migration_failed"
    assert final["cleanup_errors"]
    assert "private-error-marker" not in result.stdout + result.stderr
    assert not (Path(final["directory"]) / "config.json").exists()


def test_leader_exit_before_first_snapshot_cleans_unrecorded_child(
    tmp_path: Path,
) -> None:
    import time

    import psutil

    from infra.controlled.resources import OwnedProcess

    code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open(sys.argv[1],'w').write(str(p.pid))"
    process = OwnedProcess.start(
        "early-exit",
        [sys.executable, "-c", code, str(tmp_path / "pid")],
        cwd=ROOT,
        environ={"PATH": os.defpath},
    )
    child = None
    try:
        assert process.process.wait(timeout=5) == 0
        child = psutil.Process(int((tmp_path / "pid").read_text()))
        birth = child.create_time()
        assert not process.children
        assert os.getpgid(child.pid) == process.process.pid
        process.stop(timeout=2)

        deadline = time.monotonic() + 2
        while child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "unrecorded owner child survived stop"
            time.sleep(0.01)
    finally:
        if child is not None and child.is_running() and child.create_time() == birth:
            child.kill()
        process.stop(timeout=2)


def test_anchor_handshake_failure_prevents_exec_and_reclaims_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import psutil

    from infra.controlled import resources
    from infra.controlled.config import ControlledError

    started = []
    original = subprocess.Popen
    original_recv = resources.socket.socket.recv

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        started.append(process)
        return process

    def reject(_socket, _size):
        original_recv(_socket, _size)
        raise TimeoutError()

    monkeypatch.setattr(resources.subprocess, "Popen", start)
    monkeypatch.setattr(resources.socket.socket, "recv", reject)
    marker = tmp_path / "must-not-exec"
    with pytest.raises(ControlledError, match="process_handshake_failed"):
        resources.OwnedProcess.start(
            "handshake-failure",
            [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ],
            cwd=ROOT,
            environ={"PATH": os.defpath},
        )
    assert not marker.exists()
    assert started[0].poll() is not None
    deadline = time.monotonic() + 5
    while True:
        live = []
        for candidate in psutil.process_iter():
            try:
                if (
                    os.getpgid(candidate.pid) == started[0].pid
                    and candidate.status() != psutil.STATUS_ZOMBIE
                ):
                    live.append(candidate.pid)
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass
        if not live:
            break
        assert time.monotonic() < deadline, "handshake failure leaked owner group"
        time.sleep(0.02)


def test_anchor_does_not_retain_business_listener_or_pipe(tmp_path: Path) -> None:
    import select
    import time

    import psutil

    from infra.controlled.resources import OwnedProcess

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    read_fd, write_fd = os.pipe()
    process = OwnedProcess.start(
        "fd-ownership",
        [sys.executable, "-c", "pass"],
        cwd=ROOT,
        environ={"PATH": os.defpath, "CONTROLLED_SYNTHETIC_MARKER": "not-a-secret"},
        pass_fds=(listener.fileno(), write_fd),
    )
    listener.close()
    os.close(write_fd)
    try:
        assert process.process.wait(timeout=5) == 0
        anchor = psutil.Process(process.anchor_pid)
        assert anchor.is_running()
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))
        assert select.select([read_fd], [], [], 2)[0]
        assert os.read(read_fd, 1) == b""
        process.stop(timeout=2)
        deadline = time.monotonic() + 2
        while anchor.is_running() and anchor.status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "owner anchor survived stop"
            time.sleep(0.02)
    finally:
        os.close(read_fd)
        process.stop(timeout=2)


def test_short_lived_bootstrap_releases_anchor_before_owner_forgets_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    from infra.controlled.resources import OwnedProcess
    from scripts.controlled_web_supervisor import Supervisor

    captured = []
    original = OwnedProcess.start

    def start(*args, **kwargs):
        owned = original(*args, **kwargs)
        captured.append(owned)
        return owned

    monkeypatch.setattr(OwnedProcess, "start", start)
    supervisor = Supervisor(ROOT, tmp_path, [])
    try:
        supervisor.run_once("short", [sys.executable, "-c", "pass"], supervisor.environ)
        assert not supervisor.processes
        anchor = (
            psutil.Process(captured[0].anchor_pid)
            if psutil.pid_exists(captured[0].anchor_pid)
            else None
        )
        assert anchor is None or anchor.status() == psutil.STATUS_ZOMBIE
    finally:
        for owned in captured:
            owned.stop(timeout=2)


def test_anchor_survives_term_until_forced_child_cleanup(tmp_path: Path) -> None:
    import time

    import psutil

    from infra.controlled.config import ControlledError
    from infra.controlled.resources import OwnedProcess

    child_code = "import signal,pathlib,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).touch(); time.sleep(60)"
    parent_code = "import subprocess,sys,pathlib,time; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\npathlib.Path(sys.argv[3]).write_text(str(p.pid))"
    process = OwnedProcess.start(
        "forced-child",
        [
            sys.executable,
            "-c",
            parent_code,
            child_code,
            str(tmp_path / "ready"),
            str(tmp_path / "pid"),
        ],
        cwd=ROOT,
        environ={"PATH": os.defpath},
    )
    try:
        assert process.process.wait(timeout=5) == 0
        child = psutil.Process(int((tmp_path / "pid").read_text()))
        anchor = psutil.Process(process.anchor_pid)
        with pytest.raises(ControlledError, match="process_forced_stop"):
            process.stop(timeout=1)
        deadline = time.monotonic() + 2
        for member in (child, anchor):
            while member.is_running() and member.status() != psutil.STATUS_ZOMBIE:
                assert time.monotonic() < deadline, "forced cleanup left owner alive"
                time.sleep(0.02)
    finally:
        process.stop(timeout=1)


def test_dead_process_leader_does_not_orphan_recorded_children(tmp_path: Path) -> None:
    import signal
    import time

    import psutil

    from infra.controlled.resources import OwnedProcess

    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)"
    process = OwnedProcess.start(
        "tree",
        [sys.executable, "-c", code, str(tmp_path / "pid")],
        cwd=ROOT,
        environ={"PATH": os.defpath},
    )
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_id = int((tmp_path / "pid").read_text())
        child = psutil.Process(child_id)
        birth = child.create_time()
        process.public()
        os.kill(process.process.pid, signal.SIGKILL)
        process.process.wait(timeout=5)
        process.stop(timeout=2)
        assert (
            not psutil.pid_exists(child_id)
            or psutil.Process(child_id).status() == psutil.STATUS_ZOMBIE
        )
    finally:
        if process.process.poll() is None:
            process.stop(timeout=2)
        if (
            psutil.pid_exists(child_id)
            and psutil.Process(child_id).create_time() == birth
        ):
            psutil.Process(child_id).kill()


def test_term_completes_current_cycle_without_constructing_real_providers(
    tmp_path: Path,
) -> None:
    import time

    provider_guard = """
import runpy, sys
from connectors.openai import OpenAIJsonModelClient
from connectors.gmail.transport import GmailApiHttpTransport
from connectors.dns_auth.client import DnsPythonAsyncResolver
from connectors.tavily.transport import TavilySearchApiTransport
from connectors.web_search.transport import SafePublicPageHttpTransport
from connectors.hunter.transport import HunterApiHttpTransport
def deny(*args, **kwargs):
    raise AssertionError('real_provider_constructed')
for cls in (OpenAIJsonModelClient, GmailApiHttpTransport, DnsPythonAsyncResolver, TavilySearchApiTransport, SafePublicPageHttpTransport, HunterApiHttpTransport):
    cls.__init__ = deny
"""
    delay = """
import asyncio
from pathlib import Path
import apps.scheduler_worker.main as scheduler_main
original_cycle = scheduler_main._run_cycle
async def cycle(*args, **kwargs):
    Path(MARKER).write_text('started')
    await asyncio.sleep(2)
    await original_cycle(*args, **kwargs)
    Path(MARKER).write_text('completed')
scheduler_main._run_cycle = cycle
""".replace("MARKER", repr(str(tmp_path / "cycle")))
    driver = (
        """
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from infra.controlled.resources import OwnedProcess
original_start = OwnedProcess.start
def start(name, command, **kwargs):
    if name in {'api', 'scheduler', 'identities'}:
        code = GUARD
        if name == 'scheduler': code += DELAY
        code += chr(10) + 'runpy.run_module(' + repr(command[2]) + ', run_name="__main__")'
        command = [command[0], '-c', code, *command[3:]]
    return original_start(name, command, **kwargs)
OwnedProcess.start = staticmethod(start)
from run_web_core_controlled import main
sys.argv = ['run', '--directory', DIRECTORY]
raise SystemExit(main())
""".replace("GUARD", repr(provider_guard))
        .replace("DELAY", repr(delay))
        .replace("DIRECTORY", repr(str(tmp_path)))
    )
    # 内联驱动只延迟cycle并禁止真实Provider构造；核心域、锁与循环全部保留。
    process = subprocess.Popen(
        [sys.executable, "-c", driver],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 90
        state = {}
        while time.monotonic() < deadline:
            paths = list(tmp_path.glob("*/status.json"))
            if paths:
                state = json.loads(paths[0].read_text())
                if (
                    state["status"] == "ready"
                    and (tmp_path / "cycle").read_text() == "started"
                ):
                    break
            assert process.poll() is None, process.communicate()[0]
            time.sleep(0.1)
        assert state["status"] == "ready"
        process.terminate()
        stdout, stderr = process.communicate(timeout=40)
        assert process.returncode == 0, stdout
        assert not stderr
        assert (tmp_path / "cycle").read_text() == "completed"
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=40)


async def test_provider_mail_is_tenant_filtered_even_in_same_scene_file(
    tmp_path: Path,
) -> None:
    from infra.controlled.providers import ControlledGmailTransport

    first = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-a")
    second = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-b")
    await first.send(
        token="ignored",
        raw_message=b"Message-ID: <same@controlled.test>\r\nX-TradeOS-Idempotency-V1: same\r\n\r\nHello",
    )
    assert (
        await second.search(
            token="ignored", message_id="same@controlled.test", header="same"
        )
        is None
    )


def test_each_process_startup_failure_cleans_owned_resources(tmp_path: Path) -> None:
    import docker

    for failed_stage in ("migration", "identities", "api", "scheduler", "web"):
        driver = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
from infra.controlled.resources import OwnedProcess
original = OwnedProcess.start
def start(name, command, **kwargs):
    if name == FAILED_STAGE:
        command = [sys.executable, '-c', 'raise SystemExit(9)']
    return original(name, command, **kwargs)
OwnedProcess.start = staticmethod(start)
from run_web_core_controlled import main
sys.argv = ['run', '--directory', DIRECTORY]
raise SystemExit(main())
""".replace("FAILED_STAGE", repr(failed_stage)).replace(
            "DIRECTORY", repr(str(tmp_path))
        )
        result = subprocess.run(
            [sys.executable, "-c", driver],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"], "PYTHON_DOTENV_DISABLED": "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=70,
        )
        assert result.returncode != 0
        final = json.loads(result.stdout.splitlines()[-1])
        assert final["reason"] == (
            failed_stage + "_failed"
            if failed_stage in {"migration", "identities"}
            else "child_exited"
        )
        assert final["cleanup_errors"] == []
        state = json.loads((Path(final["directory"]) / "status.json").read_text())
        assert not (Path(final["directory"]) / "config.json").exists()
        client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
        try:
            assert (
                client.containers.list(
                    all=True,
                    filters={"label": "tradeos.controlled.owner=" + state["owner"]},
                )
                == []
            )
        finally:
            client.close()


def test_container_cleanup_never_removes_other_owner() -> None:
    import secrets
    from uuid import uuid4

    from infra.controlled.resources import OwnedContainers

    first = OwnedContainers(uuid4().hex)
    other = OwnedContainers(uuid4().hex)
    try:
        container_id, _ = first.create(
            "pgvector/pgvector:pg16",
            port=5432,
            environment={"POSTGRES_PASSWORD": secrets.token_hex(32)},
        )
        other.ids.append(container_id)
        assert other.close() == ["container_cleanup_unknown"]
        assert first.verify(container_id).id == container_id
    finally:
        assert first.close() == []


async def test_provider_calls_are_persistent_and_not_hidden_by_message_dedup(
    tmp_path: Path,
) -> None:
    from infra.controlled.providers import ControlledGmailTransport

    first = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-a")
    raw = b"Message-ID: <calls@controlled.test>\r\nX-TradeOS-Idempotency-V1: calls\r\n\r\nHello"
    await first.send(token="ignored", raw_message=raw)
    await first.send(token="ignored", raw_message=raw)
    second = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-a")
    assert [call.operation for call in await second.list_calls()] == ["send", "send"]
    other = ControlledGmailTransport(tmp_path / "mail.sqlite", tenant_id="tenant-b")
    assert await other.list_calls() == ()
