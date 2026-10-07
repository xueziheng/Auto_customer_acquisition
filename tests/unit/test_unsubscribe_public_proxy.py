"""The public Funnel target must never expose the authenticated API."""

from __future__ import annotations

import http.client
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

from scripts.unsubscribe_public_proxy import UnsubscribeProxyHandler

TOKEN = "v1." + "A" * 43 + "." + "B" * 43
FORM = b"List-Unsubscribe=One-Click"


class _Upstream(BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str, bytes, str | None]]] = []

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_GET(self) -> None:
        self.seen.append(("GET", self.path, b"", self.headers.get("Cookie")))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"unsubscribe")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.seen.append(("POST", self.path, body, self.headers.get("Cookie")))
        self.send_response(204)
        self.end_headers()


@contextmanager
def _servers() -> Iterator[int]:
    _Upstream.seen = []
    upstream = HTTPServer(("127.0.0.1", 0), _Upstream)
    handler = type(
        "BoundUnsubscribeProxyHandler",
        (UnsubscribeProxyHandler,),
        {"api_port": upstream.server_port},
    )
    public = HTTPServer(("127.0.0.1", 0), handler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (upstream, public)
    ]
    try:
        for thread in threads:
            thread.start()
        yield public.server_port
    finally:
        for server in (public, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def _request(
    port: int, method: str, path: str, body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_only_exact_unsubscribe_route_reaches_upstream() -> None:
    with _servers() as port:
        for path in ("/api/health/ready", "/unsubscribe/x", f"/unsubscribe/{TOKEN}?next=/api"):
            assert _request(port, "GET", path)[0] == 404
        assert _Upstream.seen == []
        assert _request(
            port, "GET", f"/unsubscribe/{TOKEN}",
            headers={"Cookie": "session=private"},
        ) == (200, b"unsubscribe")
        assert _Upstream.seen == [("GET", f"/api/unsubscribe/{TOKEN}", b"", None)]


def test_one_click_body_only_and_no_other_method() -> None:
    with _servers() as port:
        path = f"/unsubscribe/{TOKEN}"
        assert _request(port, "POST", path, b"bad", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 400
        assert _request(port, "PUT", path)[0] == 405
        assert _Upstream.seen == []
        assert _request(port, "POST", path, FORM, {"Content-Type": "application/x-www-form-urlencoded"})[0] == 204
        assert _Upstream.seen == [("POST", "/api" + path, FORM, None)]
