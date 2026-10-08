"""隔离命名空间内的标准库桥接器；不持有 Provider 凭证或宿主配置。"""

from __future__ import annotations

import http.server
import json
import os
import resource
import socket
import stat
import struct
import subprocess
import sys
import threading
from pathlib import Path

MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_FRAME_BYTES = 24 * 1024 * 1024


def _receive(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = connection.recv(min(65536, count - len(result)))
        if not part:
            raise ValueError("bridge_frame_invalid")
        result.extend(part)
    return bytes(result)


class Handler(http.server.BaseHTTPRequestHandler):
    """只接受本命名空间内 CLI 的固定 Responses POST，禁止日志回显。"""

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/responses" or not 0 < size <= MAX_HTTP_BYTES:
                self.send_error(400, "request_rejected")
                return
            payload = self.rfile.read(size)
            if len(payload) != size:
                raise ValueError("bridge_frame_invalid")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(300)
                channel.connect("/broker.sock")
                channel.sendall(struct.pack("!I", len(payload)) + payload)
                length = struct.unpack("!I", _receive(channel, 4))[0]
                if not 0 < length <= MAX_FRAME_BYTES:
                    raise ValueError("bridge_frame_invalid")
                envelope = json.loads(_receive(channel, length))
            status = envelope["status"]
            body = envelope["body"].encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "text/event-stream" if status == 200 else "application/json",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:  # noqa: BLE001 - 进程边界禁止回显 Provider 和资料原文。
            self.close_connection = True


def main() -> int:
    """单次 CLI 生命周期；stdout 只传有界最终结果，无 CLI 日志。"""
    try:
        settings = json.loads(Path("/settings.json").read_text())
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_CPU, (settings["timeout_seconds"] + 5,) * 2)
        os.umask(0o077)
        for name in ("/state", "/output"):
            Path(name).mkdir(exist_ok=True)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        config = {
            "model": settings["model"],
            "model_provider": "tradeos",
            "model_providers.tradeos.name": "TradeOS",
            "model_providers.tradeos.base_url": f"http://127.0.0.1:{port}",
            "model_providers.tradeos.wire_api": "responses",
            "model_providers.tradeos.requires_openai_auth": False,
            "model_providers.tradeos.request_max_retries": 0,
            "model_providers.tradeos.stream_max_retries": 0,
            "approval_policy": "never",
            "web_search": "disabled",
            "allow_login_shell": False,
            "project_doc_max_bytes": 0,
            "features.shell_tool": False,
            "features.shell_snapshot": False,
            "features.apps": False,
            "features.plugins": False,
            "features.browser_use": False,
            "features.computer_use": False,
            "features.multi_agent": False,
            "features.daemon_auto_start": False,
            "shell_environment_policy.inherit": "none",
        }
        command = [
            "/codex",
            "--no-daemon",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
            "-C",
            "/input",
            "--output-last-message",
            "/output/final.json",
        ]
        for key, value in config.items():
            command.extend(["-c", key + "=" + json.dumps(value)])
        command.append("-")
        content = Path("/input/document-0001.md").read_bytes()
        if len(content) > settings["max_input_bytes"]:
            raise ValueError("input_limit")
        result = subprocess.run(
            command,
            input=content,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=settings["timeout_seconds"],
            env={
                "HOME": "/state",
                "CODEX_HOME": "/state",
                "PATH": "/usr/bin",
                "LANG": "C.UTF-8",
                "RUST_LOG": "off",
            },
            check=False,
        )
        server.shutdown()
        server.server_close()
        if result.returncode != 0:
            return 2
        fd = os.open("/output/final.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 0 < info.st_size <= settings["max_output_bytes"]
            ):
                return 2
            final = os.read(fd, settings["max_output_bytes"] + 1).decode("utf-8")
        finally:
            os.close(fd)
        print(json.dumps({"final": final}, ensure_ascii=False))
        return 0
    except Exception:  # noqa: BLE001 - 进程边界禁止回显 Provider 和资料原文。
        return 2


if __name__ == "__main__":
    sys.exit(main())
