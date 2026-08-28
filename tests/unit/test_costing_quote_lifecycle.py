"""真实可终止子进程/loopback保护；Docker边界单独替换，不伪称真实业务链。"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e import costing_quote_lifecycle as lifecycle


@pytest.fixture
def controlled_lifecycle(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(lifecycle, "TOTAL_SECONDS", {"integration": 2, "browser": 2, "visual": 2})
    monkeypatch.setattr(lifecycle, "CLEANUP_SECONDS", .8)
    monkeypatch.setattr(lifecycle, "TERM_SECONDS", .15)
    cleaned = []

    async def cleanup(owner, deadline):
        cleaned.append((owner, time.monotonic() < deadline))
        return True

    monkeypatch.setattr(lifecycle, "_cleanup_owned", cleanup)
    return tmp_path, cleaned


def fixture_command(monkeypatch, scenario, artifacts):
    monkeypatch.setattr(lifecycle, "_child_command", lambda mode, owner, *_: [
        sys.executable, str(Path(__file__).resolve()), "--fixture", scenario,
        str(artifacts / ("t10-" + owner)),
    ])


@pytest.mark.parametrize("scenario", ["blocked_initialization", "partial_failure", "normal"])
async def test_watchdog_owns_separate_group_and_closes_real_port(controlled_lifecycle, monkeypatch, scenario):
    artifacts, cleaned = controlled_lifecycle
    fixture_command(monkeypatch, scenario, artifacts)
    sentinel = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(20)", start_new_session=True)
    started = time.monotonic()
    try:
        result = await lifecycle.run_supervised("browser")
        assert time.monotonic() - started < 2.5
        assert (result.code == 0) is (scenario == "normal")
        assert result.cleanup_verified
        assert cleaned and all(before_deadline for _, before_deadline in cleaned)
        evidence = json.loads((result.artifacts / "fixture.json").read_text())
        assert evidence["child_pgid"] != evidence["parent_pgid"]
        assert len(result.observed) >= 2
        for pid in (evidence["pid"], evidence["child_pid"]):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", evidence["port"])) != 0
        assert sentinel.returncode is None
        assert "private-fixture-log" not in result.output
    finally:
        sentinel.terminate()
        await asyncio.wait_for(sentinel.wait(), 2)


async def test_cleanup_unknown_cannot_be_green(controlled_lifecycle, monkeypatch):
    artifacts, _ = controlled_lifecycle
    fixture_command(monkeypatch, "normal", artifacts)

    async def unknown(*_args):
        return False

    monkeypatch.setattr(lifecycle, "_cleanup_owned", unknown)
    result = await lifecycle.run_supervised("browser")
    assert result.code != 0 and not result.cleanup_verified
    assert "t10_cleanup=unknown" in result.output


async def test_supervisor_preserves_cancellation_after_bounded_cleanup(controlled_lifecycle, monkeypatch):
    artifacts, cleaned = controlled_lifecycle
    fixture_command(monkeypatch, "blocked_initialization", artifacts)
    task = asyncio.create_task(lifecycle.run_supervised("browser"))
    for _ in range(100):
        if list(artifacts.glob("t10-*/fixture.json")):
            break
        await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned
    evidence = json.loads(next(artifacts.glob("t10-*/fixture.json")).read_text())
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", evidence["port"])) != 0


@pytest.mark.parametrize("scenario", ["chromium_normal", "chromium_forced"])
async def test_watchdog_tracks_actual_chromium_groups_and_keeps_unrelated_process(
    controlled_lifecycle, monkeypatch, scenario,
):
    artifacts, _ = controlled_lifecycle
    monkeypatch.setattr(lifecycle, "TOTAL_SECONDS", {"browser": 5})
    monkeypatch.setattr(lifecycle, "CLEANUP_SECONDS", 1)
    fixture_command(monkeypatch, scenario, artifacts)
    sentinel = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(20)", start_new_session=True)
    try:
        result = await lifecycle.run_supervised("browser")
        assert (result.code == 0) is (scenario == "chromium_normal")
        assert result.cleanup_verified
        chromium = json.loads((result.artifacts / "chromium.json").read_text())
        observed = {row["pid"]: row for row in result.observed}
        assert chromium and all(item["pid"] in observed for item in chromium)
        assert any(item["pgid"] != observed[chromium[0]["parent"]]["pgid"] for item in chromium)
        for item in chromium:
            with pytest.raises(ProcessLookupError):
                os.kill(item["pid"], 0)
        assert sentinel.returncode is None
        print("chromium_case=" + scenario + ";owned=" + str(len(chromium))
              + ";observed=" + str(len(observed)) + ";groups="
              + str(len({row["pgid"] for row in result.observed})) + ";sentinel_alive=true;cleanup=verified")
    finally:
        sentinel.terminate()
        await asyncio.wait_for(sentinel.wait(), 2)


async def test_visual_parent_sigterm_uses_bounded_graceful_cleanup(controlled_lifecycle, monkeypatch):
    artifacts, cleaned = controlled_lifecycle
    monkeypatch.setattr(lifecycle, "TERM_SECONDS", .5)
    fixture_command(monkeypatch, "visual_stop", artifacts)

    async def request_stop():
        while not list(artifacts.glob("t10-*/fixture.json")):
            await asyncio.sleep(.01)
        await asyncio.sleep(.1)
        os.kill(os.getpid(), signal.SIGTERM)

    stop_task = asyncio.create_task(request_stop())
    result = await lifecycle.run_supervised("visual")
    await stop_task
    assert result.code == 0 and result.cleanup_verified and cleaned


@pytest.mark.parametrize("failure", ["wrong_owner", "wrong_name", "daemon_timeout"])
async def test_fallback_refuses_uncertain_docker_ownership(monkeypatch, failure):
    owner, commands = "a" * 32, []

    async def docker(arguments, _deadline):
        commands.append(arguments)
        if failure == "daemon_timeout":
            raise TimeoutError
        if arguments[1] == "ls":
            return 0, "b" * 12
        if arguments[1] == "inspect":
            name = "unrelated" if failure == "wrong_name" else lifecycle.resource_names(owner)[
                "network" if arguments[0] == "network" else ("api" if len(commands) == 2 else "pg")]
            label = "c" * 32 if failure == "wrong_owner" else owner
            return 0, "b" * 64 + " " + name + " " + label
        pytest.fail("不明确资源不得删除")

    monkeypatch.setattr(lifecycle, "_docker", docker)
    assert not await lifecycle._cleanup_owned(owner, time.monotonic() + 1)
    assert not any("rm" in command for command in commands)


async def test_t10_readiness_uses_remaining_budget():
    from types import SimpleNamespace

    from tests.e2e.costing_quote_stack import _wait_for_ready

    listener = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
    try:
        port = listener.sockets[0].getsockname()[1]
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await _wait_for_ready(f"http://127.0.0.1:{port}/", SimpleNamespace(
                process=SimpleNamespace(poll=lambda: None)), started + .2)
        assert time.monotonic() - started < .5
    finally:
        listener.close()
        await listener.wait_closed()


@pytest.mark.parametrize("mode", ["integration", "browser", "visual"])
def test_fixed_child_entry_cannot_reenter_pytest(mode):
    command = lifecycle._child_command(mode, "a" * 32, time.monotonic() + 10, time.monotonic() + 55)
    assert command[:3] == [sys.executable, "-m", "tests.e2e.costing_quote_lifecycle"]
    assert command[3:6] == ["--child", "--mode", mode]
    assert not any("pytest" in value for value in command)


@pytest.mark.parametrize("mode", ["../browser", "browser;command", "relay", "arbitrary"])
async def test_invalid_mode_never_launches_process(mode):
    with pytest.raises(ValueError):
        await lifecycle.run_supervised(mode)


async def test_module_child_entry_shares_canonical_ownership():
    script = """
import asyncio, os, runpy, sys, time
from tests.e2e import costing_quote_lifecycle as lifecycle
async def check(mode):
    assert mode == 'browser'
    assert lifecycle.active_run().owner == 'a' * 32
    print('canonical_ownership=verified')
    return 0
lifecycle._child_work = check
deadline = time.monotonic() + 60
sys.argv = ['fixed-entry', '--child', '--mode', 'browser', '--owner', 'a' * 32,
    '--work-until', str(deadline - 45), '--finish-until', str(deadline),
    '--parent-pid', str(os.getppid())]
runpy.run_module('tests.e2e.costing_quote_lifecycle', run_name='__main__')
"""
    code, output = await lifecycle._capture([sys.executable, "-c", script], time.monotonic() + 3)
    assert code == 0 and output == "canonical_ownership=verified\n"


def fixture_main(scenario, artifacts):
    """仅本测试文件私有夹具；不通过真实监督入口接受自定义command。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)"],
        start_new_session=True)
    evidence = {"pid": os.getpid(), "parent_pgid": os.getpgrp(), "child_pid": child.pid,
                "child_pgid": os.getpgid(child.pid), "port": listener.getsockname()[1]}
    (artifacts / "fixture.json").write_text(json.dumps(evidence))
    (artifacts / "ports.json").write_text(json.dumps([evidence["port"]]))
    print("private-fixture-log", flush=True)
    if scenario.startswith("chromium_"):
        async def chromium_case():
            from playwright.async_api import async_playwright

            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(handle_sigterm=False, handle_sigint=False)
                await browser.new_page()
                session = await browser.new_browser_cdp_session()
                processes = (await session.send("SystemInfo.getProcessInfo"))["processInfo"]
                (artifacts / "chromium.json").write_text(json.dumps([
                    {"pid": int(item["id"]), "pgid": os.getpgid(int(item["id"])), "parent": os.getpid()}
                    for item in processes
                ]))
                if scenario == "chromium_forced":
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    await asyncio.sleep(20)
                await asyncio.sleep(.2)
                await browser.close()
        asyncio.run(chromium_case())
    if scenario == "blocked_initialization":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(20)
    if scenario == "visual_stop":
        stopped = False

        def stop(*_args):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        while not stopped:
            time.sleep(.01)
    time.sleep(.3)
    if scenario == "partial_failure":
        return 2
    child.kill()
    child.wait(timeout=1)
    listener.close()
    print("t10_child_exit=verified", flush=True)
    return 0


if __name__ == "__main__":
    assert sys.argv[1] == "--fixture"
    raise SystemExit(fixture_main(sys.argv[2], Path(sys.argv[3])))
