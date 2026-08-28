"""T10宿主有限loopback桥；固定Docker exec，原业务HTTP完全由Uvicorn处理。"""

import json
import os
import re
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.e2e.costing_quote_relay import (
    MAX_BYTES,
    MAX_ENVELOPE,
    METHODS,
    TIMEOUT_SECONDS,
    clean_headers,
    decode_body,
    request_bytes,
)


class RelayPool:
    """只持本次固定容器与本次子进程，不能接任意命令/目标。"""

    def __init__(self, runner):
        if re.fullmatch(r"[a-f0-9]{64}", runner.id) is None:
            raise ValueError("invalid_container")
        self.runner = runner
        self.gate = threading.BoundedSemaphore(8)
        self.lock = threading.Lock()
        self.processes = set()
        self.connections = set()
        self.closed = False

    def request(self, payload, deadline):
        with self.lock:
            if self.closed:
                raise RuntimeError("bridge_closed")
            process = subprocess.Popen(
                ["docker", "exec", "-i", "--env", "TEST_DATABASE_URL=", self.runner.id,
                 "python", "-m", "tests.e2e.costing_quote_server", "--mode", "relay"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={"PATH": os.environ["PATH"]},
            )
            self.processes.add(process)
        try:
            output, _ = process.communicate(payload, timeout=max(.001, deadline - time.monotonic()))
            if process.returncode != 0 or len(output) > MAX_ENVELOPE:
                raise RuntimeError("relay_failed")
            result = json.loads(output)
            if not isinstance(result, dict) or set(result) != {"status", "headers", "body"}:
                raise ValueError("invalid_response")
            status = result["status"]
            if type(status) is not int or not 200 <= status <= 599:
                raise ValueError("invalid_status")
            return status, clean_headers(result["headers"]), decode_body(result["body"])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            with self.lock:
                self.processes.discard(process)

    def close(self):
        with self.lock:
            self.closed = True
            for connection in tuple(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            processes = tuple(self.processes)
            for process in processes:
                if process.poll() is None:
                    process.kill()
        for process in processes:
            process.wait(timeout=2)
        # CLI被杀不等于容器exec已退出；relay独立alarm有界终止，实际inspect再确认。
        deadline = time.monotonic() + TIMEOUT_SECONDS + 2
        while True:
            self.runner.reload()
            if not self.runner.attrs.get("ExecIDs"):
                break
            if time.monotonic() >= deadline:
                raise AssertionError("T10 relay exec未回收")
            time.sleep(.05)
        assert all(process.poll() is not None for process in processes)


def make_handler(pool):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(TIMEOUT_SECONDS)
            with pool.lock:
                if pool.closed:
                    raise ConnectionAbortedError
                pool.connections.add(self.connection)

        def finish(self):
            try:
                super().finish()
            finally:
                with pool.lock:
                    pool.connections.discard(self.connection)

        def log_message(self, *_args):
            pass

        def reply(self, status, headers=(), body=b""):
            self.send_response_only(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.close_connection = True

        def execute(self):
            if not pool.gate.acquire(blocking=False):
                self.reply(503)
                return
            try:
                deadline = time.monotonic() + TIMEOUT_SECONDS
                self.connection.settimeout(TIMEOUT_SECONDS)
                lengths = self.headers.get_all("Content-Length", [])
                if (self.command not in METHODS or self.headers.get("Transfer-Encoding")
                    or len(lengths) > 1 or (lengths and not lengths[0].isdigit())):
                    self.reply(400)
                    return
                length = int(lengths[0]) if lengths else 0
                if length > MAX_BYTES:
                    self.reply(413)
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self.reply(400)
                    return
                payload = request_bytes(self.command, self.path, list(self.headers.items()), body)
                status, headers, response = pool.request(payload, deadline)
                self.reply(status, headers, response)
            except (TimeoutError, subprocess.TimeoutExpired):
                self.reply(504)
            except ValueError:
                self.reply(400)
            except (OSError, RuntimeError):
                self.reply(502)
            finally:
                pool.gate.release()

        do_GET = do_HEAD = do_POST = do_OPTIONS = execute

        def do_CONNECT(self):
            self.reply(405)

    return Handler


@contextmanager
def http_bridge(runner):
    pool = RelayPool(runner)

    class Server(ThreadingHTTPServer):
        def handle_error(self, *_args):
            # 客户端断开/关闭中的socket不输出任何请求或传输异常正文。
            pass

    server = Server(("127.0.0.1", 0), make_handler(pool))
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, name="t10-http-bridge")
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield origin
    finally:
        server.shutdown()
        try:
            pool.close()
        finally:
            server.server_close()
            thread.join(timeout=2)
        assert not thread.is_alive() and not pool.processes and not pool.connections
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", server.server_port)) != 0
