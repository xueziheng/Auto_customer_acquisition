"""监督器单元测试专用子入口；不导入pytest或业务/Docker运行环境。"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


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
    if scenario.startswith("chromium_") and sys.platform == "linux":
        executable = Path(sys.argv[5])
        assert executable.is_absolute() and executable.is_file()
        browser = subprocess.Popen([str(executable), "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-background-networking", "--disable-component-update", "--no-first-run",
            "--no-default-browser-check", "--remote-debugging-port=0",
            "--user-data-dir=" + str(artifacts / "browser-profile"), "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        while True:
            assert browser.poll() is None, "real_chromium_exited_before_ready"
            output = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,pgid="], text=True)
            rows = [tuple(map(int, line.split())) for line in output.splitlines() if len(line.split()) == 3]
            owned = {browser.pid}
            while additions := {pid for pid, ppid, _ in rows if ppid in owned} - owned:
                owned.update(additions)
            if len(owned) >= 3:
                break
            time.sleep(.01)
        (artifacts / "chromium.json").write_text(json.dumps([
            {"pid": pid, "pgid": pgid, "parent": os.getpid()} for pid, _, pgid in rows if pid in owned]))
        if scenario == "chromium_forced":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(20)
        time.sleep(.2)
        browser.terminate()
        browser.wait(timeout=1)
    elif scenario.startswith("chromium_"):
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
    while not (artifacts / "observed").exists():
        time.sleep(.001)
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
    if len(sys.argv) >= 5 and sys.argv[4]:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = sys.argv[4]
    raise SystemExit(fixture_main(sys.argv[2], Path(sys.argv[3])))
