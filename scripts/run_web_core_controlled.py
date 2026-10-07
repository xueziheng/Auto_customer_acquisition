#!/usr/bin/env python3
"""受控Web前台监督入口；不加载.env、不接管既有服务。"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def reserve(port: int) -> socket.socket:
    listener = socket.socket()
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen()
        return listener
    except OSError:
        listener.close()
        raise RuntimeError("port_in_use") from None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="只启动新建的本机受控Web环境；Ctrl-C停止，HUP重启应用。"
    )
    for service in ("api", "web", "scheduler"):
        parser.add_argument("--" + service + "-port", type=int, default=0)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    sockets: list[socket.socket] = []
    try:
        for port in (args.api_port, args.web_port, args.scheduler_port):
            if not 0 <= port <= 65535:
                raise RuntimeError("configuration_invalid")
            sockets.append(reserve(port))
        if shutil.which("node") is None or any(
            importlib.util.find_spec(name) is None
            for name in ("docker", "psutil", "uvicorn", "asyncpg", "boto3", "alembic")
        ):
            raise RuntimeError("dependency_missing")
        from scripts.controlled_web_supervisor import run

        return run(ROOT, args.directory, sockets)
    except Exception as exc:  # noqa: BLE001 安全进程边界不得回显异常
        reason = (
            str(exc)
            if str(exc)
            in {"port_in_use", "dependency_missing", "configuration_invalid"}
            else "startup_failed"
        )
        print(json.dumps({"status": "failed", "reason": reason}), flush=True)
        return 2
    finally:
        for listener in sockets:
            listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
