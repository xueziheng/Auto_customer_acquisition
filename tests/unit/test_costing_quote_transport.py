"""T10有限传输桥安全/保真保护；不替代真实Linux/API/Browser验收。"""

import base64
import io
import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace

import httpx
import pytest

from tests.e2e import costing_quote_bridge as bridge
from tests.e2e import costing_quote_relay as relay
from tests.e2e import costing_quote_server as server
from tests.e2e import costing_quote_stack as stack_module


@pytest.mark.parametrize("path", ["https://example.test/x", "//example.test/x", "/%2fexample.test", "/a/../b",
                                 "/%2e%2e/b", "/x#fragment", "/x\r\nHost:evil", "/%0d%0a", "/a\\b"])
def test_relay_rejects_non_origin_paths(path):
    with pytest.raises(ValueError):
        relay.request_bytes("GET", path, [], b"")


@pytest.mark.parametrize("method", ["CONNECT", "TRACE", "DELETE", "get", "POST\r\n"])
def test_relay_has_fixed_methods(method):
    with pytest.raises(ValueError):
        relay.request_bytes(method, "/costing-quotes", [], b"")


def test_header_injection_and_size_are_rejected():
    for headers in ([["X-Tenant-Id", "x\r\nHost:evil"]], [["bad:name", "value"]], [["X", "x" * 65537]]):
        with pytest.raises(ValueError):
            relay.clean_headers(headers)
    with pytest.raises(ValueError):
        relay.request_bytes("POST", "/", [], b"x" * (relay.MAX_BYTES + 1))
    with pytest.raises(ValueError):
        relay.decode_body(base64.b64encode(b"x" * (relay.MAX_BYTES + 1)).decode())


@pytest.mark.parametrize("status,body", [(200, b"%PDF-1.7\x00\xff"), (403, b'{"code":"permission_denied"}'), (307, b"")])
def test_fixed_target_preserves_status_auth_cors_and_binary(monkeypatch, status, body, capsys):
    seen = {}

    class Connection:
        def __init__(self, host, port, timeout):
            seen.update(host=host, port=port, timeout=timeout, headers=[])

        def putrequest(self, method, path, **_kwargs):
            seen.update(method=method, path=path)

        def putheader(self, key, value):
            seen["headers"].append((key, value))

        def endheaders(self, data):
            seen["body"] = data

        def getresponse(self):
            return SimpleNamespace(status=status, read=lambda _limit: body, getheaders=lambda: [
                ("Access-Control-Allow-Origin", "http://127.0.0.1:3001"), ("Content-Type", "application/pdf"),
                ("Connection", "keep-alive, X-Hop"), ("X-Hop", "remove"), ("Content-Length", "999"),
                ("Location", "https://not-followed.invalid")])

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(relay.http.client, "HTTPConnection", Connection)
    result = json.loads(relay.relay_request(relay.request_bytes("POST", "/costing-quotes/quotes?x=1", [
        ["Host", "evil.invalid"], ["X-Tenant-Id", "tenant"], ["X-Employee-Id", "employee"],
        ["Origin", "http://127.0.0.1:3001"], ["Connection", "X-Hop"], ["X-Hop", "remove"]], b'{}')))
    assert (seen["host"], seen["port"], seen["timeout"]) == ("127.0.0.1", 8000, 15)
    assert ("Host", "evil.invalid") not in seen["headers"] and ("Host", "127.0.0.1:8000") in seen["headers"]
    assert ("X-Employee-Id", "employee") in seen["headers"] and ("X-Tenant-Id", "tenant") in seen["headers"]
    assert result["status"] == status and relay.decode_body(result["body"]) == body
    assert ["Access-Control-Allow-Origin", "http://127.0.0.1:3001"] in result["headers"]
    assert not any(key.lower() in {"connection", "content-length", "x-hop"} for key, _ in result["headers"])
    assert seen["closed"] and capsys.readouterr() == ("", "")


def test_relay_mode_never_reads_database_or_migrates(monkeypatch):
    class NoEnvironment:
        def get(self, *_args):
            pytest.fail("relay不得读取数据库环境")

    monkeypatch.setattr(server, "os", SimpleNamespace(environ=NoEnvironment()))
    monkeypatch.setattr(server, "migrate", lambda *_: pytest.fail("relay不得迁移"))
    monkeypatch.setattr(server, "serve", lambda *_: pytest.fail("relay不得创建runtime"))
    monkeypatch.setattr(sys, "argv", ["server", "--mode", "relay"])
    monkeypatch.setattr(relay, "relay_main", lambda: 7)
    assert server.main() == 7


def test_linux_relay_error_has_no_payload_log(monkeypatch, capsys):
    monkeypatch.setattr(relay.signal, "alarm", lambda _: 0)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"private invalid content")))
    assert relay.relay_main() == 2
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure,status", [(subprocess.TimeoutExpired("fixed", 15), 504), (RuntimeError("private"), 502)])
def test_bridge_errors_are_not_success_and_no_logs(monkeypatch, capsys, failure, status):
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)
    monkeypatch.setattr(bridge.RelayPool, "request", lambda *_: (_ for _ in ()).throw(failure))
    with bridge.http_bridge(runner) as origin:
        response = httpx.get(origin + "/costing-quotes")
        assert response.status_code == status and response.content == b""
    assert capsys.readouterr() == ("", "")


def test_bridge_body_limit_and_eight_concurrency(monkeypatch):
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def request(_self, *_args):
        observed.append(1)
        if len(observed) == 8:
            entered.set()
        assert release.wait(5)
        return 200, [], b"ok"

    monkeypatch.setattr(bridge.RelayPool, "request", request)
    with bridge.http_bridge(runner) as origin, httpx.Client(timeout=10) as client:
        assert client.post(origin + "/", content=b"x" * (relay.MAX_BYTES + 1)).status_code == 413
        threads = [threading.Thread(target=lambda: client.get(origin + "/")) for _ in range(8)]
        for thread in threads:
            thread.start()
        try:
            assert entered.wait(5)
            assert client.get(origin + "/").status_code == 503
        finally:
            release.set()
            for thread in threads:
                thread.join(5)
                assert not thread.is_alive()
    assert len(observed) == 8


def test_bridge_cleanup_closes_idle_header_connection():
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)
    with bridge.http_bridge(runner) as origin:
        idle = socket.create_connection(("127.0.0.1", int(origin.rsplit(":", 1)[1])))
        idle.sendall(b"GET / HTTP/1.1\r\n")
    idle.settimeout(1)
    assert idle.recv(1) == b""
    idle.close()


def test_bridge_admits_only_eight_connections_before_headers(monkeypatch):
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)
    calls = []
    monkeypatch.setattr(bridge.RelayPool, "request", lambda *_: calls.append(True) or (200, [], b"ok"))
    with bridge.http_bridge(runner) as origin, ExitStack() as sockets:
        address = ("127.0.0.1", int(origin.rsplit(":", 1)[1]))
        for _ in range(8):
            connection = sockets.enter_context(socket.create_connection(address))
            connection.sendall(b"GET / HTTP/1.1\r\nX-Partial: ")
        rejected = sockets.enter_context(socket.create_connection(address))
        rejected.settimeout(.5)
        rejected.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert rejected.recv(4096).startswith(b"HTTP/1.1 503 ")
    assert calls == []


@pytest.mark.parametrize("stage", ["request_line", "headers", "body"])
def test_bridge_absolute_deadline_stops_continuous_drip(monkeypatch, stage, capsys):
    monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", .2)
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)
    calls = []
    monkeypatch.setattr(bridge.RelayPool, "request", lambda *_: calls.append(True) or (200, [], b"ok"))
    prefixes = {"request_line": b"G", "headers": b"GET / HTTP/1.1\r\nX-Partial: ",
                "body": b"POST / HTTP/1.1\r\nContent-Length: 20\r\n\r\n"}
    stopped = threading.Event()
    with bridge.http_bridge(runner) as origin:
        connection = socket.create_connection(("127.0.0.1", int(origin.rsplit(":", 1)[1])))
        connection.settimeout(.55)
        started = time.monotonic()
        connection.sendall(prefixes[stage])

        def drip():
            while not stopped.wait(.04):
                try:
                    connection.sendall(b"x")
                except OSError:
                    break

        writer = threading.Thread(target=drip)
        writer.start()
        try:
            try:
                response = connection.recv(4096)
            except ConnectionResetError:
                response = b""
            assert time.monotonic() - started < .5
            assert not response.startswith(b"HTTP/1.1 200 ")
            assert calls == []
        finally:
            stopped.set()
            connection.close()
            writer.join(1)
            assert not writer.is_alive()
    assert capsys.readouterr() == ("", "")


def test_bridge_deadline_remains_active_through_response(monkeypatch):
    monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", .2)
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)

    def slow_response(*_args):
        time.sleep(.3)
        return 200, [], b"late"

    monkeypatch.setattr(bridge.RelayPool, "request", slow_response)
    with bridge.http_bridge(runner) as origin, socket.create_connection(
        ("127.0.0.1", int(origin.rsplit(":", 1)[1]))
    ) as connection:
        connection.settimeout(.5)
        connection.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert not connection.recv(4096).startswith(b"HTTP/1.1 200 ")


def test_bridge_absolute_deadline_aborts_blocked_response_write(monkeypatch):
    monkeypatch.setattr(bridge, "TIMEOUT_SECONDS", .2)
    original = bridge.make_handler

    def small_send_buffer(pool):
        base = original(pool)

        class Handler(base):
            def setup(self):
                super().setup()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)

        return Handler

    monkeypatch.setattr(bridge, "make_handler", small_send_buffer)
    monkeypatch.setattr(bridge.RelayPool, "request", lambda *_: (200, [], b"x" * relay.MAX_BYTES))
    runner = SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None)
    with bridge.http_bridge(runner) as origin, socket.socket() as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        connection.settimeout(.5)
        connection.connect(("127.0.0.1", int(origin.rsplit(":", 1)[1])))
        connection.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        time.sleep(.3)
        received = bytearray()
        try:
            while chunk := connection.recv(4096):
                received.extend(chunk)
        except ConnectionResetError:
            pass
        # 已发200 header不能撤回，但不完整body必须终止，不能构成成功HTTP响应。
        assert len(received) < relay.MAX_BYTES


def test_pool_timeout_kills_only_own_fixed_exec(monkeypatch):
    seen = {}

    class Process:
        returncode = None

        def communicate(self, payload, timeout):
            seen.update(payload=payload, timeout=timeout)
            raise subprocess.TimeoutExpired("fixed", timeout)

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout):
            assert self.returncode == -9

    process = Process()

    def popen(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    pool = bridge.RelayPool(SimpleNamespace(id="a" * 64, attrs={"ExecIDs": None}, reload=lambda: None))
    with pytest.raises(subprocess.TimeoutExpired):
        pool.request(b"payload", bridge.time.monotonic() + 15)
    assert seen["command"] == ["docker", "exec", "-i", "--env", "TEST_DATABASE_URL=", "a" * 64,
                                "python", "-m", "tests.e2e.costing_quote_server", "--mode", "relay"]
    assert seen["kwargs"]["stderr"] is subprocess.DEVNULL and seen["timeout"] <= 15
    pool.close()
    assert not pool.processes and process.poll() == -9


def test_oversize_upstream_response_fails_closed(monkeypatch):
    closed = []
    response = SimpleNamespace(read=lambda _limit: b"x" * (relay.MAX_BYTES + 1))
    connection = SimpleNamespace(putrequest=lambda *_a, **_k: None, putheader=lambda *_: None,
        endheaders=lambda _: None, getresponse=lambda: response, close=lambda: closed.append(True))
    monkeypatch.setattr(relay.http.client, "HTTPConnection", lambda *_a, **_k: connection)
    with pytest.raises(ValueError, match="body_too_large"):
        relay.relay_request(relay.request_bytes("GET", "/costing-quotes", [], b""))
    assert closed == [True]


@pytest.fixture
def stopped_stack(monkeypatch):
    """仅替换外部Docker边界；实际linux_stack退出/清理控制流保留。"""
    removed = []
    outcome = SimpleNamespace(code=0, logs=(
        b"t10_worker_started_cycles=1\nt10_forbidden_gateway_calls=0\n"
        b"t10_runtime_exit=verified\n"
    ))
    network = SimpleNamespace(name="owned-network", id="owned-network",
        attrs={"Internal": True}, remove=lambda: removed.append("network"))

    class Container:
        def __init__(self, kind):
            self.kind = self.id = kind
            self.status = "running"
            self.attrs = {"State": {"ExitCode": 0, "Status": "running", "OOMKilled": False},
                "HostConfig": {"ReadonlyRootfs": True, "Binds": [], "Memory": 1073741824,
                    "PidsLimit": 128, "PortBindings": {}},
                "NetworkSettings": {"Networks": {network.name: {}}}}

        def exec_run(self, _command):
            return SimpleNamespace(exit_code=0)

        def reload(self):
            assert self.kind not in removed

        def stop(self, timeout):
            self.status = "exited"
            self.attrs["State"].update(ExitCode=outcome.code if self.kind == "runner" else 0,
                                       Status="exited")

        def logs(self):
            assert self.kind not in removed
            return outcome.logs

        def remove(self, **_kwargs):
            removed.append(self.kind)

    pg, runner = Container("pg"), Container("runner")
    client = SimpleNamespace(networks=SimpleNamespace(create=lambda *_a, **_kw: network,
        get=lambda _: network), containers=SimpleNamespace(
            run=lambda image, *_a, **_kw: pg if image == "pgvector/pgvector:pg16" else runner,
            list=lambda **_: []), close=lambda: removed.append("client"))
    monkeypatch.setattr(stack_module.docker, "from_env", lambda: client)
    monkeypatch.setattr(stack_module, "current_image", lambda: "controlled-image")
    return outcome, removed


@pytest.mark.parametrize("failure", ["exit2", "killed", "missing_receipt", "runtime_assertion"])
def test_linux_stack_rejects_unsuccessful_runtime_exit(stopped_stack, failure):
    outcome, removed = stopped_stack
    if failure == "exit2":
        outcome.code = 2
    elif failure == "killed":
        outcome.code = 137
    elif failure == "missing_receipt":
        outcome.logs = b"t10_worker_started_cycles=1\nt10_forbidden_gateway_calls=0\n"
    else:
        outcome.code, outcome.logs = 2, b"t10_fixture_error=AssertionError\n"
    with pytest.raises(AssertionError, match="退出验收"), stack_module.linux_stack(mode="browser"):
        pass
    assert removed == ["runner", "pg", "network", "client"]


def test_linux_stack_preserves_primary_cancellation_during_failed_exit(stopped_stack):
    import asyncio

    outcome, removed = stopped_stack
    outcome.code = 2
    primary = asyncio.CancelledError("controlled-primary")
    with pytest.raises(asyncio.CancelledError) as captured, stack_module.linux_stack(mode="browser"):
        raise primary
    assert captured.value is primary
    assert removed == ["runner", "pg", "network", "client"]


def test_linux_stack_accepts_verified_runtime_exit(stopped_stack):
    _, removed = stopped_stack
    with stack_module.linux_stack(mode="browser"):
        pass
    assert removed == ["runner", "pg", "network", "client"]


def test_server_runtime_cleanup_failure_has_no_success_receipt(monkeypatch, capsys):
    async def fail(*_args):
        raise AssertionError("private-runtime-cleanup")

    monkeypatch.setenv("TEST_DATABASE_URL", "controlled-test-connection")
    monkeypatch.setattr(server, "migrate", lambda _: None)
    monkeypatch.setattr(server, "serve", fail)
    monkeypatch.setattr(sys, "argv", ["server", "--mode", "browser"])
    assert server.main() == 2
    assert capsys.readouterr() == ("t10_fixture_error=AssertionError\n", "")


def test_server_success_receipt_is_after_runtime_cleanup(monkeypatch, capsys):
    async def complete(*_args):
        assert capsys.readouterr() == ("", "")
        print("controlled_cleanup_completed")

    monkeypatch.setenv("TEST_DATABASE_URL", "controlled-test-connection")
    monkeypatch.setattr(server, "migrate", lambda _: None)
    monkeypatch.setattr(server, "serve", complete)
    monkeypatch.setattr(sys, "argv", ["server", "--mode", "browser"])
    assert server.main() == 0
    assert capsys.readouterr() == ("controlled_cleanup_completed\nt10_runtime_exit=verified\n", "")


def test_unit_action_diagnostics_allow_only_fixed_numeric_fields():
    allowed = ["t10_unit_diag_requests=1", "t10_unit_diag_failed=0", "t10_unit_diag_responses=1",
               "t10_unit_diag_disabled=1", "t10_unit_diag_selection_start=0",
               "t10_unit_diag_selection_end=9", "t10_unit_diag_text_length=30"]
    allowed.extend(["t10_unit_probe_initial_response=0", "t10_unit_probe_failed=1",
                    "t10_unit_probe_response=0", "t10_unit_probe_summary_visible=0", "t10_unit_probe_disabled=1"])
    forbidden = ["t10_unit_diag_body=private", "t10_unit_diag_requests=https://private.invalid",
                 "t10_unit_diag_disabled=2", "t10_unit_diag_selection_start=0 private",
                 "t10_unit_diag_unknown=1", "t10_unit_diag_url=1"]
    forbidden.extend(["t10_unit_probe_body=private", "t10_unit_probe_response=2",
                      "t10_unit_probe_response=https://private.invalid"])
    assert stack_module.safe_output("\n".join([*allowed, *forbidden]).encode()) == "\n".join(allowed)
