"""T10唯一Linux入口：固定pytest或有限Uvicorn；不接受路径/命令。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import traceback
from pathlib import Path


def migrate(connection):
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "DATABASE_URL": connection,
             "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True, check=False, timeout=60,
    )
    if result.returncode:
        raise RuntimeError("T10隔离迁移失败")


def integration(connection):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/integration/test_costing_quote_closed_loop.py",
         "-q", "--tb=short", "-p", "no:cacheprovider"],
        cwd=Path(__file__).resolve().parents[2],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "TEST_DATABASE_URL": connection,
             "TRADEOS_T10_ISOLATED_CHILD": "1", "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True, check=False, timeout=240,
    )
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if re.fullmatch(
            r"t10_parser_(?:status=unavailable;failure=(?:platform|resource|protocol|runtime|unknown)"
            r"|probe=(?:cpu|as|wall|ipc|unknown);exit=(?:-?[0-9]{1,3}|unknown)"
            r";reason=(?:ok|short|timeout|oversized|unknown))",
            line,
        ) or re.fullmatch(r"runtime_http_error=[A-Za-z0-9_]+", line):
            print(line)
        elif line.startswith(("FAILED tests/", "ERROR tests/")):
            print(line.split(" - ", 1)[0])
        elif re.fullmatch(r"[a-zA-Z0-9_/]+\.py:[0-9]+: in [a-zA-Z0-9_]+", line):
            print(line)
        elif match := re.match(r"E\s+([a-zA-Z][a-zA-Z0-9_.]*(?:Error|Exception)):", line):
            print("fixed_exception_type=" + match[1])
        elif line and line[0].isdigit() and any(word in line for word in (" passed", " failed", " error", " skipped")):
            print(line)
    return result.returncode


async def serve(connection, mode):
    import pytest
    import uvicorn
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.integration.costing_quote_case import (
        prepare_customer_facts,
        runtime_case,
    )

    origins = json.loads(os.environ["T10_CORS_ORIGINS"])
    engine = create_async_engine(connection)
    try:
        with pytest.MonkeyPatch.context() as patch:
            async with runtime_case(engine, patch, cors_origins=origins) as case:
                await prepare_customer_facts(case)
                manifest = {
                    "tenant": case.tenant, "opportunity": case.opportunity, "need": case.need,
                    "source": case.source, "message": case.message,
                    "policy_source": case.policy_source,
                    "actor": case.actor, "boss": case.file_actor_id, "decider": case.decider,
                    "statement": case.statement,
                    "now": case.clock[0].isoformat(),
                }
                print("t10_manifest=" + json.dumps(manifest), flush=True)
                server = uvicorn.Server(uvicorn.Config(
                    case.app, host="0.0.0.0", port=8000, lifespan="off", access_log=False,
                ))
                # 真lifespan由runtime_case持有；Uvicorn不得第二次启动/关闭parser。
                # Uvicorn 退出时会重放信号；必须完成外层 worker/engine 清理后才退出 PID 1。
                previous_handler = signal.signal(
                    signal.SIGTERM, lambda *_: setattr(server, "should_exit", True)
                )
                task = asyncio.create_task(server.serve())
                try:
                    async with asyncio.timeout(900 if mode == "visual" else 300):
                        await asyncio.shield(task)
                except TimeoutError:
                    server.should_exit = True
                    await asyncio.wait_for(task, timeout=10)
                finally:
                    server.should_exit = True
                    if not task.done():
                        await asyncio.wait_for(task, timeout=10)
                    signal.signal(signal.SIGTERM, previous_handler)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("integration", "browser", "visual", "relay"), required=True)
    mode = parser.parse_args().mode
    if mode == "relay":
        from tests.e2e.costing_quote_relay import relay_main

        return relay_main()
    connection = os.environ.get("TEST_DATABASE_URL")
    if not connection:
        print("not_run：缺少隔离测试连接")
        return 2
    try:
        migrate(connection)
        if mode == "integration":
            return integration(connection)
        asyncio.run(serve(connection, mode))
        print("t10_runtime_exit=verified", flush=True)
        return 0
    except Exception as error:  # noqa: BLE001 - 不输出环境/数据库原异常正文
        root = Path(__file__).resolve().parents[2]
        for frame in traceback.extract_tb(error.__traceback__):
            try:
                relative = Path(frame.filename).resolve().relative_to(root).as_posix()
            except ValueError:
                continue
            name = "module" if frame.name == "<module>" else frame.name
            if re.fullmatch(r"tests/[a-zA-Z0-9_/]+\.py", relative) and re.fullmatch(r"[a-zA-Z0-9_]+", name):
                print(f"{relative}:{frame.lineno}: in {name}")
        print("t10_fixture_error=" + type(error).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
