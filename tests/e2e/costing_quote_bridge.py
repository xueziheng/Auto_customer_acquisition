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
        self.deadlines = {}
        self.closed = False

    def request(self, payload, deadline):
        with self.lock:
            if self.closed:
                raise RuntimeError("bridge_closed")
            if time.monotonic() >= deadline:
                raise TimeoutError
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
            self.deadline = pool.deadlines[self.connection]
            self.remaining()

        def remaining(self):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.connection.settimeout(remaining)
            return remaining

        def log_message(self, *_args):
            pass

        def parse_request(self):
            source = self.rfile

            class HeaderLines:
                last = None

                def readline(self, limit):
                    self.last = source.readline(limit)
                    return self.last

            lines = HeaderLines()
            self.rfile = lines
            try:
                parsed = super().parse_request()
            finally:
                self.rfile = source
            # 标准库接受headers中途EOF；已取消的不完整请求不能触发业务调用。
            if lines.last not in (b"\r\n", b"\n"):
                self.close_connection = True
                return False
            return parsed

        def reply(self, status, headers=(), body=b""):
            self.remaining()
            self.send_response_only(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.remaining()
                self.wfile.write(body)
            self.close_connection = True

        def execute(self):
            try:
                self.remaining()
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
                self.remaining()
                if len(body) != length:
                    self.reply(400)
                    return
                payload = request_bytes(self.command, self.path, list(self.headers.items()), body)
                status, headers, response = pool.request(payload, self.deadline)
                self.reply(status, headers, response)
            except (TimeoutError, subprocess.TimeoutExpired):
                self.reply(504)
            except ValueError:
                self.reply(400)
            except (OSError, RuntimeError):
                self.reply(502)

        do_GET = do_HEAD = do_POST = do_OPTIONS = execute

        def do_CONNECT(self):
            self.reply(405)

    return Handler


@contextmanager
def http_bridge(runner):
    pool = RelayPool(runner)

    class Server(ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            self.accepted_deadline = time.monotonic() + TIMEOUT_SECONDS
            return connection, address

        def process_request(self, connection, address):
            # 容量在解析首字节/创建处理线程之前占用；半开请求也占一席。
            if not pool.gate.acquire(blocking=False):
                try:
                    connection.setblocking(False)
                    connection.send(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass
                finally:
                    self.shutdown_request(connection)
                return
            with pool.lock:
                pool.connections.add(connection)
                pool.deadlines[connection] = self.accepted_deadline
            try:
                super().process_request(connection, address)
            except BaseException:
                self.release(connection)
                raise

        def release(self, connection):
            with pool.lock:
                pool.connections.discard(connection)
                pool.deadlines.pop(connection, None)
            pool.gate.release()

        def process_request_thread(self, connection, address):
            def expire():
                # socket超时是空闲预算；独立绝对时钟才能中断持续滴流/阻塞写。
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()

            timer = threading.Timer(max(0, pool.deadlines[connection] - time.monotonic()), expire)
            timer.start()
            try:
                super().process_request_thread(connection, address)
            finally:
                timer.cancel()
                timer.join()
                self.release(connection)

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
