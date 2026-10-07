"""真实 owned 进程的模型停机排空；不启动数据库或访问 Provider。"""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra.controlled.config import ControlledError
from infra.controlled.resources import OwnedProcess
from infra.pilot.config import PilotError
from infra.pilot.resources import PilotProfile
from scripts.pilot_web_supervisor import PilotSupervisor
from tests.unit.test_pilot_profile import make_config

ROOT = Path(__file__).resolve().parents[2]


def supervisor_without_storage(tmp_path, monkeypatch, *, model=True):
    profile = object.__new__(PilotProfile)
    profile.path = tmp_path / "profile"
    profile.config = make_config(tmp_path)
    profile.client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(profile, "verify_all", dict)
    supervisor = object.__new__(PilotSupervisor)
    supervisor.profile = profile
    supervisor.processes = []
    supervisor.owned = True
    supervisor.model_settings = tmp_path / "model.json" if model else None
    return supervisor


def start_process(tmp_path, name, code):
    ready = tmp_path / f"{name}.ready"
    process = OwnedProcess.start(
        name,
        [sys.executable, "-c", code, str(tmp_path), name],
        cwd=ROOT,
        environ={"PATH": os.defpath},
    )
    deadline = time.monotonic() + 5
    while not ready.exists():
        if process.process.poll() is not None or time.monotonic() >= deadline:
            process.stop(timeout=1)
            pytest.fail("受控进程未完成初始化")
        time.sleep(0.01)
    return process


DRAIN_CODE = """
import pathlib, signal, sys, time
root, name = pathlib.Path(sys.argv[1]), sys.argv[2]
stopping = False
def stop(signum, frame):
    global stopping
    stopping = True
    (root / (name + '.term')).write_text(str(time.monotonic()))
signal.signal(signal.SIGTERM, stop)
(root / (name + '.ready')).touch()
while not stopping:
    time.sleep(.01)
if name == 'scheduler':
    for _ in range(2):
        time.sleep(6)
        with (root / 'completed').open('a') as output:
            output.write('finished\n')
(root / (name + '.done')).write_text(str(time.monotonic()))
""".replace("'finished\n'", "'finished\\n'")


IGNORE_CODE = """
import pathlib, signal, sys, time
root, name = pathlib.Path(sys.argv[1]), sys.argv[2]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
(root / (name + '.ready')).touch()
time.sleep(60)
"""


def cleanup(processes):
    for process in processes:
        try:
            process.stop(timeout=1)
        except ControlledError:
            if not process.closed:
                raise


def test_model_stop_finishes_serial_inflight_work_and_signals_every_process(
    tmp_path, monkeypatch
):
    supervisor = supervisor_without_storage(tmp_path, monkeypatch)
    try:
        for name in ("api", "scheduler", "notification"):
            supervisor.processes.append(start_process(tmp_path, name, DRAIN_CODE))
        supervisor.close()
        assert (tmp_path / "completed").read_text().splitlines() == [
            "finished",
            "finished",
        ]
        scheduler_done = float((tmp_path / "scheduler.done").read_text())
        assert float((tmp_path / "api.term").read_text()) < scheduler_done
        assert all(p.closed and p.process.returncode == 0 for p in supervisor.processes)
        assert supervisor.profile.runtime_state().status == "stopped"
    finally:
        cleanup(supervisor.processes)


def test_model_drain_deadline_is_shared_and_forced_stop_is_not_success(
    tmp_path, monkeypatch
):
    import scripts.pilot_web_supervisor as module

    monkeypatch.setattr(module, "MODEL_DRAIN_SECONDS", 1, raising=False)
    supervisor = supervisor_without_storage(tmp_path, monkeypatch)
    try:
        for name in ("api", "scheduler", "notification"):
            supervisor.processes.append(start_process(tmp_path, name, IGNORE_CODE))
        started = time.monotonic()
        with pytest.raises(PilotError, match="application_stop_failed"):
            supervisor.close()
        assert time.monotonic() - started < 5, "排空期限不能为每个进程重新开始"
        assert all(
            p.closed and p.process.poll() is not None for p in supervisor.processes
        )
        assert supervisor.profile.runtime_state().status == "failed"
    finally:
        cleanup(supervisor.processes)


def test_cli_stop_accepts_longer_wait_without_changing_legacy_default():
    from scripts.run_web_pilot import parser

    assert parser().parse_args(["stop", "--profile", "/unused"]).timeout_seconds == 40
    assert (
        parser()
        .parse_args(["stop", "--profile", "/unused", "--timeout-seconds", "420"])
        .timeout_seconds
        == 420
    )
    with pytest.raises(PilotError):
        parser().parse_args(["stop", "--profile", "/unused", "--timeout-seconds", "0"])


SUPERVISOR_CODE = """
import pathlib, signal, sys, time
from infra.pilot.config import PilotConfig, private_write
from infra.pilot.resources import ProcessIdentity, RuntimeState
root, failure = pathlib.Path(sys.argv[1]), sys.argv[2] == 'failed'
config = PilotConfig.read(root / 'config.json')
def publish(status):
    state = RuntimeState(owner=config.owner, supervisor=ProcessIdentity.current(),
                         status=status, reason='operation_failed')
    private_write(root / 'runtime.json', state.model_dump_json().encode())
stopping = False
def stop(signum, frame):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop if failure else signal.SIG_IGN)
publish('running')
(root / 'ready').touch()
while not stopping:
    time.sleep(.01)
publish('failed')
"""


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_cli_stop_never_erases_failed_or_live_supervisor(
    tmp_path, monkeypatch, outcome
):
    profile = supervisor_without_storage(tmp_path, monkeypatch).profile
    process = subprocess.Popen(
        [sys.executable, "-c", SUPERVISOR_CODE, str(profile.path), outcome],
        cwd=ROOT,
        env={"PATH": os.defpath, "PYTHONPATH": str(ROOT)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not (profile.path / "ready").exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        reason = (
            "application_stop_failed"
            if outcome == "failed"
            else "application_stop_timeout"
        )
        with pytest.raises(PilotError, match=reason):
            profile.stop(timeout=1)
        assert profile.runtime_state().status == (
            "failed" if outcome == "failed" else "running"
        )
        if outcome == "timeout":
            assert process.poll() is None
            with pytest.raises(PilotError, match="profile_apps_running"):
                profile.require_no_processes()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_parent_and_child_cleanup_share_the_same_drain_window(tmp_path):
    code = """
import pathlib, signal, subprocess, sys, time
root, name = pathlib.Path(sys.argv[1]), sys.argv[2]
child_code = 'import signal,sys,pathlib,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).touch(); time.sleep(60)'
child_ready = root / 'child-ready'
subprocess.Popen([sys.executable, '-c', child_code, str(child_ready)])
stopping = False
def stop(signum, frame):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
while not child_ready.exists():
    time.sleep(.01)
(root / (name + '.ready')).touch()
while not stopping:
    time.sleep(.01)
time.sleep(2.5)
"""
    process = start_process(tmp_path, "parent-with-child", code)
    try:
        started = time.monotonic()
        with pytest.raises(ControlledError, match="process_forced_stop"):
            process.stop(timeout=3)
        assert time.monotonic() - started < 4.5, "子进程不能重复获得整个排空期限"
        assert process.closed
    finally:
        cleanup([process])
