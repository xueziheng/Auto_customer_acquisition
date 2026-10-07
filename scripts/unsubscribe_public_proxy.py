"""Only expose the existing anonymous unsubscribe contract to Tailscale Funnel.

This process never accepts credentials or forwards arbitrary URLs.  Funnel must
target this loopback listener, not the TradeOS API port.
"""

from __future__ import annotations

import argparse
import http.client
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

_TOKEN_PATH = re.compile(
    r"/unsubscribe/[a-z0-9-]{1,32}\.[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}"
)
_FORM = b"List-Unsubscribe=One-Click"
_CONTENT_TYPE = "application/x-www-form-urlencoded"
_RESPONSE_HEADERS = (
    "Content-Type",
    "Cache-Control",
    "Referrer-Policy",
    "Content-Security-Policy",
    "Retry-After",
)


class UnsubscribeProxyHandler(BaseHTTPRequestHandler):
    """Allow only exact GET/POST capability paths to the loopback API."""

    api_port: int
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        # Capability tokens must not enter access logs.
        del format, args

    def _reject(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _forward(self, method: str) -> None:
        if _TOKEN_PATH.fullmatch(self.path) is None:
            self._reject(404)
            return
        body: bytes | None = None
        headers: dict[str, str] = {}
        if method == "POST":
            length = self.headers.get("Content-Length")
            if (
                length != str(len(_FORM))
                or self.headers.get("Content-Type") != _CONTENT_TYPE
            ):
                self._reject(400)
                return
            body = self.rfile.read(len(_FORM))
            if body != _FORM:
                self._reject(400)
                return
            headers["Content-Type"] = _CONTENT_TYPE
        connection = http.client.HTTPConnection("127.0.0.1", self.api_port, timeout=5)
        try:
            connection.request(method, "/api" + self.path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(4097)
            if len(payload) > 4096 or response.status not in {200, 204, 503}:
                self._reject(502)
                return
            self.send_response(response.status)
            for name in _RESPONSE_HEADERS:
                value = response.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (OSError, http.client.HTTPException):
            self._reject(503)
        finally:
            connection.close()

    def do_GET(self) -> None:
        self._forward("GET")

    def do_POST(self) -> None:
        self._forward("POST")

    def do_HEAD(self) -> None:
        self._reject(405)

    def do_PUT(self) -> None:
        self._reject(405)

    def do_DELETE(self) -> None:
        self._reject(405)


def serve(*, api_port: int, listen_port: int) -> None:
    if not (1024 <= api_port <= 65535 and 1024 <= listen_port <= 65535):
        raise ValueError("invalid local port")
    if api_port == listen_port:
        raise ValueError("proxy must not share the API port")
    handler = type("BoundUnsubscribeProxyHandler", (UnsubscribeProxyHandler,), {"api_port": api_port})
    HTTPServer(("127.0.0.1", listen_port), handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback-only unsubscribe proxy")
    parser.add_argument("--api-port", required=True, type=int)
    parser.add_argument("--listen-port", required=True, type=int)
    options = parser.parse_args()
    serve(api_port=options.api_port, listen_port=options.listen_port)


if __name__ == "__main__":
    main()
