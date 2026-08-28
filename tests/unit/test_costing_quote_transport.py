"""T10有限传输桥安全/保真保护；不替代真实Linux/API/Browser验收。"""

import base64
import io
import json
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from tests.e2e import costing_quote_bridge as bridge
from tests.e2e import costing_quote_relay as relay
from tests.e2e import costing_quote_server as server


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
