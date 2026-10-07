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
        assert result.cleanup_verified is (scenario == "normal")
        if scenario != "normal":
            assert "t10_cleanup=unknown" in result.output
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


async def test_unobserved_orphan_between_snapshots_cannot_claim_cleanup_verified(
    controlled_lifecycle, monkeypatch,
):
    artifacts, _ = controlled_lifecycle
    script = """
import json, pathlib, subprocess, sys, time
path = pathlib.Path(sys.argv[1])
while not (path / 'spawn').exists():
    time.sleep(.001)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True)
(path / 'orphan.json').write_text(json.dumps({'pid': child.pid}))
"""
    monkeypatch.setattr(lifecycle, "_child_command", lambda mode, owner, *_: [
        sys.executable, "-c", script, str(artifacts / ("t10-" + owner)),
    ])
    refresh = lifecycle.ProcessTree.refresh
    snapshots = 0

    async def observe(tree, deadline):
        nonlocal snapshots
        if snapshots:
            while True:
                try:
                    os.kill(tree.pid, 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(.001)
        await refresh(tree, deadline)
        snapshots += 1
        if snapshots == 1:
            assert tree.pid in tree.observed
            next(artifacts.glob("t10-*")).joinpath("spawn").touch()

    monkeypatch.setattr(lifecycle.ProcessTree, "refresh", observe)
    sentinel = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(20)", start_new_session=True)
    orphan = None
    try:
        result = await lifecycle.run_supervised("browser")
        orphan = json.loads((result.artifacts / "orphan.json").read_text())["pid"]
        os.kill(orphan, 0)
        assert orphan not in {row["pid"] for row in result.observed}
        assert sentinel.returncode is None
        print("unobserved_orphan_alive=true;cleanup_verified=" + str(result.cleanup_verified).lower())
        assert result.code != 0
        assert not result.cleanup_verified
        assert "t10_cleanup=unknown" in result.output
    finally:
        if orphan is not None:
            os.kill(orphan, signal.SIGKILL)
            for _ in range(100):
                try:
                    os.kill(orphan, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail("仅本次孤儿进程未回收")
        assert sentinel.returncode is None
        sentinel.terminate()
        await asyncio.wait_for(sentinel.wait(), 2)


async def test_observed_child_that_changes_process_group_remains_owned(
    controlled_lifecycle, monkeypatch,
):
    artifacts, _ = controlled_lifecycle
    child_script = """
import json, os, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
path.joinpath('child.json').write_text(json.dumps(
    {'pid': os.getpid(), 'initial_pgid': os.getpgrp()}))
while not path.joinpath('change-group').exists():
    time.sleep(.001)
os.setsid()
path.joinpath('changed.json').write_text(json.dumps(
    {'pid': os.getpid(), 'changed_pgid': os.getpgrp(), 'ppid': os.getppid()}))
while True:
    time.sleep(1)
"""
    root_script = """
import pathlib, subprocess, sys, time
path = pathlib.Path(sys.argv[1])
subprocess.Popen([sys.executable, '-c', sys.argv[2], str(path)],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not path.joinpath('changed.json').exists():
    time.sleep(.001)
print('t10_child_exit=verified', flush=True)
"""
    monkeypatch.setattr(lifecycle, "_child_command", lambda mode, owner, *_: [
        sys.executable, "-c", root_script, str(artifacts / ("t10-" + owner)), child_script,
    ])
    refresh = lifecycle.ProcessTree.refresh
    change_allowed = False

    async def observe(tree, deadline):
        nonlocal change_allowed
        if change_allowed:
            path = next(artifacts.glob("t10-*"))
            while not path.joinpath("changed.json").exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(.001)
            while True:
                try:
                    os.kill(tree.pid, 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < deadline
                await asyncio.sleep(.001)
        await refresh(tree, deadline)
        path = next(artifacts.glob("t10-*"))
        child_path = path / "child.json"
        if not change_allowed and child_path.exists():
            child_pid = json.loads(child_path.read_text())["pid"]
            if child_pid in tree.observed:
                path.joinpath("change-group").touch()
                change_allowed = True

    monkeypatch.setattr(lifecycle.ProcessTree, "refresh", observe)
    sentinel = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(20)", start_new_session=True,
    )
    child_pid = None
    try:
        result = await lifecycle.run_supervised("browser")
        initial = json.loads((result.artifacts / "child.json").read_text())
        changed = json.loads((result.artifacts / "changed.json").read_text())
        child_pid = initial["pid"]
        assert changed["pid"] == child_pid
        assert changed["changed_pgid"] != initial["initial_pgid"]
        assert child_pid in {row["pid"] for row in result.observed}
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        print("changed_group_child_reaped=true;cleanup_verified="
              + str(result.cleanup_verified).lower() + ";code=" + str(result.code))
        assert result.code != 0 and not result.cleanup_verified
        assert "t10_cleanup=unknown" in result.output
        assert sentinel.returncode is None
    finally:
        child_cleanup_timed_out = False
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(.01)
            else:
                child_cleanup_timed_out = True
        cleanup_errors = []
        try:
            assert sentinel.returncode is None
        except AssertionError as error:
            cleanup_errors.append(error)
        if sentinel.returncode is None:
            try:
                sentinel.terminate()
            except OSError as error:
                cleanup_errors.append(error)
        try:
            await asyncio.wait_for(sentinel.wait(), 2)
        except (OSError, TimeoutError) as error:
            cleanup_errors.append(error)
        sentinel_still_alive = sentinel.returncode is None
        try:
            os.kill(sentinel.pid, 0)
        except ProcessLookupError:
            sentinel_still_alive = False
        except OSError as error:
            cleanup_errors.append(error)
        else:
            sentinel_still_alive = True
        if sentinel_still_alive and sentinel.returncode is None:
            try:
                sentinel.kill()
            except OSError as error:
                cleanup_errors.append(error)
            try:
                await asyncio.wait_for(sentinel.wait(), 2)
            except (OSError, TimeoutError) as error:
                cleanup_errors.append(error)
        try:
            os.kill(sentinel.pid, 0)
        except ProcessLookupError:
            pass
        except OSError as error:
            cleanup_errors.append(error)
        else:
            cleanup_errors.append(AssertionError("仅本次sentinel兜底后未回收"))
        if child_cleanup_timed_out:
            cleanup_errors.append(AssertionError("仅本次改变进程组的child未回收"))
        if cleanup_errors:
            raise ExceptionGroup("仅本测试创建的进程清理失败", cleanup_errors)


async def test_supervisor_preserves_cancellation_after_bounded_cleanup(controlled_lifecycle, monkeypatch):
    artifacts, cleaned = controlled_lifecycle
    fixture_command(monkeypatch, "blocked_initialization", artifacts)
    task = asyncio.create_task(lifecycle.run_supervised("browser"))
    for _ in range(100):
        if list(artifacts.glob("t10-*/fixture.json")):
            break
        await asyncio.sleep(.01)
    task.cancel("controlled-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("controlled-cancel",)
    assert any("cleanup_unknown" in note for note in caught.value.__notes__)
    assert cleaned
    evidence = json.loads(next(artifacts.glob("t10-*/fixture.json")).read_text())
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", evidence["port"])) != 0


async def test_process_tree_waits_for_exit_without_reaping_asyncio_children():
    """只等待已观察身份；退出码由asyncio自己的child watcher回收。"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(20)", start_new_session=True,
    )
    tree = lifecycle.ProcessTree(child.pid)
    deadline = time.monotonic() + 2
    try:
        await tree.refresh(deadline)
        assert child.pid in tree.live
        child.terminate()
        assert await tree.wait_reaped(deadline)
        assert await child.wait() == -signal.SIGTERM
        assert not tree.live and not tree.unreaped
    finally:
        if child.returncode is None:
            child.kill()
        await asyncio.wait_for(child.wait(), 2)


async def test_process_tree_does_not_treat_zombie_as_live_or_verified():
    """真实已退出未wait的子进程仍占PID；不能靠kill(0)证明运行或清理成功。"""
    child = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", "import time; time.sleep(20)"], start_new_session=True,
    )
    tree = lifecycle.ProcessTree(child.pid)
    try:
        await tree.refresh(time.monotonic() + 2)
        assert child.pid in tree.live
        child.kill()
        deadline = time.monotonic() + 2
        while child.pid not in tree.unreaped:
            await tree.refresh(deadline)
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        assert child.pid not in tree.live
        assert not await tree.wait_reaped(time.monotonic() + .2)
        assert child.pid in tree.unreaped
        child.wait(timeout=1)
        assert await tree.wait_reaped(time.monotonic() + 2)
        assert not tree.live and not tree.unreaped
    finally:
        child.kill()
        child.wait(timeout=1)


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
        assert result.cleanup_verified is (scenario == "chromium_normal")
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
              + str(len({row["pgid"] for row in result.observed})) + ";sentinel_alive=true;known_pids_gone=true;cleanup="
              + ("verified" if result.cleanup_verified else "unknown"))
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


@pytest.mark.parametrize("scenario", ["missing_receipt", "duplicate_receipt", "nonzero_receipt"])
async def test_incomplete_terminal_receipt_cannot_prove_cleanup(controlled_lifecycle, monkeypatch, scenario):
    artifacts, _ = controlled_lifecycle
    fixture_command(monkeypatch, scenario, artifacts)
    result = await lifecycle.run_supervised("browser")
    assert result.code != 0 and not result.cleanup_verified
    assert "t10_cleanup=unknown" in result.output
    evidence = json.loads((result.artifacts / "fixture.json").read_text())
    for pid in (evidence["pid"], evidence["child_pid"]):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", evidence["port"])) != 0


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


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_host_requires_explicit_isolated_child_marker(monkeypatch, platform):
    from tests.integration import test_costing_quote_closed_loop as closed_loop

    monkeypatch.setattr(closed_loop.sys, "platform", platform)
    monkeypatch.delenv("TRADEOS_T10_ISOLATED_CHILD", raising=False)
    assert not closed_loop.runs_in_isolated_child()
    monkeypatch.setenv("TRADEOS_T10_ISOLATED_CHILD", "not-a-child")
    assert not closed_loop.runs_in_isolated_child()
    monkeypatch.setenv("TRADEOS_T10_ISOLATED_CHILD", "1")
    assert closed_loop.runs_in_isolated_child()


def test_fixed_linux_entry_marks_child_without_inheriting_environment(monkeypatch):
    from types import SimpleNamespace

    from tests.e2e import costing_quote_server as server

    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setenv("PRIVATE_FIXTURE_VALUE", "must-not-be-inherited")
    assert server.integration("controlled-connection") == 0
    command, environment = calls[0]
    assert command[1:4] == [
        "-m", "pytest", "tests/integration/test_costing_quote_closed_loop.py",
    ]
    assert environment["TRADEOS_T10_ISOLATED_CHILD"] == "1"
    assert set(environment) == {
        "PATH", "TEST_DATABASE_URL", "TRADEOS_T10_ISOLATED_CHILD", "PYTHON_DOTENV_DISABLED",
    }


def test_costing_image_uses_verified_dependency_artifact(monkeypatch):
    from tests.e2e import costing_quote_stack as stack

    calls = []
    monkeypatch.setattr(stack, "audited_dependency_image_id", lambda: "audited-artifact")

    def build(image, **kwargs):
        calls.append((image, kwargs))
        return "source-artifact"

    monkeypatch.setattr(stack, "build_parser_image", build)
    assert stack.current_image.__wrapped__() == "source-artifact"
    assert calls == [("audited-artifact", {
        "chain": True, "quotation": True, "costing_quote": True,
    })]


def test_parser_diagnostic_output_accepts_only_fixed_enumerations():
    from tests.e2e.costing_quote_stack import safe_output

    lines = [
        "t10_parser_status=unavailable;failure=resource",
        "t10_parser_probe=cpu;exit=-24;reason=short",
        "t10_parser_probe=ipc;exit=unknown;reason=unknown",
    ]
    forbidden = [
        "t10_parser_status=unavailable;failure=private-fixture-value",
        "t10_parser_probe=private-fixture-value;exit=70;reason=short",
        "t10_parser_probe=cpu;exit=70;reason=private-fixture-value",
    ]
    assert safe_output(("\n".join([*lines, *forbidden])).encode()) == "\n".join(lines)


@pytest.mark.parametrize("mode", ["integration", "browser", "visual"])
def test_fixed_child_entry_cannot_reenter_pytest(mode):
    command = lifecycle._child_command(mode, "a" * 32, time.monotonic() + 10, time.monotonic() + 55)
    assert command[:3] == [sys.executable, "-m", "tests.e2e.costing_quote_lifecycle"]
    assert command[3:6] == ["--child", "--mode", mode]
    assert not any("pytest" in value for value in command)


def test_browser_cold_source_build_keeps_full_workflow_and_cleanup_budget():
    """冷源码层可占较长准备时间，仍不得挤占真实表单闭环或45秒回收窗口。"""
    assert lifecycle.TOTAL_SECONDS["browser"] == 360
    assert lifecycle.CLEANUP_SECONDS == 45
    assert lifecycle.TOTAL_SECONDS["browser"] - lifecycle.CLEANUP_SECONDS == 315


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
    if scenario == "missing_receipt":
        return 0
    if scenario == "duplicate_receipt":
        print("t10_child_exit=verified", flush=True)
    print("t10_child_exit=verified", flush=True)
    return 2 if scenario == "nonzero_receipt" else 0


if __name__ == "__main__":
    assert sys.argv[1] == "--fixture"
    raise SystemExit(fixture_main(sys.argv[2], Path(sys.argv[3])))
