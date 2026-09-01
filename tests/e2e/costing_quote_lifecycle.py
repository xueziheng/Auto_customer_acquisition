"""T10宿主监督：固定子入口，准备前计时，保留同一总预算内的清理窗口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

TOTAL_SECONDS = {"integration": 300, "browser": 360, "visual": 900}
CLEANUP_SECONDS = 45
TERM_SECONDS = 5
OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "output/playwright"
ROOT = Path(__file__).resolve().parents[2]
OWNER_LABEL = "tradeos.t10.owner"
_ACTIVE = None


@dataclass(frozen=True)
class OwnedRun:
    owner: str
    work_deadline: float
    deadline: float
    artifacts: Path
    parent_pid: int


@dataclass(frozen=True)
class RunResult:
    code: int
    output: str
    artifacts: Path
    cleanup_verified: bool
    observed: tuple[dict, ...]


def active_run():
    return _ACTIVE


def minimal_env():
    return {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "LANG": "C.UTF-8",
            "PYTHON_DOTENV_DISABLED": "1"}


def resource_names(owner):
    if re.fullmatch(r"[a-f0-9]{32}", owner) is None:
        raise ValueError("invalid_owner")
    return {"network": "t10-" + owner, "pg": "t10-pg-" + owner, "api": "t10-api-" + owner}


def _child_command(mode, owner, work_deadline, deadline):
    if mode not in TOTAL_SECONDS:
        raise ValueError("invalid_mode")
    resource_names(owner)
    return [sys.executable, "-m", "tests.e2e.costing_quote_lifecycle", "--child", "--mode", mode,
            "--owner", owner, "--work-until", str(work_deadline), "--finish-until", str(deadline),
            "--parent-pid", str(os.getpid())]


async def _capture(command, deadline):
    """固定内部ps/Docker命令；任意输出只在内存中解析，不写日志。"""
    remaining = deadline - time.monotonic()
    if remaining <= .05:
        raise TimeoutError
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=minimal_env(), start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=max(.01, remaining - .05))
        if len(output) > 1024 * 1024:
            raise ValueError("diagnostic_too_large")
        return process.returncode, output.decode("utf-8", errors="strict")
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), max(.001, deadline - time.monotonic()))


class ProcessTree:
    """只追踪本次独立session及实际子孙；不按进程名寻找Chromium。"""

    def __init__(self, pid):
        self.pid = pid
        self.observed = {}
        self.live = {}

    async def refresh(self, deadline):
        code, output = await _capture(["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart="], min(deadline, time.monotonic() + 2))
        if code:
            raise RuntimeError("process_inspect_failed")
        rows = {}
        for line in output.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) == 4 and all(value.isdigit() for value in fields[:3]):
                pid, ppid, pgid = map(int, fields[:3])
                rows[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid, "birth": fields[3]}
        owned = {pid for pid, row in rows.items() if pid in self.observed
                 and row["birth"] == self.observed[pid]["birth"]}
        if not self.observed and self.pid in rows:
            owned.add(self.pid)
        changed = True
        while changed:
            additions = {pid for pid, row in rows.items() if row["ppid"] in owned} - owned
            changed = bool(additions)
            owned.update(additions)
        self.live = {pid: rows[pid] for pid in owned}
        self.observed.update(self.live)

    async def kill(self, deadline):
        await self.refresh(deadline)
        for pid in reversed(tuple(self.live)):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _docker(arguments, deadline):
    return await _capture(["docker", *arguments], min(deadline, time.monotonic() + 3))


async def _cleanup_owned(owner, deadline):
    names = resource_names(owner)
    verified = True
    for kind in ("api", "pg", "network"):
        category = "network" if kind == "network" else "container"
        name = names[kind]
        try:
            filters = ["--filter", "name=^" + ("/" if category == "container" else "") + name + "$"]
            command = [category, "ls", *(["-a"] if category == "container" else []), *filters,
                       "--format", "{{.ID}}"]
            code, found = await _docker(command, deadline)
            if code:
                raise RuntimeError("ownership_unknown")
            ids = found.split()
            if not ids:
                continue
            if len(ids) != 1 or re.fullmatch(r"[a-f0-9]{12,64}", ids[0]) is None:
                raise RuntimeError("ownership_unknown")
            labels = ".Labels" if category == "network" else ".Config.Labels"
            template = '{{.Id}} {{.Name}} {{index ' + labels + ' "' + OWNER_LABEL + '"}}'
            code, identity = await _docker([category, "inspect", "--format", template, ids[0]], deadline)
            fields = identity.split()
            if (code or len(fields) != 3 or re.fullmatch(r"[a-f0-9]{64}", fields[0]) is None
                or fields[1].removeprefix("/") != name or fields[2] != owner):
                raise RuntimeError("ownership_mismatch")
            resource_id = fields[0]
            if category == "container":
                # 异常fallback不是runtime成功回执；只回收核对过的本次资源。
                code, _ = await _docker(["container", "rm", "--force", resource_id], deadline)
            else:
                code, _ = await _docker(["network", "rm", resource_id], deadline)
            if code:
                raise RuntimeError("cleanup_failed")
            code, found = await _docker(command, deadline)
            if code or found.strip():
                raise RuntimeError("cleanup_unknown")
        except (OSError, RuntimeError, TimeoutError, ValueError):
            verified = False
    return verified


def _ports_closed(artifacts, deadline):
    path = artifacts / "ports.json"
    if not path.exists():
        return True
    if path.stat().st_size > 1024:
        return False
    ports = json.loads(path.read_text())
    if not isinstance(ports, list) or len(ports) > 4 or any(type(p) is not int or not 0 < p < 65536 for p in ports):
        return False
    for port in ports:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        with socket.socket() as probe:
            probe.settimeout(min(.05, remaining))
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return False
    return True


async def _read_output(stream, lines):
    from tests.e2e.costing_quote_stack import safe_output

    pending = b""
    while chunk := await stream.read(4096):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if len(line) <= 4096:
                decoded = line.decode("utf-8", errors="replace")
                if decoded in {"t10_child_exit=verified", "t10_visual_handoff=ready"}:
                    lines.append(decoded)
                elif filtered := safe_output(line):
                    lines.append(filtered)
        if len(pending) > 8192:
            pending = b""


async def run_supervised(mode):
    """仅固定入口合作式收尾可verified；PPID快照只尽力清理已观察子孙。"""
    if mode not in TOTAL_SECONDS:
        raise ValueError("invalid_mode")
    started = time.monotonic()
    deadline = started + TOTAL_SECONDS[mode]
    work_deadline = deadline - CLEANUP_SECONDS
    owner = uuid4().hex
    artifacts = OUTPUT_ROOT / ("t10-" + owner)
    artifacts.mkdir(parents=True, exist_ok=False)
    lines, observed = [], ()
    process = tree = reader = waiter = None
    cleanup_verified = cooperative_exit = False
    code, primary, reason = 2, None, ""
    stop = asyncio.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for kind in (signal.SIGINT, signal.SIGTERM):
            previous[kind] = signal.getsignal(kind)
            signal.signal(kind, lambda *_: stop.set())
    try:
        process = await asyncio.create_subprocess_exec(*_child_command(mode, owner, work_deadline, deadline),
            cwd=ROOT, env=minimal_env(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        tree = ProcessTree(process.pid)
        reader = asyncio.create_task(_read_output(process.stdout, lines))
        waiter = asyncio.create_task(process.wait())
        handed_off = False
        while process.returncode is None:
            if time.monotonic() >= work_deadline - .1:
                reason = "deadline"
                break
            await tree.refresh(work_deadline)
            if mode == "visual" and not handed_off and "t10_visual_handoff=ready" in lines:
                handoff = json.loads((artifacts / "handoff.json").read_text())
                handoff["pid"] = os.getpid()
                print("t10_visual_handoff=" + json.dumps(handoff, ensure_ascii=False), flush=True)
                handed_off = True
            if stop.is_set():
                reason = "requested_stop"
                break
            if time.monotonic() >= work_deadline - .05:
                reason = "deadline"
                break
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=min(.05, work_deadline - time.monotonic()))
            except TimeoutError:
                pass
    except TimeoutError as error:
        if time.monotonic() >= work_deadline - .1:
            reason = "deadline"
        else:
            primary = error
    except BaseException as error:  # noqa: BLE001 - 有界清理后原样重新抛出主异常/取消
        primary = error
    finally:
        try:
            if process is not None:
                if process.returncode is None:
                    process.terminate()
                    grace = min(deadline - .2, time.monotonic() + TERM_SECONDS)
                    while process.returncode is None and time.monotonic() < grace:
                        await tree.refresh(deadline)
                        await asyncio.sleep(.02)
                await tree.refresh(deadline)
                forced = bool(tree.live)
                if forced:
                    await tree.kill(deadline)
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(asyncio.shield(waiter), max(.01, deadline - time.monotonic()))
                await asyncio.wait_for(reader, max(.01, deadline - time.monotonic()))
                await tree.refresh(deadline)
                observed = tuple(tree.observed.values())
                cooperative_exit = (
                    process.pid in tree.observed
                    and process.returncode == 0
                    and lines.count("t10_child_exit=verified") == 1
                    and not forced
                    and primary is None
                    and (not reason or (mode == "visual" and reason == "requested_stop"))
                )
        except (OSError, RuntimeError, TimeoutError, ValueError):
            cleanup_verified = False
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), max(.001, deadline - time.monotonic()))
                except TimeoutError:
                    pass
        try:
            docker_cleared = await _cleanup_owned(owner, deadline)
            ports_closed = _ports_closed(artifacts, deadline)
            known_cleanup = bool(process is not None and tree is not None and not tree.live
                                 and docker_cleared and ports_closed)
            cleanup_verified = cooperative_exit and known_cleanup
            if cleanup_verified:
                code = 0
        except (OSError, RuntimeError, TimeoutError, ValueError):
            cleanup_verified = False
        finally:
            for kind, handler in previous.items():
                signal.signal(kind, handler)
        if not cleanup_verified or primary is not None:
            code = 2
        if reason == "deadline":
            lines.append("t10_deadline=expired")
        lines.append("t10_cleanup=" + ("verified" if cleanup_verified else "unknown"))
        if not cleanup_verified:
            lines.append("t10_cleanup_owner=" + owner)
        lines.append("t10_processes_observed=" + str(len(observed)))
        lines.append("t10_process_groups_observed=" + str(len({row["pgid"] for row in observed})))
        (artifacts / "lifecycle.json").write_text(json.dumps({"owner": owner, "code": code,
            "cleanup_verified": cleanup_verified, "elapsed": time.monotonic() - started,
            "processes": observed}, indent=2))
    if primary is not None:
        if not cleanup_verified:
            primary.add_note("T10 cleanup_unknown；仅限本次owner=" + owner)
        raise primary
    return RunResult(code, "\n".join(lines), artifacts, cleanup_verified, observed)


async def _child_work(mode):
    from tests.e2e.costing_quote_stack import (
        _run_closed_loop,
        browser_stack,
        visual_handoff,
    )

    if mode == "integration":
        code, output = _run_closed_loop()
        print(output, flush=True)
        if code or "passed" not in output or "skipped" in output:
            return 2
    elif mode == "browser":
        from tests.e2e.test_costing_quote_browser import exercise_browser

        async with browser_stack(_ACTIVE.artifacts) as stack:
            await exercise_browser(stack, _ACTIVE.artifacts)
    else:
        await visual_handoff()
    print("t10_child_exit=verified", flush=True)
    return 0


async def _child_main(mode):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if mode != "visual":
        for kind in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(kind, task.cancel)
    return await _child_work(mode)


def main():
    global _ACTIVE
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=tuple(TOTAL_SECONDS), required=True)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--owner")
    parser.add_argument("--work-until", type=float)
    parser.add_argument("--finish-until", type=float)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    if not args.child:
        if any(value is not None for value in (args.owner, args.work_until, args.finish_until, args.parent_pid)):
            parser.error("仅固定子入口允许内部预算参数")
        result = asyncio.run(run_supervised(args.mode))
        print(result.output, flush=True)
        print("T10浏览器产物：" + str(result.artifacts), flush=True)
        return result.code
    resource_names(args.owner or "")
    now = time.monotonic()
    if (args.parent_pid != os.getppid() or args.work_until is None or args.finish_until is None
        or not now < args.work_until < args.finish_until <= now + TOTAL_SECONDS[args.mode]
        or abs(args.finish_until - args.work_until - CLEANUP_SECONDS) > .001):
        parser.error("无效子入口所有权/预算")
    _ACTIVE = OwnedRun(args.owner, args.work_until, args.finish_until,
                       OUTPUT_ROOT / ("t10-" + args.owner), args.parent_pid)
    try:
        return asyncio.run(_child_main(args.mode))
    except BaseException as error:  # noqa: BLE001 - 子入口只输出固定类别，不泄异常正文
        import traceback
        for frame in traceback.extract_tb(error.__traceback__):
            try:
                path = Path(frame.filename).relative_to(ROOT).as_posix()
            except ValueError:
                continue
            if path.startswith("tests/") and re.fullmatch(r"[A-Za-z0-9_./]+", path):
                print(f"{path}:{frame.lineno}: in {frame.name}", flush=True)
        print("t10_fixture_error=" + type(error).__name__, flush=True)
        return 2


if __name__ == "__main__":
    # -m的__main__与被stack导入的canonical模块必须共享唯一所有权上下文。
    from tests.e2e.costing_quote_lifecycle import main as entrypoint

    raise SystemExit(entrypoint())
